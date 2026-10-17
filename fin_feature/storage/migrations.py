"""迁移框架：schema_version 管理 + 迁移函数。

替代 ad-hoc 加列方式，每个迁移在独立事务中执行，失败回滚不更新版本号。
"""

import sqlite3
from typing import Callable

from fin_feature.storage.schema import (
    ALL_DDL,
    ALL_INDEXES,
    DDL_FEATURES_ADD_COLUMNS,
    ALL_INDEXES_STAGE2,
)


def _ensure_schema_version_table(conn: sqlite3.Connection) -> None:
    """确保 schema_version 表存在（首次迁移用）。R7 修复：不提交外层事务。"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version         INTEGER PRIMARY KEY,
            name            TEXT NOT NULL,
            applied_at      TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    # 不提交，由 _apply_migration 或调用方控制事务


def _get_current_version(conn: sqlite3.Connection) -> int:
    """获取当前迁移版本，表不存在返回 0。"""
    try:
        cur = conn.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1")
        row = cur.fetchone()
        return row[0] if row else 0
    except sqlite3.OperationalError:
        return 0


def _apply_migration(conn: sqlite3.Connection, version: int, name: str, fn: Callable) -> None:
    """在显式事务中执行迁移，失败回滚不残留 DDL 或数据变更。

    R7 修复：支持嵌入外层事务，只管理自己的 savepoint，不提交外层数据。
    """
    in_transaction = conn.in_transaction  # 检查是否已有外层事务

    conn.execute("SAVEPOINT migration_savepoint")
    try:
        fn(conn)
        conn.execute("INSERT INTO schema_version (version, name) VALUES (?, ?)", (version, name))
        conn.execute("RELEASE SAVEPOINT migration_savepoint")
        # R7：仅当无外层事务时才提交
        if not in_transaction:
            conn.commit()
    except Exception as e:
        conn.execute("ROLLBACK TO SAVEPOINT migration_savepoint")
        # R7：仅当无外层事务时才回滚整个连接
        if not in_transaction:
            conn.rollback()
        raise RuntimeError(f"迁移 v{version} ({name}) 失败：{e}") from e


def run_migrations(conn: sqlite3.Connection) -> None:
    """运行所有未应用的迁移。

    2B：迁移完成后，若库自称 v4 则校验必需表/列/索引齐全，
    避免"版本号对但结构不完整"的库被静默接受。
    """
    _ensure_schema_version_table(conn)
    current = _get_current_version(conn)

    if current >= 4:
        verify_v4_structure(conn)

    for version, name, fn in MIGRATIONS:
        if version > current:
            _apply_migration(conn, version, name, fn)

    if _get_current_version(conn) >= 4:
        verify_v4_structure(conn)
    if _get_current_version(conn) >= 5:
        from fin_feature.storage.clean_model import verify_v5_structure
        verify_v5_structure(conn)


def migrate_v1_baseline(conn: sqlite3.Connection) -> None:
    """v1 基线迁移：兼容旧库，确保基础表存在。

    - 执行 ALL_DDL（幂等 CREATE TABLE IF NOT EXISTS）
    - 确保 features.definition_version 列存在
    - 创建基础索引
    """
    # 执行所有 DDL（幂等）
    for ddl in ALL_DDL:
        conn.execute(ddl)

    # 确保 features.definition_version 列存在（阶段 1 已有，这里兼容空库）
    _ensure_column_exists(conn, "features", "definition_version", "TEXT")

    # 创建基础索引
    for idx in ALL_INDEXES:
        conn.execute(idx)


def migrate_v2_stage2(conn: sqlite3.Connection) -> None:
    """v2 阶段 2 迁移：新增 6 张表 + features 加列 + 旧数据迁移。

    - 创建 6 张新表（schema_version 已在 v1 创建，这里跳过重复）
    - features 加 6 列（幂等）
    - 旧 raw_* 数据迁移到 statement_version
    - 创建阶段 2 索引
    """
    # 创建阶段 2 表（schema_version 已存在，跳过）
    from fin_feature.storage.schema import (
        DDL_INGEST_RUN,
        DDL_RAW_SNAPSHOT,
        DDL_STATEMENT_VERSION,
        DDL_STATEMENT_OBSERVATION,
        DDL_RECONCILIATION_RESULT,
        DDL_FEATURE_COMPUTE_LOG,
    )

    for ddl in [DDL_INGEST_RUN, DDL_RAW_SNAPSHOT, DDL_STATEMENT_VERSION,
                DDL_STATEMENT_OBSERVATION, DDL_RECONCILIATION_RESULT,
                DDL_FEATURE_COMPUTE_LOG]:
        conn.execute(ddl)

    # features 加 6 列（幂等）
    _add_features_columns(conn)

    # 旧 raw_* 数据迁移到 statement_version
    _migrate_raw_to_statement_version(conn)

    # 创建阶段 2 索引
    for idx in ALL_INDEXES_STAGE2:
        conn.execute(idx)


def _ensure_column_exists(conn: sqlite3.Connection, table: str, column: str, dtype: str) -> None:
    """幂等添加列，已存在则跳过。"""
    if not _column_exists(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {dtype}")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """检查表是否存在。"""
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return cur.fetchone() is not None


def _statement_version_needs_upgrade(conn: sqlite3.Connection) -> bool:
    """检查已存在的 statement_version 是否缺少新的哈希约束。"""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='statement_version'"
    ).fetchone()
    if row is None:
        return False
    return "NOT GLOB" not in (row[0] or "")


def _upgrade_statement_version(conn: sqlite3.Connection) -> None:
    """升级旧 v2 草稿库的 statement_version 以补全新约束。

    SQLite 无法 ALTER 添加 CHECK 约束，只能重建。仅当表为空时安全重建
    （未发布的草稿库通常为空）；含数据时拒绝，提示从受支持的阶段一副本迁移，
    不静默接受、也不直接删除原库数据。
    """
    count = conn.execute("SELECT COUNT(*) FROM statement_version").fetchone()[0]
    if count:
        raise RuntimeError(
            f"旧 v2 草稿库的 statement_version 含 {count} 条数据且缺少哈希约束，"
            "无法自动升级；请保留原库并从受支持的阶段一副本迁移。"
        )
    conn.execute("DROP TABLE statement_version")


def migrate_v3_rereview_fixes(conn: sqlite3.Connection) -> None:
    """v3 复审修复迁移：补齐旧 v2 草稿库缺失的结构。

    A2 修复：5390dac/4f99ee8/181c4b6 等旧 v2 草稿库记录 version=2 但缺少
    statement_observation 表、观察元信息列、新哈希 CHECK 约束和阶段 2 索引。
    本迁移幂等补齐，且在无法安全升级时明确拒绝（不静默接受、不删原库数据）：
    - 旧 statement_version 缺新约束且为空 → 重建；含数据 → 拒绝
    - 幂等补齐全部阶段 2 表（旧草稿库缺 statement_observation 等）
    - 补 statement_observation 的 A3 观察元信息列
    - 创建阶段 2 索引
    """
    from fin_feature.storage.schema import (
        DDL_INGEST_RUN,
        DDL_RAW_SNAPSHOT,
        DDL_STATEMENT_VERSION,
        DDL_STATEMENT_OBSERVATION,
        DDL_RECONCILIATION_RESULT,
        DDL_FEATURE_COMPUTE_LOG,
        ALL_INDEXES_STAGE2,
    )

    # 1. 旧 statement_version 缺新约束 → 空表重建，含数据则拒绝
    if _table_exists(conn, "statement_version") and _statement_version_needs_upgrade(conn):
        _upgrade_statement_version(conn)

    # 2. 幂等补齐全部阶段 2 表（旧草稿库缺 statement_observation 等）
    for ddl in [DDL_INGEST_RUN, DDL_RAW_SNAPSHOT, DDL_STATEMENT_VERSION,
                DDL_STATEMENT_OBSERVATION, DDL_RECONCILIATION_RESULT,
                DDL_FEATURE_COMPUTE_LOG]:
        conn.execute(ddl)

    # 3. 补 A3 观察元信息列（181c4b6 v2 库的 statement_observation 缺这些列）
    if _table_exists(conn, "statement_observation"):
        _ensure_column_exists(conn, "statement_observation", "notice_date", "TEXT")
        _ensure_column_exists(conn, "statement_observation", "revision_notice_date", "TEXT")
        _ensure_column_exists(conn, "statement_observation", "revision_type", "TEXT DEFAULT 'unknown'")

    # 4. 创建阶段 2 索引（旧 v2 草稿库缺索引；已有则 IF NOT EXISTS 跳过）
    for idx in ALL_INDEXES_STAGE2:
        conn.execute(idx)


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """检查列是否存在（表不存在时返回 False）。"""
    if not _table_exists(conn, table):
        return False
    cur = conn.execute(f"PRAGMA table_info({table})")
    return column in {row[1] for row in cur.fetchall()}


def _index_exists(conn: sqlite3.Connection, index: str) -> bool:
    """检查索引是否存在。"""
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name=?", (index,)
    )
    return cur.fetchone() is not None


def _normalize_index_predicate(sql: str) -> set[str] | None:
    """从 CREATE INDEX 的 SQL 文本提取规范化的部分条件集合。

    D1：不能只看有无 WHERE——WHERE 0 / 反向 IS NULL / 遗漏非空条件都须拒绝。
    把 WHERE 子句拆成以 AND 连接的条件，规范化为 "列名 IS NOT NULL" 形式。
    无法识别为等价写法时返回 None（调用方应拒绝，不静默接受）。
    """
    import re
    upper = sql.upper()
    if " WHERE " not in upper:
        return set()
    # 提取 WHERE 之后的全部文本（去掉末尾分号/空白）
    where_idx = upper.index(" WHERE ") + len(" WHERE ")
    clause = sql[where_idx:].strip().rstrip(";").strip()
    conditions = re.split(r"\s+AND\s+", clause, flags=re.IGNORECASE)
    normalized: set[str] = set()
    for cond in conditions:
        cond = cond.strip()
        # 匹配 "列名 IS NOT NULL"（允许任意空白）
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s+IS\s+NOT\s+NULL\s*$", cond, re.IGNORECASE)
        if m:
            normalized.add(f"{m.group(1).lower()} IS NOT NULL")
        else:
            # 无法识别的条件——返回 None 表示不可证明等价
            return None
    return normalized


def _index_definition(conn: sqlite3.Connection, index: str) -> dict | None:
    """读取索引定义：unique、表、列、部分条件谓词。

    C1/D1：同名普通索引冒充唯一索引、错列或错误部分条件都必须能被识别。
    D1：partial 布尔不够——具体谓词须可比较；无法证明等价时返回 predicate=None。
    返回 None 表示索引不存在。
    """
    row = conn.execute(
        "SELECT tbl_name, sql FROM sqlite_master WHERE type='index' AND name=?",
        (index,),
    ).fetchone()
    if row is None:
        return None
    table, sql = row[0], row[1] or ""
    # sqlite_master.sql 对自动索引（如 UNIQUE 约束）为 NULL；显式 CREATE INDEX 才有文本
    is_partial = " WHERE " in sql.upper()
    predicate = _normalize_index_predicate(sql)
    # unique 由 PRAGMA index_list 给出（origin='c' 为约束自动索引，也唯一）
    unique = False
    columns: tuple[str, ...] = ()
    for info in conn.execute(f"PRAGMA index_list({table})").fetchall():
        if info[1] == index:
            unique = bool(info[2])
            break
    for col in conn.execute(f"PRAGMA index_info({index})").fetchall():
        columns += (col[2],)
    return {"unique": unique, "table": table, "columns": columns,
            "partial": is_partial, "predicate": predicate}


def _add_features_columns(conn: sqlite3.Connection) -> None:
    """features 表加 6 列（幂等）。"""
    columns_to_add = [
        ("compute_run_id", "TEXT"),
        ("input_version_ids", "TEXT"),
        ("code_commit", "TEXT"),
        ("config_hash", "TEXT"),
        ("as_of_date", "TEXT"),
        ("not_computable_reason", "TEXT"),
    ]
    for column, dtype in columns_to_add:
        _ensure_column_exists(conn, "features", column, dtype)


MIGRATION_AMBIGUITY_PREFIX = "迁移投影："


# 契约依据：docs/stage-2/2b-schema.md。
# v4 必须在"已发布 v3"之上做增量建表/加列/建索引，不重写 v1/v2/v3 的 DDL 或数据含义，
# 也不把 v4 对象塞进 ALL_DDL（否则空库会由 v1 提前创建 v4 结构）。
V4_REQUIRED_TABLES = ("ingest_unit", "ingest_attempt", "raw_snapshot", "statement_observation")
V4_REQUIRED_INDEXES = (
    "uq_raw_snapshot_attempt",
    "ix_ingest_unit_run_status",
    "ix_ingest_attempt_unit",
    "uq_observation_source_row",
)

# R6/C1：新表的运行依赖列（缺列会在业务落盘时报 SQL 错误，应在初始化阶段拒绝）。
# 必须覆盖全部实际写入字段，不能只列主键与状态字段——started_at/ended_at/
# failure_code/detail 都是 mark_unit_running/finish_unit 的写入目标。
V4_INGEST_UNIT_REQUIRED_COLUMNS = (
    "unit_id", "run_id", "unit_key", "source_name", "unit_kind", "report_name",
    "secucode", "request_scope", "status", "started_at", "ended_at",
    "failure_code", "source_rows", "selected_rows", "new_versions",
    "new_observations", "compat_writes", "detail",
)
V4_INGEST_ATTEMPT_REQUIRED_COLUMNS = (
    "attempt_id", "unit_id", "page_number", "http_attempt", "started_at",
    "ended_at", "outcome", "error_code", "detail",
)

# C1/D1：关键索引的期望定义（unique、列集、具体部分谓词），用于结构复查。
# 同名但非唯一、错列或错误部分条件的索引必须被识别为缺口。
# D1：partial 布尔不够——WHERE 0 / 反向 IS NULL / 遗漏非空条件都须拒绝。
# predicate 是规范化的部分条件集合（列名 IS NOT NULL），用于精确比较。
V4_REQUIRED_INDEX_SPECS = {
    "uq_raw_snapshot_attempt": {"unique": True, "table": "raw_snapshot",
                                "columns": ("attempt_id",), "partial": True,
                                "predicate": {"attempt_id IS NOT NULL"}},
    "uq_observation_source_row": {"unique": True, "table": "statement_observation",
                                  "columns": ("snapshot_id", "row_index"), "partial": True,
                                  "predicate": {"snapshot_id IS NOT NULL",
                                                "row_index IS NOT NULL"}},
}


def migrate_v4_ingestion_evidence(conn: sqlite3.Connection) -> None:
    """v4 采集证据迁移：新增 ingest_unit / ingest_attempt，扩展 raw_snapshot 与观察表。

    增量、幂等、可重试：
    - 新表 CREATE TABLE IF NOT EXISTS
    - 新增列先查 PRAGMA（故障回滚后重试不会重复 ALTER）
    - 索引 IF NOT EXISTS
    - 旧行不被删除或改写：raw_snapshot 旧行落 legacy-unverified，
      statement_observation 旧行 row_index 保持 NULL
    - 不在迁移函数中 executescript（避免隐式提交打断事务）
    """
    from fin_feature.storage.schema import (
        DDL_INGEST_ATTEMPT,
        DDL_INGEST_UNIT,
        V4_INDEXES,
        V4_RAW_SNAPSHOT_COLUMNS,
        V4_STATEMENT_OBSERVATION_COLUMNS,
    )

    # 1. 新表（依赖 ingest_run，须在已发布的 v3 结构之后）
    conn.execute(DDL_INGEST_UNIT)
    conn.execute(DDL_INGEST_ATTEMPT)

    # 2. raw_snapshot 增列（旧行默认 legacy-unverified / legacy_unverified）
    for column, spec in V4_RAW_SNAPSHOT_COLUMNS:
        _ensure_column_exists(conn, "raw_snapshot", column, spec)

    # 3. statement_observation 增列（旧行 row_index / entry_time 保持 NULL）
    for column, spec in V4_STATEMENT_OBSERVATION_COLUMNS:
        _ensure_column_exists(conn, "statement_observation", column, spec)

    # 4. 索引
    for idx in V4_INDEXES:
        conn.execute(idx)


def collect_v4_structure_gaps(conn: sqlite3.Connection) -> list[str]:
    """列出 v4 必需结构的缺失项（只读，不修改库）。

    R6 修复：不只检查新表名字，还检查 ingest_unit / ingest_attempt 的必需列，
    以及关键索引定义，避免"版本号对、表名对但缺列"的库被静默接受。

    C1 修复：必需列覆盖全部运行写入字段（started_at/ended_at/failure_code/detail）；
    关键唯一索引不只查名字，还查 unique 标志、列集与部分条件——同名非唯一索引、
    错列或错误部分条件的索引都报为缺口。
    """
    from fin_feature.storage.schema import (
        V4_RAW_SNAPSHOT_COLUMNS,
        V4_STATEMENT_OBSERVATION_COLUMNS,
    )

    gaps: list[str] = []
    for table in V4_REQUIRED_TABLES:
        if not _table_exists(conn, table):
            gaps.append(f"缺少表 {table}")
    for column, _spec in V4_RAW_SNAPSHOT_COLUMNS:
        if not _column_exists(conn, "raw_snapshot", column):
            gaps.append(f"缺少列 raw_snapshot.{column}")
    for column, _spec in V4_STATEMENT_OBSERVATION_COLUMNS:
        if not _column_exists(conn, "statement_observation", column):
            gaps.append(f"缺少列 statement_observation.{column}")
    # R6/C1：新表的必需列（运行依赖，缺列会在业务落盘时报 SQL 错误）
    for column in V4_INGEST_UNIT_REQUIRED_COLUMNS:
        if not _column_exists(conn, "ingest_unit", column):
            gaps.append(f"缺少列 ingest_unit.{column}")
    for column in V4_INGEST_ATTEMPT_REQUIRED_COLUMNS:
        if not _column_exists(conn, "ingest_attempt", column):
            gaps.append(f"缺少列 ingest_attempt.{column}")
    # C1/D1：关键索引不只查存在，还查定义（unique/列/具体部分谓词）
    for index in V4_REQUIRED_INDEXES:
        if not _index_exists(conn, index):
            gaps.append(f"缺少索引 {index}")
            continue
        spec = V4_REQUIRED_INDEX_SPECS.get(index)
        if spec is None:
            continue
        actual = _index_definition(conn, index)
        if actual is None:
            gaps.append(f"缺少索引 {index}")
            continue
        if spec["unique"] and not actual["unique"]:
            gaps.append(f"索引 {index} 应为 UNIQUE 但实际非唯一")
        if actual["columns"] != spec["columns"]:
            gaps.append(
                f"索引 {index} 列集不符：期望 {spec['columns']}，实际 {actual['columns']}"
            )
        if spec["partial"] and not actual["partial"]:
            gaps.append(f"索引 {index} 应为部分索引（含 WHERE）但实际无部分条件")
        # D1：比较具体部分谓词，不只看有无 WHERE
        expected_pred = spec.get("predicate")
        actual_pred = actual.get("predicate")
        if expected_pred is not None:
            if actual_pred is None:
                gaps.append(
                    f"索引 {index} 部分谓词无法识别为等价写法，拒绝接受："
                    f"期望 {sorted(expected_pred)}"
                )
            elif actual_pred != expected_pred:
                gaps.append(
                    f"索引 {index} 部分谓词不符：期望 {sorted(expected_pred)}，"
                    f"实际 {sorted(actual_pred)}"
                )
    return gaps


def verify_v4_structure(conn: sqlite3.Connection) -> None:
    """版本自称 v4 却缺必需表/列/索引时明确拒绝，不静默接受不完整库。"""
    gaps = collect_v4_structure_gaps(conn)
    if gaps:
        raise RuntimeError(
            "数据库声明为 v4 但结构不完整，拒绝在缺失结构上继续运行："
            + "；".join(gaps)
        )


def backup_database_for_migration(conn: sqlite3.Connection, target_path):
    """用 SQLite backup API 生成一致性副本（含 WAL 已提交内容）。

    契约要求：
    - 真实已有库升级前先生成副本；失败即停止迁移
    - 唯一新文件且不覆盖既有文件（目标已存在直接拒绝）
    - 失败时清理本次未完成的副本，原库与既有文件不受影响

    R5 修复：先写唯一私有临时备份，再用 os.link 原子不覆盖发布到目标。
    检查后普通 connect 不是"不覆盖"——并发可在检查与 connect 之间抢先创建目标。
    失败清理只删除自己拥有的临时文件，绝不 unlink 目标（目标可能是别人创建的）。
    """
    import os
    import sqlite3 as _sqlite3
    import uuid as _uuid
    from pathlib import Path as _Path

    target = _Path(target_path)
    if target.exists():
        raise RuntimeError(f"备份目标已存在，拒绝覆盖：{target}")
    target.parent.mkdir(parents=True, exist_ok=True)

    # 私有临时文件：只有本调用知道其名字，失败时只清理它
    temp = target.parent / f".{target.name}.{_uuid.uuid4().hex}.tmp"
    dest = _sqlite3.connect(str(temp))
    try:
        conn.backup(dest)
        dest.commit()
    except Exception:
        dest.close()
        temp.unlink(missing_ok=True)  # 只删自己的临时文件
        raise
    finally:
        try:
            dest.close()
        except Exception:
            pass

    # 原子不覆盖发布：os.link 创建目标，已存在则说明被并发抢先，本方失败
    try:
        os.link(temp, target)
    except FileExistsError:
        temp.unlink(missing_ok=True)
        raise RuntimeError(f"备份目标已被并发创建，拒绝覆盖：{target}")
    except OSError as exc:
        temp.unlink(missing_ok=True)
        raise RuntimeError(f"备份发布失败（errno={exc.errno}）：{target}") from exc
    finally:
        # link 成功后临时硬链接计数=2，unlink 临时名只减计数，不删目标内容
        temp.unlink(missing_ok=True)
    return target


# 已有数据的表：任一非空即视为"已有库"，升级前必须先备份
_BACKUP_SENSITIVE_TABLES = (
    "stock_list",
    "raw_financial_indicator",
    "raw_income_statement",
    "raw_balance_sheet",
    "clean_financial_indicator",
    "clean_income_statement",
    "clean_balance_sheet",
    "features",
    "ingest_run",
    "raw_snapshot",
    "statement_version",
    "statement_observation",
)


def _database_has_existing_data(conn: sqlite3.Connection) -> bool:
    """只读判断库内是否已有受保护数据（决定升级前是否需要备份）。"""
    for table in _BACKUP_SENSITIVE_TABLES:
        if not _table_exists(conn, table):
            continue
        if conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None:
            return True
    return False


def default_backup_path(db_path, *, when=None):
    """生成唯一的新备份文件名（``<db>.backup-<UTC 时间戳>[-n]``），绝不指向既有文件。"""
    from datetime import datetime, timezone
    from pathlib import Path as _Path

    base = _Path(db_path)
    stamp = (when or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    candidate = base.with_name(f"{base.name}.backup-{stamp}")
    suffix = 1
    while candidate.exists():
        candidate = base.with_name(f"{base.name}.backup-{stamp}-{suffix}")
        suffix += 1
    return candidate


def migrate_with_backup(conn: sqlite3.Connection, db_path, *, backup_path=None):
    """已有库升级入口：先做一致性副本，再迁移；备份失败即停止，不执行任何迁移。

    返回实际写入的备份路径；空库（无版本记录且无数据）直接迁移并返回 None。
    目标文件与既有备份不覆盖：显式目标已存在时拒绝，自动命名保证唯一。
    """
    needs_backup = _get_current_version(conn) > 0 or _database_has_existing_data(conn)
    if not needs_backup:
        run_migrations(conn)
        return None

    target = backup_path if backup_path is not None else default_backup_path(db_path)
    backup_database_for_migration(conn, target)  # 失败即抛出，迁移不执行
    run_migrations(conn)
    return target


def _log_migration_ambiguity(
    conn: sqlite3.Connection,
    raw_table: str,
    secucode: str,
    report_date: str,
    ordered: list[dict],
) -> None:
    """记录迁移投影中的并列歧义（B1）。

    与阶段一 `deduplicate` 判断同口径：组内最新两条在 (notice_date, entry_time)
    上并列时（同日同 entry_time，或同日均无 entry_time），按内容摘要确定性选取，
    并写入与阶段一同名的 `dedup_ambiguous` 日志保留歧义，不声称恢复真实
    公告/修订先后。detail 前缀用于区分阶段一自身写下的同名日志。
    """
    if len(ordered) < 2 or not _table_exists(conn, "clean_log"):
        return

    chosen, prev = ordered[-1], ordered[-2]
    chosen_date = chosen.get("notice_date") or ""
    prev_date = prev.get("notice_date") or ""
    if chosen_date != prev_date:
        return

    chosen_entry = chosen.get("entry_time") or ""
    prev_entry = prev.get("entry_time") or ""
    if chosen_entry and chosen_entry != prev_entry:
        return

    if chosen_entry:
        detail = (
            f"{MIGRATION_AMBIGUITY_PREFIX}同日同 entry_time={chosen_entry} 多版本，"
            "按内容摘要选取，不声称恢复真实修订先后"
        )
    else:
        detail = (
            f"{MIGRATION_AMBIGUITY_PREFIX}同日 notice_date={chosen_date} 无 entry_time，"
            "按内容摘要选取，不声称恢复真实修订先后"
        )

    conn.execute(
        "INSERT INTO clean_log (secucode, report_date, source_table, action, detail) "
        "VALUES (?, ?, ?, 'dedup_ambiguous', ?)",
        (secucode, report_date, raw_table, detail),
    )


def _migrate_raw_to_statement_version(conn: sqlite3.Connection) -> None:
    """旧 raw_* 数据迁移到 statement_version。

    R4-a/R4-b 修复：复用统一标准化入口 standardize_record（传入固定 unit_map），
    保留有限数检查与非法输入处理；currency 统一标 unknown（不推断 CNY），
    哈希与 INSERT 使用同一规范化对象，避免两套字面值漂移。

    B1 修复：读取完整旧来源元信息（含 entry_time），当前投影复用阶段一已验收的
    选值规则 `fin_feature.cleaning.dedup.sort_key`（notice_date → entry_time →
    内容摘要），不再用 `COALESCE(notice_date, crawled_at)` 排序——未知公告日不能
    借采集时间获得公告优先级。

    投影语义（B1：排序含义必须明确，不声称恢复真实先后）：
    - evidence_sufficient=0（无原始响应，不编造）
    - snapshot_id=NULL、run_id=NULL（不编造采集批次）
    - observed_at 用 crawled_at（若为 NULL 则用迁移时间）：这是**系统观察时间**，
      不是公告或修订时间
    - notice_date 用规范化后的原始公告日；未知保持 NULL，不用采集时间补齐，
      也不制造日期证据（原值仍保留在 raw_* 表）
    - observation_seq 是组内按阶段一选值规则的**升序位置**，序号最大者为当前投影；
      它是确定性的排序位置，**不代表已证实的公告或修订先后**
    - is_superseded：同组内仅选中的最后一条为 0，其余为 1
    - 并列歧义（同为同日同 entry_time，或同日均无 entry_time）按内容摘要确定性
      选取，并以 `dedup_ambiguous` 记入 clean_log 保留歧义
    - revision_type：统一标 unknown（不编造修订类型）
    - metric_hash/semantic_hash 用 hashing 模块统一计算
    - 旧 raw_* 表数据保留不删除（复制不是移动）；无法解析报告期的记录保留原值，
      不因规范化失败被丢弃
    """
    from datetime import datetime

    # R4-b 修复：使用统一标准化入口，传入固定 unit_map（不受运行时 _unit_overrides 污染）
    from fin_feature.cleaning.dedup import sort_key as stage1_sort_key
    from fin_feature.features.standardize import standardize_record, FIELD_UNITS
    from fin_feature.storage.hashing import compute_metric_hash, compute_semantic_hash, canonical_json, normalize_numeric
    from fin_feature.utils.date_utils import normalize_date

    migration_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 三表映射：(raw 表，table_type, [字段列表])
    table_mappings = [
        ("raw_financial_indicator", "financial_indicator", [
            "basic_eps", "total_operate_income", "parent_netprofit", "weightavg_roe",
            "revenue_yoy", "profit_yoy", "bps", "gross_margin", "revenue_qoq", "profit_qoq",
        ]),
        ("raw_income_statement", "income_statement", [
            "total_operate_income", "total_operate_cost", "operate_cost", "operate_expense",
            "sale_expense", "manage_expense", "finance_expense", "operate_profit",
            "total_profit", "income_tax", "parent_netprofit", "deduct_parent_netprofit",
        ]),
        ("raw_balance_sheet", "balance_sheet", [
            "total_assets", "fixed_asset", "monetary_funds", "accounts_receivable",
            "inventory", "total_liabilities", "accounts_payable", "total_equity",
            "current_ratio", "debt_asset_ratio",
        ]),
    ]

    for raw_table, table_type, fields in table_mappings:
        # B1 修复：SELECT * 读取完整旧来源元信息（含 entry_time），
        # 供阶段一选值规则消费。SQL 的 ORDER BY 只用于在同一 DB 上给
        # 完全并列（含内容摘要并列）的行一个可复现的物理顺序，不参与选值判断。
        order_columns = "secucode, report_date, notice_date, entry_time, rowid"
        cur = conn.execute(f"SELECT * FROM {raw_table} ORDER BY {order_columns}")

        # 分组键与阶段一同口径：规范化 report_date 后再分组
        groups: dict[tuple, list[dict]] = {}
        for row in cur.fetchall():
            record = dict(row)
            secucode = record.get("secucode")
            # 无法解析报告期时保留原值，只对齐可解析的规范化结果，不丢弃历史记录
            report_date = normalize_date(record.get("report_date")) or record.get("report_date")
            record["report_date"] = report_date
            # 公告日规范化后再参与选值；未知保持 None，不借用采集时间补齐
            record["notice_date"] = normalize_date(record.get("notice_date"))
            key = (secucode, report_date, table_type, "eastmoney")
            groups.setdefault(key, []).append(record)

        for key, records in groups.items():
            secucode, report_date, table_type, source_name = key

            # B1 修复：复用阶段一已验收的选值规则，不依赖输入顺序，
            # 也不把采集先后当作已证实的公告或修订先后
            ordered = sorted(records, key=stage1_sort_key)

            # 元信息并列时保留歧义记录（与阶段一同口径、同日志动作）
            _log_migration_ambiguity(conn, raw_table, secucode, report_date, ordered)

            for seq, record in enumerate(ordered, start=1):
                notice_date = record.get("notice_date")
                crawled_at = record.get("crawled_at") or migration_time

                # 构造原始记录（用于标准化）
                raw_record = {f: record.get(f) for f in fields if record.get(f) is not None}

                # R4-b 修复：调用统一标准化入口，传入固定 unit_map
                # 保留有限数检查（NaN/Inf → None + invalid_value）、布尔检查、未知单位处理
                standardized_dict, _diagnostics = standardize_record(
                    raw_record, list(raw_record.keys()), unit_map=FIELD_UNITS
                )

                # raw_fields：保留原计量值（R4：原始证据可追溯）
                # R4-b：非有限值（NaN/Inf）无法 JSON 序列化，转为可逆字符串标记
                import math
                raw_fields_safe = {}
                for f, v in raw_record.items():
                    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                        raw_fields_safe[f] = str(v)  # "inf"/"-inf"/"nan"，可逆
                    else:
                        raw_fields_safe[f] = v
                raw_fields_json = canonical_json(raw_fields_safe)

                # standardized_fields：标准单位值（过滤 None 值）
                standardized_filtered = {k: v for k, v in standardized_dict.items() if v is not None}
                # A6 修复：统一数值表示，避免 100 与 100.0 产生不同哈希
                standardized_filtered = normalize_numeric(standardized_filtered)
                standardized_json = canonical_json(standardized_filtered)

                # R4-a 修复：currency 统一标 unknown（不推断 CNY），哈希与 INSERT 使用同一值
                currency = "unknown"
                metric_hash = compute_metric_hash(standardized_filtered)
                semantic_hash = compute_semantic_hash(
                    standardized_filtered,
                    source_name=source_name,
                    currency=currency,
                )

                # is_superseded：仅组内按阶段一规则选中的最后一条为 0
                is_superseded = 0 if seq == len(ordered) else 1

                conn.execute("""
                    INSERT INTO statement_version (
                        version_id, snapshot_id, run_id, secucode, report_date,
                        table_type, source_name, notice_date, revision_type,
                        observed_at, metric_hash, semantic_hash, observation_seq,
                        consolidate_scope, period_basis, currency, amount_unit,
                        industry_type, standardized_fields, raw_fields, evidence_sufficient,
                        is_superseded
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    f"{secucode}_{report_date}_{table_type}_{seq}",  # 临时 version_id
                    None,  # snapshot_id
                    None,  # run_id
                    secucode,
                    report_date,
                    table_type,
                    source_name,
                    notice_date,
                    "unknown",  # revision_type
                    crawled_at,  # observed_at
                    metric_hash,
                    semantic_hash,
                    seq,  # observation_seq
                    "unknown",  # consolidate_scope
                    "unknown",  # period_basis
                    currency,  # currency（与哈希一致：unknown）
                    "unknown",  # amount_unit
                    "unknown",  # industry_type
                    standardized_json,  # standardized_fields（标准单位）
                    raw_fields_json,  # raw_fields（原计量）
                    0,  # evidence_sufficient（无原始响应）
                    is_superseded,
                ))


def verify_migration(conn: sqlite3.Connection) -> dict:
    """验证迁移结果，返回指标 dict。

    返回：
    - raw_count: 三表 raw_* 记录数合计
    - sv_count: statement_version 记录数
    - sv_evidence_insufficient: evidence_sufficient=0 的记录数
    - sv_superseded: is_superseded=1 的记录数
    - sv_current_count: is_superseded=0 的当前投影条数（B1）
    - sv_groups_not_single_current: 当前投影不唯一的组数，应为 0（B1）
    - migration_ambiguous_selections: 迁移投影保留的并列歧义条数（B1）
    - schema_version: 当前迁移版本
    - raw_counts_by_table: 各 raw_* 表记录数
    - sv_counts_by_type: 各 table_type 记录数
    """
    raw_counts = {}
    for table in ["raw_financial_indicator", "raw_income_statement", "raw_balance_sheet"]:
        cur = conn.execute(f"SELECT COUNT(*) FROM {table}")
        raw_counts[table] = cur.fetchone()[0]

    cur = conn.execute("SELECT COUNT(*) FROM statement_version")
    sv_count = cur.fetchone()[0]

    cur = conn.execute("SELECT COUNT(*) FROM statement_version WHERE evidence_sufficient = 0")
    sv_evidence_insufficient = cur.fetchone()[0]

    cur = conn.execute("SELECT COUNT(*) FROM statement_version WHERE is_superseded = 1")
    sv_superseded = cur.fetchone()[0]

    cur = conn.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1")
    schema_version = cur.fetchone()[0] if cur.fetchone else 0

    # 重新获取 schema_version（上面 fetchone 已消耗）
    cur = conn.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1")
    row = cur.fetchone()
    schema_version = row[0] if row else 0

    # 各 table_type 记录数
    sv_by_type = {}
    cur = conn.execute("""
        SELECT table_type, COUNT(*) FROM statement_version GROUP BY table_type
    """)
    for row in cur.fetchall():
        sv_by_type[row[0]] = row[1]

    # B1 验证：当前投影条数，以及"每组当前唯一"成立与否（应为 0）
    cur = conn.execute("SELECT COUNT(*) FROM statement_version WHERE is_superseded = 0")
    sv_current_count = cur.fetchone()[0]

    cur = conn.execute("""
        SELECT COUNT(*) FROM (
            SELECT secucode, report_date, table_type, source_name
            FROM statement_version
            GROUP BY secucode, report_date, table_type, source_name
            HAVING SUM(CASE WHEN is_superseded = 0 THEN 1 ELSE 0 END) <> 1
        )
    """)
    sv_groups_not_single_current = cur.fetchone()[0]

    # B1 验证：迁移投影保留的并列歧义条数（不含阶段一自身写下的同名日志）
    migration_ambiguous_selections = 0
    if _table_exists(conn, "clean_log"):
        cur = conn.execute(
            "SELECT COUNT(*) FROM clean_log "
            "WHERE action = 'dedup_ambiguous' AND detail LIKE ?",
            (f"{MIGRATION_AMBIGUITY_PREFIX}%",),
        )
        migration_ambiguous_selections = cur.fetchone()[0]

    return {
        "raw_count": sum(raw_counts.values()),
        "sv_count": sv_count,
        "sv_evidence_insufficient": sv_evidence_insufficient,
        "sv_superseded": sv_superseded,
        "sv_current_count": sv_current_count,
        "sv_groups_not_single_current": sv_groups_not_single_current,
        "migration_ambiguous_selections": migration_ambiguous_selections,
        "schema_version": schema_version,
        "raw_counts_by_table": raw_counts,
        "sv_counts_by_type": sv_by_type,
    }


from fin_feature.storage.clean_model import migrate_v5_clean_lineage

# 迁移注册表（按顺序）
MIGRATIONS: list[tuple[int, str, Callable]] = [
    (1, "v1_baseline", migrate_v1_baseline),
    (2, "v2_stage2", migrate_v2_stage2),
    (3, "v3_rereview_fixes", migrate_v3_rereview_fixes),
    (4, "v4_ingestion_evidence", migrate_v4_ingestion_evidence),
    (5, "v5_clean_lineage", migrate_v5_clean_lineage),
]
