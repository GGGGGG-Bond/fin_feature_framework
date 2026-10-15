"""数据访问层：原始数据写入、清洗数据写入、特征写入、按维度查询、增量水位、清洗日志。

所有写入使用 INSERT OR REPLACE（upsert），保证幂等。
"""

import sqlite3
from typing import Any, Iterable

from fin_feature.constants import CLEAN_TABLES, RAW_TABLES


class Repository:
    """统一的数据访问层，封装所有表的 CRUD 与查询。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._table_columns_cache: dict[str, set[str]] = {}

    def _get_table_columns(self, table: str) -> set[str]:
        """获取表的列名集合（带缓存）。"""
        if table not in self._table_columns_cache:
            rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
            self._table_columns_cache[table] = {r[1] for r in rows}
        return self._table_columns_cache[table]

    # ------------------------------------------------------------------
    # 原始数据写入
    # ------------------------------------------------------------------
    def upsert_stock_list(self, records: Iterable[dict]) -> int:
        """批量写入股票列表。"""
        rows = list(records)
        if not rows:
            return 0
        sql = (
            "INSERT OR REPLACE INTO stock_list "
            "(secucode, security_code, security_name, market) "
            "VALUES (:secucode, :security_code, :security_name, :market)"
        )
        with self.conn:
            self.conn.executemany(sql, rows)
        return len(rows)

    def batch_upsert_raw(self, table_suffix: str, records: Iterable[dict]) -> int:
        """批量 upsert 原始数据到对应原始表。

        table_suffix: financial_indicator / income_statement / balance_sheet
        """
        rows = list(records)
        if not rows:
            return 0
        table = RAW_TABLES[table_suffix]
        # 查询表的实际列名，只写入表中存在的列（丢弃映射后多余的列）
        table_cols = self._get_table_columns(table)
        cols = [c for c in rows[0].keys() if c in table_cols]
        if not cols:
            return 0
        placeholders = ", ".join(f":{c}" for c in cols)
        col_list = ", ".join(cols)
        sql = f"INSERT OR REPLACE INTO {table} ({col_list}) VALUES ({placeholders})"
        # 过滤每行，只保留有效列
        filtered = [{c: r.get(c) for c in cols} for r in rows]
        with self.conn:
            self.conn.executemany(sql, filtered)
        return len(rows)

    # ------------------------------------------------------------------
    # 原始数据查询
    # ------------------------------------------------------------------
    def get_raw_by_company(
        self, table_suffix: str, secucode: str
    ) -> list[sqlite3.Row]:
        """按公司查询原始数据（全部报告期）。"""
        table = RAW_TABLES[table_suffix]
        sql = (
            f"SELECT * FROM {table} WHERE secucode = ? "
            "ORDER BY report_date DESC, notice_date DESC"
        )
        return self.conn.execute(sql, (secucode,)).fetchall()

    def get_raw_by_date_range(
        self, table_suffix: str, start_date: str, end_date: str
    ) -> list[sqlite3.Row]:
        """按报告期日期范围查询原始数据。"""
        table = RAW_TABLES[table_suffix]
        sql = (
            f"SELECT * FROM {table} "
            "WHERE report_date >= ? AND report_date <= ? "
            "ORDER BY secucode, report_date DESC"
        )
        return self.conn.execute(sql, (start_date, end_date)).fetchall()

    def get_all_secucodes_from_raw(self, table_suffix: str) -> list[str]:
        """获取某张原始表中的所有公司代码（去重）。"""
        table = RAW_TABLES[table_suffix]
        sql = f"SELECT DISTINCT secucode FROM {table}"
        return [r[0] for r in self.conn.execute(sql).fetchall()]

    # ------------------------------------------------------------------
    # 清洗数据写入与查询
    # ------------------------------------------------------------------
    def upsert_clean(self, table_suffix: str, record: dict) -> None:
        """upsert 单条清洗后数据。"""
        table = CLEAN_TABLES[table_suffix]
        table_cols = self._get_table_columns(table)
        cols = [c for c in record.keys() if c in table_cols]
        if not cols:
            return
        placeholders = ", ".join(f":{c}" for c in cols)
        col_list = ", ".join(cols)
        sql = f"INSERT OR REPLACE INTO {table} ({col_list}) VALUES ({placeholders})"
        filtered = {c: record.get(c) for c in cols}
        with self.conn:
            self.conn.execute(sql, filtered)

    def get_clean_by_company(
        self, table_suffix: str, secucode: str
    ) -> list[sqlite3.Row]:
        """按公司查询清洗后数据（全部报告期，按日期降序）。"""
        table = CLEAN_TABLES[table_suffix]
        sql = (
            f"SELECT * FROM {table} WHERE secucode = ? "
            "ORDER BY report_date DESC"
        )
        return self.conn.execute(sql, (secucode,)).fetchall()

    def get_clean_by_period(
        self, table_suffix: str, report_date: str
    ) -> list[sqlite3.Row]:
        """按报告期查询清洗后数据（全部公司）。"""
        table = CLEAN_TABLES[table_suffix]
        sql = f"SELECT * FROM {table} WHERE report_date = ?"
        return self.conn.execute(sql, (report_date,)).fetchall()

    def get_clean_record(
        self, table_suffix: str, secucode: str, report_date: str
    ) -> sqlite3.Row | None:
        """获取某公司某报告期的清洗后记录。"""
        table = CLEAN_TABLES[table_suffix]
        sql = f"SELECT * FROM {table} WHERE secucode = ? AND report_date = ?"
        return self.conn.execute(sql, (secucode, report_date)).fetchone()

    def get_all_clean_secucode_dates(
        self, table_suffix: str
    ) -> list[tuple[str, str]]:
        """获取某张清洗表的所有 (secucode, report_date) 组合。"""
        table = CLEAN_TABLES[table_suffix]
        sql = (
            f"SELECT secucode, report_date FROM {table} "
            "ORDER BY secucode, report_date DESC"
        )
        return [(r[0], r[1]) for r in self.conn.execute(sql).fetchall()]

    # ------------------------------------------------------------------
    # 特征写入与查询
    # ------------------------------------------------------------------
    def upsert_feature(
        self,
        secucode: str,
        report_date: str,
        feature_name: str,
        feature_value: float | None,
        feature_group: str,
        definition_version: str = "stage1-v1",
    ) -> None:
        """upsert 单条特征。

        stage1-v1: 新写入标 definition_version。
        同主键 legacy 行（definition_version IS NULL）默认拒写，保护旧结果。
        """
        # 检查同主键是否存在 legacy 行
        existing = self.conn.execute(
            "SELECT definition_version FROM features "
            "WHERE secucode = ? AND report_date = ? AND feature_name = ?",
            (secucode, report_date, feature_name),
        ).fetchone()
        if existing is not None and existing[0] is None and definition_version is not None:
            # legacy 行存在，拒绝覆盖
            from fin_feature.utils.logger import setup_logger
            logger = setup_logger("fin_feature.storage.repository")
            logger.warning(
                "拒绝覆盖 legacy 行: %s %s %s（请使用隔离新库）",
                secucode, report_date, feature_name,
            )
            return

        sql = (
            "INSERT OR REPLACE INTO features "
            "(secucode, report_date, feature_name, feature_value, feature_group, definition_version) "
            "VALUES (?, ?, ?, ?, ?, ?)"
        )
        with self.conn:
            self.conn.execute(sql, (secucode, report_date, feature_name, feature_value, feature_group, definition_version))

    def batch_upsert_features(self, records: Iterable[dict]) -> int:
        """批量 upsert 特征。

        records: [{secucode, report_date, feature_name, feature_value, feature_group, definition_version}]
        stage1-v1: 新写入标 definition_version；同主键 legacy 行默认拒写。
        """
        rows = list(records)
        if not rows:
            return 0

        # 检查同主键 legacy 行，过滤掉会覆盖 legacy 的记录
        from fin_feature.utils.logger import setup_logger
        logger = setup_logger("fin_feature.storage.repository")

        safe_rows = []
        rejected = 0
        for r in rows:
            existing = self.conn.execute(
                "SELECT definition_version FROM features "
                "WHERE secucode = ? AND report_date = ? AND feature_name = ?",
                (r["secucode"], r["report_date"], r["feature_name"]),
            ).fetchone()
            dv = r.get("definition_version")
            if existing is not None and existing[0] is None and dv is not None:
                rejected += 1
                continue
            safe_rows.append(r)

        if rejected:
            logger.warning(
                "拒绝覆盖 %d 条 legacy 行（请使用隔离新库）", rejected,
            )

        if not safe_rows:
            return 0

        sql = (
            "INSERT OR REPLACE INTO features "
            "(secucode, report_date, feature_name, feature_value, feature_group, definition_version) "
            "VALUES (:secucode, :report_date, :feature_name, :feature_value, :feature_group, :definition_version)"
        )
        with self.conn:
            self.conn.executemany(sql, safe_rows)
        return len(safe_rows)

    def get_features(
        self, secucode: str, report_date: str | None = None, feature_group: str | None = None,
        definition_version: str = "stage1-v1",
    ) -> list[sqlite3.Row]:
        """查询特征，可按报告期、特征组和定义版本过滤。

        stage1-v1: 默认只返回 stage1-v1 定义的特征，不混读 legacy 行。
        """
        sql = "SELECT * FROM features WHERE secucode = ?"
        params: list[Any] = [secucode]
        if report_date is not None:
            sql += " AND report_date = ?"
            params.append(report_date)
        if feature_group is not None:
            sql += " AND feature_group = ?"
            params.append(feature_group)
        if definition_version is not None:
            sql += " AND definition_version = ?"
            params.append(definition_version)
        sql += " ORDER BY report_date, feature_name"
        return self.conn.execute(sql, params).fetchall()

    def get_feature_series(
        self, feature_name: str, report_date: str,
        definition_version: str = "stage1-v1",
    ) -> list[sqlite3.Row]:
        """获取某特征在某报告期的全部公司取值（用于回测分组）。

        stage1-v1: 默认只返回 stage1-v1 定义的特征。
        """
        sql = (
            "SELECT secucode, feature_value FROM features "
            "WHERE feature_name = ? AND report_date = ? AND feature_value IS NOT NULL"
        )
        params: list[Any] = [feature_name, report_date]
        if definition_version is not None:
            sql += " AND definition_version = ?"
            params.append(definition_version)
        return self.conn.execute(sql, params).fetchall()

    # ------------------------------------------------------------------
    # 清洗日志
    # ------------------------------------------------------------------
    def log_clean_action(
        self,
        secucode: str,
        report_date: str,
        source_table: str,
        action: str,
        detail: str,
    ) -> None:
        """记录一条清洗日志。"""
        sql = (
            "INSERT INTO clean_log (secucode, report_date, source_table, action, detail) "
            "VALUES (?, ?, ?, ?, ?)"
        )
        with self.conn:
            self.conn.execute(sql, (secucode, report_date, source_table, action, detail))

    def get_clean_logs(self, limit: int = 100) -> list[sqlite3.Row]:
        """查询最近的清洗日志。"""
        sql = "SELECT * FROM clean_log ORDER BY id DESC LIMIT ?"
        return self.conn.execute(sql, (limit,)).fetchall()

    # ------------------------------------------------------------------
    # 增量水位
    # ------------------------------------------------------------------
    def get_watermark(self, source_name: str, secucode: str) -> str | None:
        """获取某数据源某公司的最后爬取水位（notice_date）。"""
        sql = "SELECT last_notice_date FROM crawl_watermark WHERE source_name = ? AND secucode = ?"
        row = self.conn.execute(sql, (source_name, secucode)).fetchone()
        return row[0] if row else None

    def update_watermark(self, source_name: str, secucode: str, last_notice_date: str) -> None:
        """更新增量水位。"""
        sql = (
            "INSERT OR REPLACE INTO crawl_watermark (source_name, secucode, last_notice_date) "
            "VALUES (?, ?, ?)"
        )
        with self.conn:
            self.conn.execute(sql, (source_name, secucode, last_notice_date))

    # ------------------------------------------------------------------
    # 质量报告
    # ------------------------------------------------------------------
    def save_quality_metric(
        self, table_name: str, metric_name: str, metric_value: float, detail: str = ""
    ) -> None:
        """保存一条质量指标。"""
        sql = (
            "INSERT INTO quality_report (table_name, metric_name, metric_value, detail) "
            "VALUES (?, ?, ?, ?)"
        )
        with self.conn:
            self.conn.execute(sql, (table_name, metric_name, metric_value, detail))

    def get_table_count(self, table_name: str) -> int:
        """获取某张表的总记录数。"""
        sql = f"SELECT COUNT(*) FROM {table_name}"
        return self.conn.execute(sql).fetchone()[0]
