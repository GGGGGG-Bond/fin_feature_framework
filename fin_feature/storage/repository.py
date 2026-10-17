"""数据访问层：原始数据写入、清洗数据写入、特征写入、按维度查询、增量水位、清洗日志。

所有写入使用 INSERT OR REPLACE（upsert），保证幂等。
"""

import sqlite3
from typing import Any, Iterable

from fin_feature.constants import CLEAN_TABLES, RAW_TABLES


class _UnitOfWork:
    """事务上下文管理器：成功提交，异常回滚。

    R1 修复：为 Repository 提供明确的事务所有权边界，使多个写入可组合为原子单元。
    A1 修复：嵌套时使用 savepoint，不提交/回滚外层事务。最外层显式开启事务，
    使嵌套的 unit_of_work 能通过 conn.in_transaction 检测到外层事务并使用 savepoint。
    """

    def __init__(self, conn: sqlite3.Connection, repository=None):
        self._conn = conn
        self._repository = repository
        self._savepoint: str | None = None

    def __enter__(self) -> "_UnitOfWork":
        if self._conn.in_transaction:
            # 嵌套：已有外层事务，使用 savepoint 管理自己的范围
            import uuid
            self._savepoint = f"uow_{uuid.uuid4().hex[:8]}"
            self._conn.execute(f"SAVEPOINT {self._savepoint}")
        else:
            # 最外层：显式开启事务，内层 unit_of_work 将检测到并使用 savepoint
            self._conn.execute("BEGIN")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._savepoint is not None:
            # 嵌套：只管理自己的 savepoint，不影响外层事务
            if exc_type is None:
                self._conn.execute(f"RELEASE SAVEPOINT {self._savepoint}")
            else:
                self._conn.execute(f"ROLLBACK TO SAVEPOINT {self._savepoint}")
                self._conn.execute(f"RELEASE SAVEPOINT {self._savepoint}")
        elif exc_type is None:
            # 最外层：提交失败时必须回滚本事务并保留原异常。否则连接会停留在
            # 未提交事务中，调用方可能继续读到尚未落盘的 success 状态。
            try:
                self._conn.commit()
            except BaseException:
                self._rollback_outer()
                raise
        else:
            # 最外层：回滚整个事务
            self._rollback_outer()

    def _rollback_outer(self) -> None:
        """回滚最外层事务；无法恢复连接状态时标记仓库，供调用方终止运行。"""
        try:
            self._conn.rollback()
        except Exception:
            if self._repository is not None:
                self._repository.transaction_broken = True


def utc_now_iso() -> str:
    """UTC ISO-8601（Z）时间戳：v4 采集证据的时间由应用给出，不用本地时间默认值。"""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_positive_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _selection_sort_key(record: dict) -> tuple:
    """按阶段一 deduplicate 口径规范化日期后取 sort_key。

    阶段一在分组前把 report_date/notice_date 规范化为 YYYY-MM-DD，再计算
    notice_date → entry_time → 内容摘要。兼容 raw 的同键覆盖必须用同一口径，
    否则仅调用同一 sort_key 仍会因报告期格式不同而选出不同行（2B-2 复审 R3）。
    """
    from fin_feature.cleaning.dedup import sort_key
    from fin_feature.utils.date_utils import normalize_date

    normalized = dict(record)
    normalized["report_date"] = normalize_date(record.get("report_date")) or ""
    normalized["notice_date"] = normalize_date(record.get("notice_date")) or ""
    return sort_key(normalized)


class Repository:
    """统一的数据访问层，封装所有表的 CRUD 与查询。

    R1 修复：所有写入方法不自行提交（移除 with self.conn），事务由调用方通过
    unit_of_work() 或显式 commit() 控制。这使多个写入可组合为原子单元。

    2B（v4）：raw_store 可选注入，用于在写入新证据时校验真实文件与哈希。
    未注入时仍可读写 legacy-unverified 审计记录，但不能声明证据充分。
    """

    def __init__(self, conn: sqlite3.Connection, raw_store=None):
        self.conn = conn
        self.raw_store = raw_store
        self._table_columns_cache: dict[str, set[str]] = {}
        # 2B-2 复审 R1：最外层提交失败且回滚也失败时置位；协调器据此终止运行，
        # 不再依据可能未提交的数据统计成功。
        self.transaction_broken = False

    def _get_table_columns(self, table: str) -> set[str]:
        """获取表的列名集合（带缓存）。"""
        if table not in self._table_columns_cache:
            rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
            self._table_columns_cache[table] = {r[1] for r in rows}
        return self._table_columns_cache[table]

    def unit_of_work(self):
        """事务上下文管理器：成功时提交，异常时回滚。

        R1 修复：提供明确的事务所有权边界。多个写入方法可在同一 unit_of_work
        中组合为原子单元，失败一起回滚。

        A1 修复：嵌套时使用 savepoint，不提交/回滚外层事务。最外层显式 BEGIN，
        内层检测到已有事务后使用独立 savepoint，内层成功只 RELEASE，失败只
        ROLLBACK TO 自己的 savepoint。

        用法：
            with repo.unit_of_work():
                repo.batch_upsert_raw(...)
                repo.update_watermark(...)
        """
        return _UnitOfWork(self.conn, self)

    def commit(self) -> None:
        """显式提交当前事务。"""
        self.conn.commit()

    def rollback(self) -> None:
        """显式回滚当前事务。"""
        self.conn.rollback()

    # ------------------------------------------------------------------
    # 原始数据写入
    # ------------------------------------------------------------------
    def upsert_stock_list(self, records: Iterable[dict]) -> int:
        """批量写入股票列表。R1 修复：不自行提交。"""
        rows = list(records)
        if not rows:
            return 0
        sql = (
            "INSERT OR REPLACE INTO stock_list "
            "(secucode, security_code, security_name, market) "
            "VALUES (:secucode, :security_code, :security_name, :market)"
        )
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
        self.conn.executemany(sql, filtered)
        return len(rows)

    def upsert_compat_raw(self, table_suffix: str, records: Iterable[dict]) -> int:
        """兼容 raw 写入：保留原计量，供阶段一 clean/features 继续消费。

        - notice_date 非空：同主键已存在时按阶段一选值口径比较；仅当新记录
          不早于既有行才覆盖。这样后续批次只返回同公告日的较旧 entry 时，
          兼容 raw 不会被降级（2B-2 复审 R2）。
        - notice_date 为空：SQLite 复合主键允许 NULL 重复，须按
          (secucode, report_date, 公告为空) 比较同一映射内容（排除 crawled_at），
          相同内容复用既有行，不同内容保留为新行；不因幂等删除既有历史。
        返回实际写入行数（未覆盖/同内容复用不计）。
        """
        rows = list(records)
        if not rows:
            return 0
        table = RAW_TABLES[table_suffix]
        table_cols = self._get_table_columns(table)
        written = 0
        for record in rows:
            cols = [c for c in record.keys() if c in table_cols and c != "crawled_at"]
            if not cols:
                continue
            incoming = {c: record.get(c) for c in cols}
            if incoming.get("notice_date") is None:
                existing = self.conn.execute(
                    f"SELECT * FROM {table} "
                    "WHERE secucode = ? AND report_date = ? AND notice_date IS NULL",
                    (incoming.get("secucode"), incoming.get("report_date")),
                ).fetchall()
                if any({c: row[c] for c in cols} == incoming for row in existing):
                    continue
            else:
                existing_row = self.conn.execute(
                    f"SELECT * FROM {table} "
                    "WHERE secucode = ? AND report_date = ? AND notice_date = ?",
                    (incoming.get("secucode"), incoming.get("report_date"),
                     incoming.get("notice_date")),
                ).fetchone()
                if existing_row is not None:
                    # Keep historical selection evidence even when the new response
                    # omits that field. Incoming columns describe only the new row.
                    existing = dict(existing_row)
                    if _selection_sort_key(existing) > _selection_sort_key(incoming):
                        # 后续批次只带回同公告日的较旧记录：保留既有当前行
                        continue
            placeholders = ", ".join(f":{c}" for c in cols)
            col_list = ", ".join(cols)
            self.conn.execute(
                f"INSERT OR REPLACE INTO {table} ({col_list}) VALUES ({placeholders})",
                incoming,
            )
            written += 1
        return written

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
        """upsert 单条清洗后数据。R1 修复：不自行提交。"""
        table = CLEAN_TABLES[table_suffix]
        table_cols = self._get_table_columns(table)
        cols = [c for c in record.keys() if c in table_cols]
        if not cols:
            return
        placeholders = ", ".join(f":{c}" for c in cols)
        col_list = ", ".join(cols)
        sql = f"INSERT OR REPLACE INTO {table} ({col_list}) VALUES ({placeholders})"
        filtered = {c: record.get(c) for c in cols}
        self.conn.execute(sql, filtered)

    def invalidate_clean(
        self, table_suffix: str, secucode: str, report_date: str
    ) -> None:
        """失效某公司某报告期的清洗记录。R1 修复：不自行提交。

        stage1-v1: 最新输入被 drop 时，同步失效旧 clean 数据，避免陈旧值残留。
        """
        table = CLEAN_TABLES[table_suffix]
        self.conn.execute(
            f"DELETE FROM {table} WHERE secucode = ? AND report_date = ?",
            (secucode, report_date),
        )

    def invalidate_features(self, secucode: str, report_date: str) -> None:
        """失效某公司某报告期的特征记录。R1 修复：不自行提交。

        stage1-v1: 仅失效当前定义版本（stage1-v1），保留 legacy 和其他版本。
        不能由 clean 的删除路径绕过写入端的 legacy 拒覆盖保护。
        """
        self.conn.execute(
            "DELETE FROM features "
            "WHERE secucode = ? AND report_date = ? "
            "AND definition_version = 'stage1-v1'",
            (secucode, report_date),
        )

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
        """记录一条清洗日志。R1 修复：不自行提交。"""
        sql = (
            "INSERT INTO clean_log (secucode, report_date, source_table, action, detail) "
            "VALUES (?, ?, ?, ?, ?)"
        )
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
        """更新增量水位。R1 修复：不自行提交，事务由外层控制。"""
        sql = (
            "INSERT OR REPLACE INTO crawl_watermark (source_name, secucode, last_notice_date) "
            "VALUES (?, ?, ?)"
        )
        self.conn.execute(sql, (source_name, secucode, last_notice_date))

    # ------------------------------------------------------------------
    # 质量报告
    # ------------------------------------------------------------------
    def save_quality_metric(
        self, table_name: str, metric_name: str, metric_value: float, detail: str = ""
    ) -> None:
        """保存一条质量指标。R1 修复：不自行提交。"""
        sql = (
            "INSERT INTO quality_report (table_name, metric_name, metric_value, detail) "
            "VALUES (?, ?, ?, ?)"
        )
        self.conn.execute(sql, (table_name, metric_name, metric_value, detail))

    def get_table_count(self, table_name: str) -> int:
        """获取某张表的总记录数。"""
        sql = f"SELECT COUNT(*) FROM {table_name}"
        return self.conn.execute(sql).fetchone()[0]

    # ------------------------------------------------------------------
    # 阶段 2 新增：采集批次 (ingest_run)
    # ------------------------------------------------------------------
    def create_ingest_run(
        self,
        source_name: str,
        request_scope: str | None = None,
        crawler_version: str | None = None,
        started_at: str | None = None,
    ) -> str:
        """创建采集批次，返回 run_id（UUID）。

        P1 #3 修复：不自行提交，事务由外层调用方控制（采集单元原子性）。
        2B-2：协调器传入实际 UTC 开始时间；未传时沿用历史默认值。
        """
        import uuid
        run_id = str(uuid.uuid4())
        sql = """
            INSERT INTO ingest_run
            (run_id, source_name, request_scope, crawler_version, status, started_at)
            VALUES (?, ?, ?, ?, 'running', COALESCE(?, datetime('now', 'localtime')))
        """
        self.conn.execute(
            sql, (run_id, source_name, request_scope, crawler_version, started_at)
        )
        return run_id

    def finish_ingest_run(
        self,
        run_id: str,
        status: str,
        failed_units: int = 0,
        detail: str | None = None,
    ) -> None:
        """更新采集批次状态。P1 #3 修复：不自行提交。"""
        sql = """
            UPDATE ingest_run
            SET status = ?, failed_units = ?, detail = ?, ended_at = datetime('now', 'localtime')
            WHERE run_id = ?
        """
        self.conn.execute(sql, (status, failed_units, detail, run_id))

    # ------------------------------------------------------------------
    # 2B（v4）：采集单元 (ingest_unit) 与 HTTP 尝试 (ingest_attempt)
    # ------------------------------------------------------------------
    def create_ingest_unit(
        self,
        run_id: str,
        unit_key: str,
        source_name: str,
        unit_kind: str,
        report_name: str,
        secucode: str,
        request_scope: str,
    ) -> str:
        """登记一个采集单元，返回 unit_id（UUID）。不自行提交。

        同一 run 内 (run_id, unit_key) 唯一：重复登记不新建单元。
        已提交完成的单元直接复用结果；running/failed 的同 run 重入明确拒绝
        （恢复留给新 run，不在旧 run 内续跑）。
        """
        import uuid

        from fin_feature.storage.hashing import is_valid_hash

        if unit_kind not in ("financial", "stock_list"):
            raise ValueError(f"非法 unit_kind：{unit_kind!r}")
        if not is_valid_hash(unit_key):
            raise ValueError(f"unit_key 必须为 64 位小写十六进制：{unit_key!r}")
        if unit_kind == "stock_list":
            if secucode != "" or report_name != "stock_list":
                raise ValueError("股票发现单元必须 secucode='' 且 report_name='stock_list'")
        else:
            if not secucode:
                raise ValueError("财务单元必须有 secucode")
            if report_name == "stock_list":
                raise ValueError("财务单元不能使用 report_name='stock_list'")
        if not isinstance(request_scope, str) or not request_scope:
            raise ValueError("request_scope 必须为非空 JSON 字符串（规范化的业务请求范围）")

        self._require_run_source(run_id, source_name)

        existing = self.get_unit_by_key(run_id, unit_key)
        if existing is not None:
            if existing["status"] in ("running", "failed"):
                raise ValueError(
                    f"同一 run 内 unit 重入被拒绝（status={existing['status']}）；"
                    "失败单元的恢复请新建 run"
                )
            return existing["unit_id"]

        unit_id = str(uuid.uuid4())
        self.conn.execute(
            "INSERT INTO ingest_unit "
            "(unit_id, run_id, unit_key, source_name, unit_kind, report_name, secucode, "
            " request_scope, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'planned')",
            (unit_id, run_id, unit_key, source_name, unit_kind, report_name, secucode,
             request_scope),
        )
        return unit_id

    def mark_unit_running(self, unit_id: str, started_at: str | None = None) -> str:
        """planned → running。已完成单元不就地重跑。"""
        row = self._require_unit(unit_id)
        if row["status"] != "planned":
            raise ValueError(
                f"仅 planned 单元可开始（当前 {row['status']}）：不能重新开始或改写已完成单元"
            )
        started_at = started_at or utc_now_iso()
        self.conn.execute(
            "UPDATE ingest_unit SET status = 'running', started_at = ? WHERE unit_id = ?",
            (started_at, unit_id),
        )
        return started_at

    def finish_unit(
        self,
        unit_id: str,
        status: str,
        *,
        source_rows: int = 0,
        selected_rows: int = 0,
        new_versions: int = 0,
        new_observations: int = 0,
        compat_writes: int = 0,
        failure_code: str | None = None,
        detail: str | None = None,
        ended_at: str | None = None,
    ) -> None:
        """running → success / success_empty / failed；计数与状态随业务事务提交。"""
        if status not in ("success", "success_empty", "failed"):
            raise ValueError(f"非法单元终态：{status!r}")
        for label, value in (
            ("source_rows", source_rows), ("selected_rows", selected_rows),
            ("new_versions", new_versions), ("new_observations", new_observations),
            ("compat_writes", compat_writes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{label} 必须为非负整数，当前 {value!r}")
        row = self._require_unit(unit_id)
        if row["status"] != "running":
            raise ValueError(
                f"仅 running 单元可结算（当前 {row['status']}）：不能把成功单元改为 failed 覆盖历史"
            )
        self.conn.execute(
            "UPDATE ingest_unit SET status = ?, ended_at = ?, failure_code = ?, detail = ?, "
            "source_rows = ?, selected_rows = ?, new_versions = ?, new_observations = ?, "
            "compat_writes = ? WHERE unit_id = ?",
            (status, ended_at or utc_now_iso(), failure_code, detail,
             source_rows, selected_rows, new_versions, new_observations, compat_writes,
             unit_id),
        )

    def get_unit(self, unit_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM ingest_unit WHERE unit_id = ?", (unit_id,)
        ).fetchone()

    def get_unit_by_key(self, run_id: str, unit_key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM ingest_unit WHERE run_id = ? AND unit_key = ?",
            (run_id, unit_key),
        ).fetchone()

    def get_units_for_run(
        self, run_id: str, status: str | None = None
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM ingest_unit WHERE run_id = ?"
        params: list[Any] = [run_id]
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY unit_kind, report_name, secucode"
        return self.conn.execute(sql, params).fetchall()

    def count_units(
        self, run_id: str, unit_kinds: tuple[str, ...] | None = None
    ) -> dict[str, int]:
        """按状态统计单元数。unit_kinds 用于把终态汇总限定在财务单元（2B-3 接线）。"""
        sql = "SELECT status, COUNT(*) FROM ingest_unit WHERE run_id = ?"
        params: list[Any] = [run_id]
        if unit_kinds:
            placeholders = ", ".join("?" for _ in unit_kinds)
            sql += f" AND unit_kind IN ({placeholders})"
            params.extend(unit_kinds)
        sql += " GROUP BY status"
        return {row[0]: row[1] for row in self.conn.execute(sql, params).fetchall()}

    def finalize_run(
        self,
        run_id: str,
        *,
        unit_kinds: tuple[str, ...] | None = None,
        detail: str | None = None,
        ended_at: str | None = None,
    ) -> dict:
        """从已持久化的单元状态汇总 run 终态并一次性提交（不自行提交事务）。

        规则（契约第 7 节）：
        - 0 单元 → failed / no_units（不编造财务失败单元）
        - 全部单元 success/success_empty → success
        - 存在成功单元且另有 failed/未完成 → partial_failure
        - 无任何成功单元（全部 failed 或未完成）→ failed
        未完成的 planned/running 单元保留原状态，不补造 success，也不改水位。
        """
        counts = self.count_units(run_id, unit_kinds)
        total = sum(counts.values())
        succeeded = counts.get("success", 0) + counts.get("success_empty", 0)
        failed = counts.get("failed", 0)
        incomplete = counts.get("planned", 0) + counts.get("running", 0)

        if total == 0:
            status = "failed"
            run_detail = "no_units"
            failed_units = 0
        else:
            failed_units = failed + incomplete
            if failed or incomplete:
                status = "partial_failure" if succeeded else "failed"
            else:
                status = "success"
            run_detail = detail or (
                f"units={total}, success={succeeded}, failed={failed}, incomplete={incomplete}"
            )

        self.conn.execute(
            "UPDATE ingest_run SET status = ?, failed_units = ?, ended_at = ?, detail = ? "
            "WHERE run_id = ?",
            (status, failed_units, ended_at or utc_now_iso(), run_detail, run_id),
        )
        return {
            "run_id": run_id,
            "status": status,
            "failed_units": failed_units,
            "total": total,
            "success": succeeded,
            "failed": failed,
            "incomplete": incomplete,
        }

    def register_attempt(
        self, unit_id: str, page_number: int, http_attempt: int,
        started_at: str | None = None,
    ) -> str:
        """登记一次实际 HTTP 调用（每页从 1 开始计数，含重试），返回 attempt_id。

        身份唯一键 (unit_id, page_number, http_attempt)：重复登记返回既有
        attempt_id；已完成的 attempt 明确拒绝重跑（不覆盖历史观察）。
        """
        import uuid

        self._require_unit(unit_id)
        if not _is_positive_int(page_number):
            raise ValueError(f"page_number 必须为 >=1 的整数，当前 {page_number!r}")
        if not _is_positive_int(http_attempt):
            raise ValueError(f"http_attempt 必须为 >=1 的整数，当前 {http_attempt!r}")

        existing = self.conn.execute(
            "SELECT attempt_id, outcome FROM ingest_attempt "
            "WHERE unit_id = ? AND page_number = ? AND http_attempt = ?",
            (unit_id, page_number, http_attempt),
        ).fetchone()
        if existing is not None:
            if existing["outcome"] != "running":
                raise ValueError(
                    f"attempt 已完成（outcome={existing['outcome']}）：拒绝重跑同一尝试"
                )
            return existing["attempt_id"]

        attempt_id = str(uuid.uuid4())
        self.conn.execute(
            "INSERT INTO ingest_attempt "
            "(attempt_id, unit_id, page_number, http_attempt, started_at, outcome) "
            "VALUES (?, ?, ?, ?, ?, 'running')",
            (attempt_id, unit_id, page_number, http_attempt,
             started_at or utc_now_iso()),
        )
        return attempt_id

    def finish_attempt(
        self,
        attempt_id: str,
        outcome: str,
        error_code: str | None = None,
        detail: str | None = None,
        ended_at: str | None = None,
    ) -> None:
        """结算一次尝试：ok / http_error / business_error / decode_error /
        schema_error / transport_error / storage_error。"""
        allowed = {
            "ok", "http_error", "business_error", "decode_error",
            "schema_error", "transport_error", "storage_error",
        }
        if outcome not in allowed:
            raise ValueError(f"非法 attempt outcome：{outcome!r}")
        row = self.conn.execute(
            "SELECT outcome FROM ingest_attempt WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"attempt_id={attempt_id} 不存在")
        if row["outcome"] != "running":
            raise ValueError(f"attempt 已结算（{row['outcome']}），不接受重复结算")
        self.conn.execute(
            "UPDATE ingest_attempt SET outcome = ?, error_code = ?, detail = ?, ended_at = ? "
            "WHERE attempt_id = ?",
            (outcome, error_code, detail, ended_at or utc_now_iso(), attempt_id),
        )

    def get_attempt(self, attempt_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM ingest_attempt WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()

    def get_attempts_for_unit(self, unit_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM ingest_attempt WHERE unit_id = ? "
            "ORDER BY page_number, http_attempt",
            (unit_id,),
        ).fetchall()

    def _require_unit(self, unit_id: str) -> sqlite3.Row:
        row = self.get_unit(unit_id)
        if row is None:
            raise ValueError(f"unit_id={unit_id} 不存在于 ingest_unit 表")
        return row

    def _require_run_source(self, run_id: str, source_name: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM ingest_run WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"run_id={run_id} 不存在于 ingest_run 表")
        if row["source_name"] != source_name:
            raise ValueError(
                f"来源不一致：ingest_run.source_name={row['source_name']}，请求={source_name}"
            )
        return row

    # ------------------------------------------------------------------
    # 阶段 2 新增：原始响应留存 (raw_snapshot)
    # ------------------------------------------------------------------
    def save_raw_snapshot(
        self,
        run_id: str,
        source_name: str,
        report_name: str,
        secucode: str | None,
        page_number: int | None,
        request_params: str,
        raw_content_hash: str,
        file_path: str,
        record_count: int | None,
        *,
        attempt_id: str | None = None,
        raw_format: str | None = None,
        http_status: int | None = None,
        content_type: str | None = None,
        text_encoding: str | None = None,
        raw_bytes: int | None = None,
        validation_status: str | None = None,
        fetched_at: str | None = None,
    ) -> str:
        """保存原始响应记录，返回 snapshot_id（UUID）。P1 #3 修复：不自行提交。

        两条写入路径（契约 docs/stage-2/2b-schema.md）：

        1. 新证据路径（attempt_id 非空）：必须同时给出 raw_format=http-body-v1、
           100..599 的 http_status、raw_bytes、真实 fetched_at、合法 SHA-256 与
           相对路径；并调用 raw_store 核对文件当前存在且哈希/大小一致。
           unit / run / source / report / company / page 逐项与本快照一致。
        2. legacy 审计路径（attempt_id 为空）：保持旧调用兼容，落
           legacy-unverified / legacy_unverified；它不能作为 v4 充分证据。

        同一 attempt 重登记：核心字段与内容一致时返回既有 snapshot_id；
        任一不同报冲突，不更新原证据，也不为同一尝试另插一行。
        """
        import uuid

        from fin_feature.storage.hashing import is_valid_hash
        from fin_feature.storage.raw_store import (
            RAW_FORMAT_HTTP_BODY,
            RAW_FORMAT_LEGACY,
        )

        # A4：校验 run 存在且来源一致
        run_row = self.conn.execute(
            "SELECT source_name FROM ingest_run WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if run_row is None:
            raise ValueError(f"run_id={run_id} 不存在于 ingest_run 表")
        if run_row[0] != source_name:
            raise ValueError(
                f"来源不一致：ingest_run.source_name={run_row[0]}，snapshot source_name={source_name}"
            )

        if raw_format is None:
            raw_format = RAW_FORMAT_HTTP_BODY if attempt_id is not None else RAW_FORMAT_LEGACY

        if attempt_id is None:
            # legacy 审计路径：不能伪装成 http-body-v1
            if raw_format != RAW_FORMAT_LEGACY:
                raise ValueError(
                    "raw_format=http-body-v1 必须给出 attempt_id（否则无身份、无校验依据）"
                )
            if validation_status is None:
                validation_status = "legacy_unverified"
            if validation_status != "legacy_unverified":
                raise ValueError("无 attempt_id 的快照只能标 legacy_unverified")
        else:
            if raw_format != RAW_FORMAT_HTTP_BODY:
                raise ValueError(
                    f"带 attempt_id 的新证据必须 raw_format={RAW_FORMAT_HTTP_BODY}，"
                    f"当前 {raw_format!r}（legacy 引用不能声明为 v4 证据）"
                )
            self._validate_v4_snapshot_fields(
                attempt_id=attempt_id,
                run_id=run_id,
                source_name=source_name,
                report_name=report_name,
                secucode=secucode,
                page_number=page_number,
                raw_content_hash=raw_content_hash,
                file_path=file_path,
                raw_bytes=raw_bytes,
                http_status=http_status,
                fetched_at=fetched_at,
            )
            # 新写入必须核对真实文件与哈希，不接受调用方自述的摘要
            self._verify_evidence_file(file_path, raw_content_hash, raw_bytes)
            if validation_status is None:
                validation_status = "pending"
            elif validation_status not in ("pending", "valid", "invalid"):
                raise ValueError(f"非法 validation_status：{validation_status!r}")

        if validation_status == "valid":
            if record_count is None or not isinstance(record_count, int) or isinstance(record_count, bool) \
                    or record_count < 0:
                raise ValueError("valid 页必须给出非负整数 record_count")

        # 同一 attempt 至多一个 snapshot
        if attempt_id is not None:
            existing = self.conn.execute(
                "SELECT * FROM raw_snapshot WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if existing is not None:
                return self._reuse_snapshot(
                    existing,
                    run_id=run_id,
                    source_name=source_name,
                    report_name=report_name,
                    secucode=secucode,
                    page_number=page_number,
                    request_params=request_params,
                    raw_content_hash=raw_content_hash,
                    file_path=file_path,
                    raw_bytes=raw_bytes,
                )

        if not is_valid_hash(raw_content_hash):
            raise ValueError(f"raw_content_hash 必须为 64 位小写十六进制：{raw_content_hash!r}")

        snapshot_id = str(uuid.uuid4())
        sql = """
            INSERT INTO raw_snapshot
            (snapshot_id, run_id, source_name, report_name, secucode, page_number,
             request_params, raw_content_hash, file_path, record_count,
             attempt_id, raw_format, http_status, content_type, text_encoding,
             raw_bytes, validation_status, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        self.conn.execute(
            sql,
            (snapshot_id, run_id, source_name, report_name, secucode, page_number,
             request_params, raw_content_hash, file_path, record_count,
             attempt_id, raw_format, http_status, content_type, text_encoding,
             raw_bytes, validation_status, fetched_at),
        )
        return snapshot_id

    def _reuse_snapshot(self, existing, **incoming) -> str:
        """同一 attempt 重登记：核心字段与内容一致则复用，否则报冲突。"""
        from fin_feature.ingestion.evidence import CaptureConflict

        same = (
            existing["run_id"] == incoming["run_id"]
            and existing["source_name"] == incoming["source_name"]
            and existing["report_name"] == incoming["report_name"]
            and existing["secucode"] == incoming["secucode"]
            and existing["page_number"] == incoming["page_number"]
            and existing["request_params"] == incoming["request_params"]
            and existing["raw_content_hash"] == incoming["raw_content_hash"]
            and existing["file_path"] == incoming["file_path"]
            and existing["raw_bytes"] == incoming["raw_bytes"]
        )
        if not same:
            raise CaptureConflict(
                f"attempt {existing['attempt_id']} 已有快照 {existing['snapshot_id']} 且核心字段/内容不同："
                "拒绝更新原证据，也不为同一尝试另插一行"
            )
        return existing["snapshot_id"]

    def _validate_v4_snapshot_fields(
        self, *, attempt_id, run_id, source_name, report_name, secucode, page_number,
        raw_content_hash, file_path, raw_bytes, http_status, fetched_at,
    ) -> None:
        """校验 v4 快照的必填元信息与 unit/run/source/report/company 逐项一致。"""
        from fin_feature.storage.hashing import is_valid_hash

        attempt = self.conn.execute(
            "SELECT * FROM ingest_attempt WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        if attempt is None:
            raise ValueError(f"attempt_id={attempt_id} 不存在于 ingest_attempt 表")

        unit = self._require_unit(attempt["unit_id"])
        if unit["run_id"] != run_id:
            raise ValueError(
                f"采集批次不一致：unit.run_id={unit['run_id']}，snapshot.run_id={run_id}"
            )
        if unit["source_name"] != source_name:
            raise ValueError(
                f"来源不一致：unit.source_name={unit['source_name']}，snapshot={source_name}"
            )
        if unit["report_name"] != report_name:
            raise ValueError(
                f"报表不一致：unit.report_name={unit['report_name']}，snapshot={report_name}"
            )
        if unit["unit_kind"] == "stock_list":
            if secucode is not None:
                raise ValueError("股票发现单元的 snapshot.secucode 必须为 NULL")
        elif unit["secucode"] != secucode:
            raise ValueError(
                f"公司不一致：unit.secucode={unit['secucode']}，snapshot={secucode}"
            )
        if page_number is None or not _is_positive_int(page_number):
            raise ValueError(f"v4 快照必须给出 >=1 的 page_number，当前 {page_number!r}")
        if page_number != attempt["page_number"]:
            raise ValueError(
                f"页号不一致：attempt.page_number={attempt['page_number']}，snapshot={page_number}"
            )
        if http_status is None or isinstance(http_status, bool) or not isinstance(http_status, int) \
                or not (100 <= http_status <= 599):
            raise ValueError(f"http_status 必须为 100..599 的整数，当前 {http_status!r}")
        if not isinstance(raw_bytes, int) or isinstance(raw_bytes, bool) or raw_bytes < 0:
            raise ValueError(f"raw_bytes 必须为非负整数，当前 {raw_bytes!r}")
        if not isinstance(fetched_at, str) or not fetched_at:
            raise ValueError("v4 快照必须给出真实 fetched_at（UTC ISO-8601）")
        if not is_valid_hash(raw_content_hash):
            raise ValueError(f"raw_content_hash 必须为 64 位小写十六进制：{raw_content_hash!r}")
        if not isinstance(file_path, str) or file_path.startswith("/") or ".." in file_path.split("/"):
            raise ValueError(f"file_path 必须是 raw_root 下的相对路径：{file_path!r}")

    def _verify_evidence_file(self, file_path: str, raw_content_hash: str, raw_bytes: int) -> None:
        """调用 raw_store 核对文件当前存在、可解压且哈希/大小一致。"""
        if self.raw_store is None:
            raise ValueError(
                "写入 v4 证据需要注入 raw_store（缺 verifier 时不能声明新证据充分）"
            )
        self.raw_store.verify({
            "file_path": file_path, "raw_hash": raw_content_hash, "raw_bytes": raw_bytes,
        })

    def set_snapshot_validation(
        self, snapshot_id: str, validation_status: str, record_count: int | None = None
    ) -> None:
        """pending 校验一次完成为 valid/invalid；不允许从 invalid 翻为 valid。"""
        row = self.conn.execute(
            "SELECT validation_status, raw_format FROM raw_snapshot WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"snapshot_id={snapshot_id} 不存在")
        if row["raw_format"] != "http-body-v1":
            raise ValueError("legacy-unverified 快照不能改判为 v4 已核验结论")
        if row["validation_status"] != "pending":
            raise ValueError(
                f"仅 pending 快照可改判（当前 {row['validation_status']}），不允许从 invalid 翻为 valid"
            )
        if validation_status not in ("valid", "invalid"):
            raise ValueError("validation_status 只能为 valid / invalid")
        if validation_status == "valid" and (
            record_count is None or not isinstance(record_count, int)
            or isinstance(record_count, bool) or record_count < 0
        ):
            raise ValueError("valid 页必须给出非负整数 record_count")
        self.conn.execute(
            "UPDATE raw_snapshot SET validation_status = ?, record_count = ? WHERE snapshot_id = ?",
            (validation_status, record_count, snapshot_id),
        )

    def get_snapshot(self, snapshot_id: str) -> sqlite3.Row | None:
        """按 snapshot_id 读取快照。"""
        return self.conn.execute(
            "SELECT * FROM raw_snapshot WHERE snapshot_id = ?", (snapshot_id,)
        ).fetchone()

    def get_snapshot_for_attempt(self, attempt_id: str) -> sqlite3.Row | None:
        """按 attempt 读取其唯一快照（无响应 attempt 返回 None）。"""
        return self.conn.execute(
            "SELECT * FROM raw_snapshot WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()

    # ------------------------------------------------------------------
    # 阶段 2 新增：标准化版本与血缘 (statement_version)
    # ------------------------------------------------------------------
    def create_statement_version(
        self,
        secucode: str,
        report_date: str,
        table_type: str,
        source_name: str = "eastmoney",
        snapshot_id: str | None = None,
        run_id: str | None = None,
        notice_date: str | None = None,
        revision_notice_date: str | None = None,
        revision_type: str = "unknown",
        metric_hash: str | None = None,
        semantic_hash: str | None = None,
        consolidate_scope: str = "unknown",
        period_basis: str = "unknown",
        currency: str = "unknown",
        amount_unit: str = "unknown",
        industry_type: str = "unknown",
        standardized_fields: str | None = None,
        raw_fields: str | None = None,
        evidence_sufficient: int | None = None,
        row_index: int | None = None,
        entry_time: str | None = None,
        normalization_diagnostics: str | None = None,
        observed_at: str | None = None,
        unit_evidence=None,
        defer_projection: bool = False,
    ) -> str:
        """创建标准化版本记录，处理 observation_seq 自增和旧版本替代。

        R3 修复：同内容重复观察不制造新版本，但通过 statement_observation 表保存
        每次观察与 snapshot 的关联，与版本投影同事务写入。
        R5 修复：evidence_sufficient=1 时校验 snapshot 的来源/公司/报表与版本一致。
        R6 修复：哈希由 Repository 内部根据 standardized_fields 和语义维度统一计算，
        不接受调用方任意提供的哈希值，确保哈希与负载一致。

        2B（v4）：
        - 有 snapshot 而未显式传 run_id 时，导出 snapshot.run_id（不保存未知批次）
        - evidence_sufficient=1 只用于 http-body-v1、文件校验通过、valid 页、
          分页/行定位一致且单元完整性有据的输入；legacy-unverified 引用不能声明充分
        - 新观察持久化 snapshot_id + row_index，并在修改当前投影之前做身份幂等预检
        - H1：observed_at 由调用方传入该响应真实接收时间（UTC）；首次与重复观察
          都使用同一实际接收时间，不再落到数据库本地提交时间默认值
        - defer_projection=True 时只写版本/观察，不在此处翻转当前投影；协调器在
          同一业务事务内按阶段一选值规则统一重建（避免较旧公告后到反而覆盖新版）

        返回 version_id（UUID）。
        """
        import json
        import uuid

        from fin_feature.storage.hashing import (
            compute_metric_hash, compute_semantic_hash,
            normalize_numeric, canonical_json,
        )

        # R6 修复：内部计算哈希，不接受调用方提供的哈希值
        # standardized_fields 必须提供（JSON 字符串）
        if standardized_fields is None:
            raise ValueError("standardized_fields 必须提供（用于计算哈希）")
        try:
            std_dict = json.loads(standardized_fields)
        except (json.JSONDecodeError, TypeError) as e:
            raise ValueError(f"standardized_fields 不是有效 JSON：{e}") from e

        # A6 修复：统一数值表示后重新序列化存储，确保存储与哈希一致
        std_dict = {k: v for k, v in std_dict.items() if v is not None}
        std_dict = normalize_numeric(std_dict)
        standardized_fields = canonical_json(std_dict)

        metric_hash = compute_metric_hash(std_dict)
        semantic_hash = compute_semantic_hash(
            std_dict,
            source_name=source_name,
            currency=currency,
            consolidate_scope=consolidate_scope,
            period_basis=period_basis,
            amount_unit=amount_unit,
            industry_type=industry_type,
        )

        # R5 修复：证据充分性校验
        # 2B（v4）：默认只有在 v4 已校验页证据 + 单元完整性证明齐备时才是 1；
        # legacy-unverified 引用（旧库/旧测试）不能默认取得充分性。
        if row_index is not None and (
            isinstance(row_index, bool) or not isinstance(row_index, int) or row_index < 0
        ):
            raise ValueError(f"row_index 必须为非负整数或 None，当前 {row_index!r}")

        snapshot = self.get_snapshot(snapshot_id) if snapshot_id is not None else None

        # 2B（v4）：有快照但未显式给出 run_id 时导出快照所属批次
        if snapshot is not None and run_id is None:
            run_id = snapshot["run_id"]

        v4_page_evidence = (
            snapshot is not None
            and snapshot["raw_format"] == "http-body-v1"
            and snapshot["validation_status"] == "valid"
        )
        if evidence_sufficient is None:
            evidence_sufficient = 1 if (v4_page_evidence and unit_evidence is not None) else 0
        if evidence_sufficient not in (0, 1):
            raise ValueError(f"evidence_sufficient 只能为 0/1，当前 {evidence_sufficient!r}")

        # R5/A4：有快照时校验来源/公司/报表/批次一致
        if snapshot_id is not None:
            self._verify_snapshot_consistency(
                snapshot_id, source_name, secucode, table_type, run_id
            )
        elif evidence_sufficient == 1:
            raise ValueError("evidence_sufficient=1 时必须提供有效 snapshot_id")

        if evidence_sufficient == 1:
            self._verify_new_evidence(
                snapshot=snapshot,
                unit_evidence=unit_evidence,
                snapshot_id=snapshot_id,
                row_index=row_index,
                secucode=secucode,
                table_type=table_type,
                run_id=run_id,
                source_name=source_name,
                standardized_dict=std_dict,
                raw_fields_json=raw_fields,
            )

        # 2B（v4）：来源行身份幂等预检，必须在修改当前投影之前
        if snapshot_id is not None and row_index is not None:
            replayed = self._precheck_observation_identity(
                snapshot_id=snapshot_id,
                row_index=row_index,
                metric_hash=metric_hash,
                semantic_hash=semantic_hash,
                notice_date=notice_date,
                revision_notice_date=revision_notice_date,
                revision_type=revision_type,
                entry_time=entry_time,
                normalization_diagnostics=normalization_diagnostics,
                report_date=report_date,
                table_type=table_type,
                source_name=source_name,
                secucode=secucode,
            )
            if replayed is not None:
                return replayed

        # R2/R3 修复：检查是否已存在相同 metric_hash + semantic_hash 的版本（同内容幂等）
        existing = self.conn.execute("""
            SELECT version_id, is_superseded FROM statement_version
            WHERE secucode = ? AND report_date = ? AND table_type = ? AND source_name = ?
              AND metric_hash = ? AND semantic_hash = ?
            ORDER BY observation_seq DESC LIMIT 1
        """, (secucode, report_date, table_type, source_name, metric_hash, semantic_hash)).fetchone()

        if existing is not None:
            existing_id = existing[0]
            # R2：恢复当前状态（is_superseded=0），并将同组其余版本标记为已替代
            # 2B-2：defer_projection 时由协调器在同一业务事务内统一按选值规则重建
            if existing[1] == 1 and not defer_projection:
                self.conn.execute("""
                    UPDATE statement_version
                    SET is_superseded = 0, superseded_by = NULL
                    WHERE version_id = ?
                """, (existing_id,))
                # 同组内其余版本（含中间的 B）标记为已替代，指向恢复的 A
                self.conn.execute("""
                    UPDATE statement_version
                    SET is_superseded = 1, superseded_by = ?
                    WHERE secucode = ? AND report_date = ? AND table_type = ? AND source_name = ?
                      AND version_id != ?
                """, (existing_id, secucode, report_date, table_type, source_name, existing_id))
            # R3：同内容重复观察不制造新版本，但保存本次观察与 snapshot 的关联
            # A3：保存本次观察的公告/修订元信息
            # H1：observed_at = 本次响应真实接收时间（UTC），不是提交时间
            self._record_observation(
                existing_id, snapshot_id, run_id,
                notice_date=notice_date,
                revision_notice_date=revision_notice_date,
                revision_type=revision_type,
                row_index=row_index,
                entry_time=entry_time,
                normalization_diagnostics=normalization_diagnostics,
                observed_at=observed_at,
            )
            return existing_id

        # 获取下一 observation_seq
        cur = self.conn.execute("""
            SELECT COALESCE(MAX(observation_seq), 0) + 1
            FROM statement_version
            WHERE secucode = ? AND report_date = ? AND table_type = ? AND source_name = ?
        """, (secucode, report_date, table_type, source_name))
        next_seq = cur.fetchone()[0]

        version_id = str(uuid.uuid4())

        sql = """
            INSERT INTO statement_version
            (version_id, snapshot_id, run_id, secucode, report_date, table_type, source_name,
             notice_date, revision_notice_date, revision_type, observed_at,
             metric_hash, semantic_hash,
             observation_seq, consolidate_scope, period_basis, currency, amount_unit,
             industry_type, standardized_fields, raw_fields, evidence_sufficient)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now', 'localtime')),
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        self.conn.execute(
            sql,
            (version_id, snapshot_id, run_id, secucode, report_date, table_type, source_name,
             notice_date, revision_notice_date, revision_type, observed_at,
             metric_hash, semantic_hash,
             next_seq, consolidate_scope, period_basis, currency, amount_unit,
             industry_type, standardized_fields, raw_fields, evidence_sufficient),
        )

        # R3：记录首次观察与 snapshot 的关联
        # A3：保存本次观察的公告/修订元信息
        # H1：首次观察同样使用本次响应真实接收时间（UTC）
        self._record_observation(
            version_id, snapshot_id, run_id,
            notice_date=notice_date,
            revision_notice_date=revision_notice_date,
            revision_type=revision_type,
            row_index=row_index,
            entry_time=entry_time,
            normalization_diagnostics=normalization_diagnostics,
            observed_at=observed_at,
        )

        # 标记旧版本为替代（同分组内仅最新为 0）
        # 2B-2：defer_projection 时由协调器统一重建当前投影
        if not defer_projection:
            self.conn.execute("""
                UPDATE statement_version
                SET is_superseded = 1, superseded_by = ?
                WHERE secucode = ? AND report_date = ? AND table_type = ? AND source_name = ?
                  AND version_id != ?
            """, (version_id, secucode, report_date, table_type, source_name, version_id))

        return version_id

    def _verify_snapshot_consistency(
        self, snapshot_id: str, source_name: str, secucode: str, table_type: str,
        run_id: str | None = None,
    ) -> None:
        """R5 修复：校验 snapshot 的来源/公司/报表与版本一致。

        A4 修复：同时校验 run 一致性——快照的 run_id 与版本的 run_id 必须相同，
        避免版本和观察归属错误的采集批次。

        snapshot 的 secucode 可为空（股票列表请求无公司），此时不校验公司。
        report_name 与 table_type 的映射通过 REPORT_TO_TABLE_TYPE 完成。
        """
        from fin_feature.constants import REPORT_FINANCIAL_INDICATOR, REPORT_INCOME_STATEMENT, REPORT_BALANCE_SHEET

        # table_type -> report_name 映射
        TABLE_TYPE_TO_REPORT = {
            "financial_indicator": REPORT_FINANCIAL_INDICATOR,
            "income_statement": REPORT_INCOME_STATEMENT,
            "balance_sheet": REPORT_BALANCE_SHEET,
        }
        expected_report = TABLE_TYPE_TO_REPORT.get(table_type)

        row = self.conn.execute(
            "SELECT source_name, secucode, report_name, run_id FROM raw_snapshot WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"snapshot_id={snapshot_id} 不存在于 raw_snapshot 表")

        snap_source = row[0]
        snap_secucode = row[1]
        snap_report = row[2]
        snap_run = row[3]

        if snap_source != source_name:
            raise ValueError(
                f"snapshot 来源不一致：snapshot.source_name={snap_source}，版本 source_name={source_name}"
            )
        # secucode 可为空（股票列表请求无公司），非空时必须一致
        if snap_secucode is not None and snap_secucode != secucode:
            raise ValueError(
                f"snapshot 公司不一致：snapshot.secucode={snap_secucode}，版本 secucode={secucode}"
            )
        if expected_report is not None and snap_report != expected_report:
            raise ValueError(
                f"snapshot 报表不一致：snapshot.report_name={snap_report}，版本 table_type={table_type}（期望 {expected_report}）"
            )
        # A4：run 一致性校验——两者非空时必须相同
        if run_id is not None and snap_run is not None and snap_run != run_id:
            raise ValueError(
                f"snapshot 采集批次不一致：snapshot.run_id={snap_run}，版本 run_id={run_id}"
            )

    def _record_observation(
        self, version_id: str, snapshot_id: str | None, run_id: str | None,
        notice_date: str | None = None,
        revision_notice_date: str | None = None,
        revision_type: str = "unknown",
        row_index: int | None = None,
        entry_time: str | None = None,
        normalization_diagnostics: str | None = None,
        observed_at: str | None = None,
    ) -> str | None:
        """R3 修复：在 statement_observation 表记录本次观察与 snapshot 的关联。

        A3 修复：保存本次观察的公告/修订元信息，不覆盖版本行的首次记录。
        2B（v4）：保存来源行定位 (snapshot_id, row_index)、entry_time 与标准化诊断；
        observed_at 取该响应真实接收时间而非数据库提交时间。
        snapshot_id 为 None 时不记录观察（旧数据迁移无原始响应）。

        返回 observation_id；未记录时返回 None。
        """
        if snapshot_id is None:
            return None
        import uuid
        observation_id = str(uuid.uuid4())
        self.conn.execute("""
            INSERT INTO statement_observation
            (observation_id, version_id, snapshot_id, run_id,
             notice_date, revision_notice_date, revision_type,
             row_index, entry_time, normalization_diagnostics, observed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now', 'localtime')))
        """, (observation_id, version_id, snapshot_id, run_id,
              notice_date, revision_notice_date, revision_type,
              row_index, entry_time, normalization_diagnostics, observed_at))
        return observation_id

    def _precheck_observation_identity(
        self, *, snapshot_id: str, row_index: int, metric_hash: str, semantic_hash: str,
        notice_date, revision_notice_date, revision_type, entry_time,
        normalization_diagnostics, report_date, table_type, source_name, secucode,
    ) -> str | None:
        """来源行身份幂等预检：同 (snapshot_id, row_index) 重处理。

        - 同一身份、同一版本分组（report_date/table_type/source_name/secucode）、
          同一内容版本与同一元信息/诊断 → 幂等，返回既有 version_id
        - 任一不同 → CaptureConflict，不静默改旧观察，也不部分修改当前投影
        预检发生在当前投影更新之前（契约第 6 节）。
        """
        from fin_feature.ingestion.evidence import CaptureConflict

        existing = self.conn.execute(
            "SELECT * FROM statement_observation WHERE snapshot_id = ? AND row_index = ?",
            (snapshot_id, row_index),
        ).fetchone()
        if existing is None:
            return None

        version = self.conn.execute(
            "SELECT metric_hash, semantic_hash, report_date, table_type, "
            "source_name, secucode FROM statement_version WHERE version_id = ?",
            (existing["version_id"],),
        ).fetchone()
        same_group = (
            version is not None
            and version["report_date"] == report_date
            and version["table_type"] == table_type
            and version["source_name"] == source_name
            and version["secucode"] == secucode
        )
        same_content = (
            version is not None
            and version["metric_hash"] == metric_hash
            and version["semantic_hash"] == semantic_hash
        )
        same_meta = (
            existing["notice_date"] == notice_date
            and existing["revision_notice_date"] == revision_notice_date
            and existing["revision_type"] == revision_type
            and existing["entry_time"] == entry_time
            and existing["normalization_diagnostics"] == normalization_diagnostics
        )
        if same_group and same_content and same_meta:
            return existing["version_id"]
        raise CaptureConflict(
            f"来源行身份 (snapshot={snapshot_id}, row_index={row_index}) 已存在，"
            "但本次版本分组、内容或元信息/诊断不同：拒绝静默改写既有观察"
        )

    def _verify_new_evidence(
        self, *, snapshot, unit_evidence, snapshot_id, row_index, secucode, table_type,
        run_id, source_name, standardized_dict, raw_fields_json,
    ) -> None:
        """校验"新证据充分"的全部条件，任一不满足即拒绝声明。

        条件：http-body-v1 + valid 页 + 文件当前可通过校验 + 单元完整性证明
        （全部最终页均为已校验 v4 证据且属于本单元）+ 行定位可重建传入值。
        """
        if snapshot is None:
            raise ValueError("evidence_sufficient=1 时必须提供有效 snapshot_id")
        if snapshot["raw_format"] != "http-body-v1":
            raise ValueError(
                "legacy-unverified 快照不能声明 evidence_sufficient=1："
                "旧快照没有经过 v4 文件/哈希核验"
            )
        if snapshot["validation_status"] != "valid":
            raise ValueError(
                f"evidence_sufficient=1 要求页校验状态为 valid，"
                f"当前 {snapshot['validation_status']!r}"
            )

        # 文件当前存在且哈希/大小一致（不接受历史标志或调用方自述）
        self._verify_evidence_file(
            snapshot["file_path"], snapshot["raw_content_hash"], snapshot["raw_bytes"]
        )

        if unit_evidence is None:
            raise ValueError(
                "evidence_sufficient=1 需要单元完整性证明（UnitEvidence）："
                "仅凭单页快照不能宣称分页完整、单元成功"
            )
        if not getattr(unit_evidence, "complete", False):
            raise ValueError("单元完整性证明为不完整（complete=False），不能声明证据充分")

        self._verify_unit_evidence(
            unit_evidence=unit_evidence,
            snapshot_id=snapshot_id,
            row_index=row_index,
            secucode=secucode,
            table_type=table_type,
            run_id=run_id,
            source_name=source_name,
            standardized_dict=standardized_dict,
            raw_fields_json=raw_fields_json,
        )

    def _final_attempts_by_page(self, unit_id: str) -> dict[int, sqlite3.Row]:
        """D2：从数据库事实确定每页的最终尝试（同页最大 http_attempt）。

        最终页身份不能由调用方列表决定——必须查询持久化的 attempt 集合。
        返回 {page_number: attempt_row}。
        """
        rows = self.conn.execute(
            "SELECT * FROM ingest_attempt WHERE unit_id = ? ORDER BY page_number, http_attempt",
            (unit_id,),
        ).fetchall()
        final_by_page: dict[int, sqlite3.Row] = {}
        for row in rows:
            page = row["page_number"]
            if page not in final_by_page or row["http_attempt"] > final_by_page[page]["http_attempt"]:
                final_by_page[page] = row
        return final_by_page

    def _verify_unit_evidence(
        self, *, unit_evidence, snapshot_id, row_index, secucode, table_type, run_id,
        source_name, standardized_dict, raw_fields_json,
    ) -> None:
        """核验单元完整性证明与逐项身份一致。"""
        from fin_feature.constants import (
            REPORT_BALANCE_SHEET,
            REPORT_FINANCIAL_INDICATOR,
            REPORT_INCOME_STATEMENT,
        )

        table_type_to_report = {
            "financial_indicator": REPORT_FINANCIAL_INDICATOR,
            "income_statement": REPORT_INCOME_STATEMENT,
            "balance_sheet": REPORT_BALANCE_SHEET,
        }
        expected_report = table_type_to_report.get(table_type)

        unit = self.get_unit(getattr(unit_evidence, "unit_id", None))
        if unit is None:
            raise ValueError(
                f"单元完整性证明引用的 unit_id={getattr(unit_evidence, 'unit_id', None)!r} 不存在"
            )
        if run_id is not None and unit["run_id"] != run_id:
            raise ValueError(
                f"单元批次不一致：unit.run_id={unit['run_id']}，版本 run_id={run_id}"
            )
        if unit["source_name"] != source_name:
            raise ValueError(
                f"单元来源不一致：unit.source_name={unit['source_name']}，版本={source_name}"
            )
        if expected_report is not None and unit["report_name"] != expected_report:
            raise ValueError(
                f"单元报表不一致：unit.report_name={unit['report_name']}，期望 {expected_report}"
            )
        if unit["secucode"] != secucode:
            raise ValueError(
                f"单元公司不一致：unit.secucode={unit['secucode']}，版本 secucode={secucode}"
            )

        page_ids = tuple(getattr(unit_evidence, "page_snapshot_ids", ()) or ())
        if not page_ids:
            raise ValueError("单元完整性证明必须给出全部最终成功页的 snapshot 身份")
        if snapshot_id not in page_ids:
            raise ValueError(
                f"本次快照 {snapshot_id} 不在单元最终页证据集合中：不能把非最终页当业务输入"
            )
        # D2：最终页身份由数据库事实决定，不能只信调用方给出的快照列表。
        # 查询本单元每页的实际最终尝试（同页最大 http_attempt），证明引用的快照必须
        # 恰为该最终尝试的唯一 snapshot——旧成功快照在后续失败/running 尝试存在时不能冒充。
        final_attempts_by_page = self._final_attempts_by_page(unit["unit_id"])
        # E1：证明引用集合必须精确覆盖整个单元的最终页集合，不能是任意子集。
        # 遗漏/重复/范围外页均拒绝——局部页都成功不代表单元完整。
        proof_pages: set[int] = set()
        for page_snapshot_id in page_ids:
            page = self.get_snapshot(page_snapshot_id)
            if page is None:
                raise ValueError(f"单元页证据 {page_snapshot_id} 不存在")
            attempt = self.get_attempt(page["attempt_id"]) if page["attempt_id"] else None
            if attempt is None or attempt["unit_id"] != unit["unit_id"]:
                raise ValueError(
                    f"单元页证据 {page_snapshot_id} 不属于单元 {unit['unit_id']}"
                )
            proof_pages.add(attempt["page_number"])
        expected_pages = set(final_attempts_by_page.keys())
        # E1：重复页单独检查——即使页集合恰好匹配，重复引用也破坏证明的精确性
        if len(page_ids) != len(proof_pages):
            raise ValueError(
                f"单元完整性证明未精确覆盖全部最终页（证明包含重复页）："
                f"证明引用 {len(page_ids)} 个快照但只覆盖 {len(proof_pages)} 页"
            )
        if proof_pages != expected_pages:
            missing = sorted(expected_pages - proof_pages)
            extra = sorted(proof_pages - expected_pages)
            detail = []
            if missing:
                detail.append(f"遗漏页 {missing}")
            if extra:
                detail.append(f"范围外页 {extra}")
            raise ValueError(
                f"单元完整性证明未精确覆盖全部最终页（{', '.join(detail)}）："
                f"证明页 {sorted(proof_pages)}，单元最终页 {sorted(expected_pages)}"
            )
        for page_snapshot_id in page_ids:
            page = self.get_snapshot(page_snapshot_id)
            if page["raw_format"] != "http-body-v1" or page["validation_status"] != "valid":
                raise ValueError(
                    f"单元页证据 {page_snapshot_id} 未经 v4 校验（format/validation 不符）"
                )
            attempt = self.get_attempt(page["attempt_id"])
            # D2：核对证明引用的快照恰为该页最终尝试的唯一 snapshot
            page_number = attempt["page_number"]
            final_attempt = final_attempts_by_page[page_number]
            if final_attempt["attempt_id"] != attempt["attempt_id"]:
                raise ValueError(
                    f"单元页 {page_number} 的最终尝试是 http_attempt="
                    f"{final_attempt['http_attempt']}（attempt_id="
                    f"{final_attempt['attempt_id']}），但证明引用的是 http_attempt="
                    f"{attempt['http_attempt']}：不能把非最终页当业务输入"
                )
            # C2：终态失败的响应不能凭 valid 标志冒充业务输入。最终尝试必须 outcome=ok
            # 且 HTTP 状态成功（2xx）；http_error/business_error 等终态失败即使文件有效、
            # 快照标 valid，也不得声明证据充分。矛盾数据在此拒绝，不只指望协调器不生成。
            if attempt["outcome"] != "ok":
                raise ValueError(
                    f"单元页证据 {page_snapshot_id} 的最终尝试 outcome={attempt['outcome']}，"
                    f"非 ok：终态失败响应不能作为充分证据"
                )
            http_status = page["http_status"]
            if http_status is None or not (200 <= int(http_status) < 300):
                raise ValueError(
                    f"单元页证据 {page_snapshot_id} 的 HTTP 状态 {http_status} 非成功（2xx），"
                    f"不能作为充分证据"
                )
            # R7：不能只信历史 valid 标志——逐页重新核对当前文件存在且哈希/大小一致
            self._verify_evidence_file(
                page["file_path"], page["raw_content_hash"], page["raw_bytes"]
            )

        # F1/F2/F3：与 audit 共用同一份信封解析结果，验证原体声明范围与内容。
        # F1：证明必须覆盖源端声明的全部页范围，不能只覆盖已登记页。
        # E1 只能发现调用方遗漏一个已存在的失败页，却发现不了采集未继续到下一页的情况。
        # 从已核验原体取得源端声明 pages，proof 页、持久化最终页和声明范围必须一致。
        from fin_feature.storage.envelope import parse_envelope
        if self.raw_store is not None:
            page_envelopes: dict[int, object] = {}
            for page_snapshot_id in page_ids:
                page = self.get_snapshot(page_snapshot_id)
                page_attempt = self.get_attempt(page["attempt_id"])
                page_number = page_attempt["page_number"]
                body = self.raw_store.verify({
                    "file_path": page["file_path"],
                    "raw_hash": page["raw_content_hash"],
                    "raw_bytes": page["raw_bytes"],
                }).body
                envelope = parse_envelope(body, unit["unit_kind"])
                if envelope.error is not None:
                    raise ValueError(
                        f"单元页证据 {page_snapshot_id} 信封解析失败：{envelope.error}"
                    )
                # F3：非对象元素拒绝——仅"最外层是 list"不满足契约
                if not envelope.all_rows_objects:
                    raise ValueError(
                        f"单元页证据 {page_snapshot_id} 原体 data/diff 含非对象元素"
                    )
                # F3：原体行数与 metadata record_count 对照——两个相同错误值不能互相作证
                if (
                    envelope.data_rows is not None
                    and page["record_count"] is not None
                    and envelope.data_rows != int(page["record_count"])
                ):
                    raise ValueError(
                        f"单元页证据 {page_snapshot_id} 原体行数 {envelope.data_rows} "
                        f"与快照 record_count {page['record_count']} 不一致"
                    )
                page_envelopes[page_number] = envelope
            # 股票发现不分页（pages 固定 1），跳过声明范围覆盖检查
            if unit["unit_kind"] == "financial" and page_envelopes:
                declared_pages_set = {env.pages for env in page_envelopes.values()}
                if len(declared_pages_set) != 1:
                    raise ValueError(
                        f"单元各页声明的总页数不一致：{sorted(declared_pages_set)}"
                    )
                declared_pages = declared_pages_set.pop()
                if declared_pages == 0:
                    # F2：pages=0 必须是合法空（单页、data=[]、count 为 0 或缺失）
                    env = next(iter(page_envelopes.values()))
                    if proof_pages != {1} or not env.is_legal_empty:
                        raise ValueError(
                            f"单元声明 pages=0 但不满足合法空条件"
                            f"（证明页 {sorted(proof_pages)}，data_rows={env.data_rows}，"
                            f"count={env.count}）：合法空要求单页、data=[] 且 count 为 0 或缺失"
                        )
                elif declared_pages == 1:
                    # F2：单页空响应必须是合法空；非空则正常单页
                    env = next(iter(page_envelopes.values()))
                    if proof_pages != {1}:
                        raise ValueError(
                            f"单元声明 pages=1，但证明页 {sorted(proof_pages)}"
                            f"（单页单元只允许页 [1]）"
                        )
                    if env.data_rows == 0 and not env.is_legal_empty:
                        raise ValueError(
                            f"单元声明 pages=1 但空数据不满足合法空条件"
                            f"（count={env.count}）：合法空要求 count 为 0 或缺失"
                        )
                else:
                    expected_declared = set(range(1, declared_pages + 1))
                    if proof_pages != expected_declared:
                        missing = sorted(expected_declared - proof_pages)
                        extra = sorted(proof_pages - expected_declared)
                        detail = []
                        if missing:
                            detail.append(f"遗漏声明页 {missing}")
                        if extra:
                            detail.append(f"范围外页 {extra}")
                        raise ValueError(
                            f"单元完整性证明未覆盖源端声明的全部页范围"
                            f"（{', '.join(detail)}）：证明页 {sorted(proof_pages)}，"
                            f"源端声明 {declared_pages} 页"
                        )
                    # F2：多页场景下任一页 data=[] → 不完整
                    for pn, env in page_envelopes.items():
                        if env.data_rows == 0:
                            raise ValueError(
                                f"单元页 {pn} 原体 data 为空：多页场景下空页不满足完整性"
                            )
                # F3：已知 count 必须等于全部声明页原体行数之和
                known_counts = {
                    env.count for env in page_envelopes.values() if env.count is not None
                }
                if len(known_counts) > 1:
                    raise ValueError(
                        f"单元各页声明的总记录数不一致：{sorted(known_counts)}"
                    )
                if known_counts:
                    total_body_rows = sum(
                        env.data_rows or 0 for env in page_envelopes.values()
                    )
                    declared_count = known_counts.pop()
                    if declared_count != total_body_rows:
                        raise ValueError(
                            f"单元声明总记录数 count={declared_count}，"
                            f"但原体行数合计 {total_body_rows}"
                        )

        if row_index is None:
            raise ValueError(
                "新证据必须给出 row_index（来源行定位）：无法定位的行不能声明证据充分"
            )
        verifier = getattr(unit_evidence, "row_verifier", None)
        if verifier is None:
            raise ValueError(
                "缺少行定位核验器（row_verifier）：不能声明 row_index 与传入值一致"
            )
        raw_fields = {}
        if raw_fields_json:
            import json as _json
            try:
                raw_fields = _json.loads(raw_fields_json)
            except (_json.JSONDecodeError, TypeError):
                raw_fields = {}
        if not verifier(snapshot_id, row_index, dict(standardized_dict), raw_fields):
            raise ValueError(
                f"行定位与传入值不一致：snapshot={snapshot_id}, row_index={row_index} "
                "无法重建本次映射/标准值"
            )

    def get_observations_for_version(self, version_id: str) -> list[sqlite3.Row]:
        """R3 修复：获取某版本的全部观察记录（含关联的 snapshot）。"""
        return self.conn.execute("""
            SELECT * FROM statement_observation WHERE version_id = ?
            ORDER BY observed_at ASC
        """, (version_id,)).fetchall()

    def get_latest_statement_version(
        self, secucode: str, report_date: str, table_type: str, source_name: str = "eastmoney"
    ) -> sqlite3.Row | None:
        """获取最新非替代版本。"""
        sql = """
            SELECT * FROM statement_version
            WHERE secucode = ? AND report_date = ? AND table_type = ? AND source_name = ?
              AND is_superseded = 0
            ORDER BY observation_seq DESC
            LIMIT 1
        """
        return self.conn.execute(
            sql, (secucode, report_date, table_type, source_name)
        ).fetchone()

    def get_statement_versions(
        self, secucode: str, report_date: str, table_type: str, source_name: str = "eastmoney"
    ) -> list[sqlite3.Row]:
        """获取全部版本历史（按 observation_seq 升序）。"""
        sql = """
            SELECT * FROM statement_version
            WHERE secucode = ? AND report_date = ? AND table_type = ? AND source_name = ?
            ORDER BY observation_seq ASC
        """
        return self.conn.execute(
            sql, (secucode, report_date, table_type, source_name)
        ).fetchall()

    def count_versions_for_run(self, run_id: str) -> int:
        """本批次已持久化的版本数（含复用版本的本次观察，不含回滚事务）。"""
        return self.conn.execute(
            "SELECT COUNT(*) FROM statement_version WHERE run_id = ?", (run_id,)
        ).fetchone()[0]

    def count_observations_for_run(self, run_id: str) -> int:
        """本批次已持久化的观察数；业务事务回滚后计数回到事务前。"""
        return self.conn.execute(
            "SELECT COUNT(*) FROM statement_observation WHERE run_id = ?", (run_id,)
        ).fetchone()[0]

    def apply_current_version(
        self, secucode: str, report_date: str, table_type: str,
        source_name: str, version_id: str,
    ) -> bool:
        """把组内唯一当前版本设为 version_id，其余版本标记为已替代。

        2B-2：协调器在同一业务事务内按阶段一选值规则选出候选后调用；不在
        create_statement_version 内按到达顺序翻转当前投影（较旧公告后到不能
        降级覆盖已知新版）。返回是否有标记发生变化。
        """
        row = self.conn.execute(
            "SELECT is_superseded, superseded_by FROM statement_version "
            "WHERE version_id = ? AND secucode = ? AND report_date = ? "
            "AND table_type = ? AND source_name = ?",
            (version_id, secucode, report_date, table_type, source_name),
        ).fetchone()
        if row is None:
            raise ValueError(
                f"version_id={version_id} 不属于分组 "
                f"({secucode}, {report_date}, {table_type}, {source_name})"
            )
        changed = row["is_superseded"] != 0 or row["superseded_by"] is not None
        self.conn.execute(
            "UPDATE statement_version SET is_superseded = 0, superseded_by = NULL "
            "WHERE version_id = ?",
            (version_id,),
        )
        cur = self.conn.execute(
            "UPDATE statement_version SET is_superseded = 1, superseded_by = ? "
            "WHERE secucode = ? AND report_date = ? AND table_type = ? AND source_name = ? "
            "AND version_id != ? AND (is_superseded != 1 OR superseded_by IS NOT ?)",
            (version_id, secucode, report_date, table_type, source_name,
             version_id, version_id),
        )
        return changed or cur.rowcount > 0

    def get_latest_versions_for_report(
        self, secucode: str, report_date: str
    ) -> dict[str, sqlite3.Row]:
        """获取同一 report_date 三表（FI/IS/BS）各自的最新非替代版本。

        返回：{"financial_indicator": Row, "income_statement": Row, "balance_sheet": Row}
        """
        result = {}
        for table_type in ["financial_indicator", "income_statement", "balance_sheet"]:
            row = self.get_latest_statement_version(secucode, report_date, table_type)
            if row is not None:
                result[table_type] = row
        return result

    # ------------------------------------------------------------------
    # 阶段 2 新增：不可变计算记录 (feature_compute_log)
    # ------------------------------------------------------------------
    def save_compute_log(
        self,
        compute_run_id: str,
        secucode: str,
        report_date: str,
        feature_name: str,
        definition_version: str,
        input_version_ids: str,
        feature_value: float | None,
        not_computable_reason: str | None,
        code_commit: str | None,
        config_snapshot: str | None,
        as_of_date: str | None,
    ) -> str:
        """写入不可变计算记录，返回 result_id（UUID）。P1 #3 修复：不自行提交。"""
        import uuid
        result_id = str(uuid.uuid4())
        sql = """
            INSERT INTO feature_compute_log
            (result_id, compute_run_id, secucode, report_date, feature_name,
             definition_version, input_version_ids, feature_value, not_computable_reason,
             code_commit, config_snapshot, as_of_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        self.conn.execute(
            sql,
            (result_id, compute_run_id, secucode, report_date, feature_name,
             definition_version, input_version_ids, feature_value, not_computable_reason,
             code_commit, config_snapshot, as_of_date),
        )
        return result_id

    def get_compute_log(self, result_id: str) -> sqlite3.Row | None:
        """按 result_id 读取计算记录及其 input_version_ids（重放入口）。"""
        sql = "SELECT * FROM feature_compute_log WHERE result_id = ?"
        return self.conn.execute(sql, (result_id,)).fetchone()
