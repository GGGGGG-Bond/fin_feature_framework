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
    """确保 schema_version 表存在（首次迁移用）。"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version         INTEGER PRIMARY KEY,
            name            TEXT NOT NULL,
            applied_at      TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    conn.commit()


def _get_current_version(conn: sqlite3.Connection) -> int:
    """获取当前迁移版本，表不存在返回 0。"""
    try:
        cur = conn.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1")
        row = cur.fetchone()
        return row[0] if row else 0
    except sqlite3.OperationalError:
        return 0


def _apply_migration(conn: sqlite3.Connection, version: int, name: str, fn: Callable) -> None:
    """在事务中执行迁移，失败回滚不更新版本号。"""
    try:
        fn(conn)
        conn.execute("INSERT INTO schema_version (version, name) VALUES (?, ?)", (version, name))
        conn.commit()
    except Exception as e:
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
        DDL_RECONCILIATION_RESULT,
        DDL_FEATURE_COMPUTE_LOG,
    )

    for ddl in [DDL_INGEST_RUN, DDL_RAW_SNAPSHOT, DDL_STATEMENT_VERSION,
                DDL_RECONCILIATION_RESULT, DDL_FEATURE_COMPUTE_LOG]:
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


def _migrate_raw_to_statement_version(conn: sqlite3.Connection) -> None:
    """旧 raw_* 数据迁移到 statement_version。

    约束：
    - evidence_sufficient=0（无原始响应，不编造）
    - snapshot_id=NULL、run_id=NULL（不编造采集批次）
    - observed_at 用 crawled_at（若为 NULL 则用迁移时间）
    - metric_hash/semantic_hash 用标准化字段内容计算
    - observation_seq 按 (secucode, report_date, table_type, source_name) 分组排序
    - is_superseded：同组内仅最新为 0，其余为 1
    - revision_type：统一标 unknown（不编造修订类型）
    - 旧 raw_* 表数据保留不删除（复制不是移动）
    """
    import hashlib
    import json
    from datetime import datetime

    migration_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 三表映射
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
        # 按分组排序，生成 observation_seq
        query = f"""
            SELECT
                secucode, report_date, notice_date, crawled_at, rowid,
                {', '.join(fields)}
            FROM {raw_table}
            ORDER BY secucode, report_date,
                     COALESCE(notice_date, crawled_at, '{migration_time}'),
                     crawled_at, rowid
        """
        cur = conn.execute(query)

        # 分组内分配 observation_seq 和 is_superseded
        groups = {}
        for row in cur.fetchall():
            secucode = row[0]
            report_date = row[1]
            key = (secucode, report_date, table_type, "eastmoney")
            if key not in groups:
                groups[key] = []
            groups[key].append(row)

        for key, rows in groups.items():
            secucode, report_date, table_type, source_name = key
            for seq, row in enumerate(rows, start=1):
                notice_date = row[2]
                crawled_at = row[3] or migration_time
                field_values = row[5:]

                # 构造标准化字段 JSON
                standardized = {}
                for i, field in enumerate(fields):
                    if field_values[i] is not None:
                        standardized[field] = field_values[i]

                # 计算哈希
                serialized = json.dumps(standardized, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
                metric_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
                # semantic_hash 阶段 2 简化：与 metric_hash 相同（单一来源 eastmoney）
                semantic_hash = metric_hash

                # is_superseded：仅最新为 0
                is_superseded = 0 if seq == len(rows) else 1

                conn.execute("""
                    INSERT INTO statement_version (
                        version_id, snapshot_id, run_id, secucode, report_date,
                        table_type, source_name, notice_date, revision_type,
                        observed_at, metric_hash, semantic_hash, observation_seq,
                        consolidate_scope, period_basis, currency, amount_unit,
                        industry_type, standardized_fields, evidence_sufficient,
                        is_superseded
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    "unknown",  # currency
                    "unknown",  # amount_unit
                    "unknown",  # industry_type
                    serialized,  # standardized_fields
                    0,  # evidence_sufficient
                    is_superseded,
                ))


def verify_migration(conn: sqlite3.Connection) -> dict:
    """验证迁移结果，返回指标 dict。

    返回：
    - raw_count: 三表 raw_* 记录数合计
    - sv_count: statement_version 记录数
    - sv_evidence_insufficient: evidence_sufficient=0 的记录数
    - sv_superseded: is_superseded=1 的记录数
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

    return {
        "raw_count": sum(raw_counts.values()),
        "sv_count": sv_count,
        "sv_evidence_insufficient": sv_evidence_insufficient,
        "sv_superseded": sv_superseded,
        "schema_version": schema_version,
        "raw_counts_by_table": raw_counts,
        "sv_counts_by_type": sv_by_type,
    }


# 迁移注册表（按顺序）
MIGRATIONS: list[tuple[int, str, Callable]] = [
    (1, "v1_baseline", migrate_v1_baseline),
    (2, "v2_stage2", migrate_v2_stage2),
]
