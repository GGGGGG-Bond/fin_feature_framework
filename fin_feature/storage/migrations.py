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
    """运行所有未应用的迁移。"""
    _ensure_schema_version_table(conn)
    current = _get_current_version(conn)

    for version, name, fn in MIGRATIONS:
        if version > current:
            _apply_migration(conn, version, name, fn)


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
    cur = conn.execute(f"PRAGMA table_info({table})")
    columns = {row[1] for row in cur.fetchall()}
    if column not in columns:
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


# 迁移注册表（按顺序）
MIGRATIONS: list[tuple[int, str, Callable]] = [
    (1, "v1_baseline", migrate_v1_baseline),
    (2, "v2_stage2", migrate_v2_stage2),
    (3, "v3_rereview_fixes", migrate_v3_rereview_fixes),
]
