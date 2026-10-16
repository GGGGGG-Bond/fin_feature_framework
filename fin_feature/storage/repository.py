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

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
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
        else:
            # 最外层：提交或回滚整个事务
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()


class Repository:
    """统一的数据访问层，封装所有表的 CRUD 与查询。

    R1 修复：所有写入方法不自行提交（移除 with self.conn），事务由调用方通过
    unit_of_work() 或显式 commit() 控制。这使多个写入可组合为原子单元。
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._table_columns_cache: dict[str, set[str]] = {}

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
        return _UnitOfWork(self.conn)

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
    ) -> str:
        """创建采集批次，返回 run_id（UUID）。

        P1 #3 修复：不自行提交，事务由外层调用方控制（采集单元原子性）。
        """
        import uuid
        run_id = str(uuid.uuid4())
        sql = """
            INSERT INTO ingest_run (run_id, source_name, request_scope, crawler_version, status)
            VALUES (?, ?, ?, ?, 'running')
        """
        self.conn.execute(sql, (run_id, source_name, request_scope, crawler_version))
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
    # 阶段 2 新增：原始响应留存 (raw_snapshot)
    # ------------------------------------------------------------------
    def save_raw_snapshot(
        self,
        run_id: str,
        source_name: str,
        report_name: str,
        secucode: str | None,
        page_number: int,
        request_params: str,
        raw_content_hash: str,
        file_path: str,
        record_count: int,
    ) -> str:
        """保存原始响应记录，返回 snapshot_id（UUID）。P1 #3 修复：不自行提交。

        A4 修复：校验 run_id 存在于 ingest_run 且 source_name 一致，
        在创建边界阻止错误的 run/source 关联。
        """
        import uuid
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
        snapshot_id = str(uuid.uuid4())
        sql = """
            INSERT INTO raw_snapshot
            (snapshot_id, run_id, source_name, report_name, secucode, page_number,
             request_params, raw_content_hash, file_path, record_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        self.conn.execute(
            sql,
            (snapshot_id, run_id, source_name, report_name, secucode, page_number,
             request_params, raw_content_hash, file_path, record_count),
        )
        return snapshot_id

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
    ) -> str:
        """创建标准化版本记录，处理 observation_seq 自增和旧版本替代。

        R3 修复：同内容重复观察不制造新版本，但通过 statement_observation 表保存
        每次观察与 snapshot 的关联，与版本投影同事务写入。
        R5 修复：evidence_sufficient=1 时校验 snapshot 的来源/公司/报表与版本一致。
        R6 修复：哈希由 Repository 内部根据 standardized_fields 和语义维度统一计算，
        不接受调用方任意提供的哈希值，确保哈希与负载一致。

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
        # 默认无 snapshot 时 evidence_sufficient=0
        if evidence_sufficient is None:
            evidence_sufficient = 0 if snapshot_id is None else 1

        # R5/A4：有快照时校验来源/公司/报表/批次一致
        if snapshot_id is not None:
            self._verify_snapshot_consistency(
                snapshot_id, source_name, secucode, table_type, run_id
            )
        elif evidence_sufficient == 1:
            raise ValueError("evidence_sufficient=1 时必须提供有效 snapshot_id")

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
            if existing[1] == 1:
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
            self._record_observation(
                existing_id, snapshot_id, run_id,
                notice_date=notice_date,
                revision_notice_date=revision_notice_date,
                revision_type=revision_type,
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
             notice_date, revision_notice_date, revision_type, metric_hash, semantic_hash,
             observation_seq, consolidate_scope, period_basis, currency, amount_unit,
             industry_type, standardized_fields, raw_fields, evidence_sufficient)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        self.conn.execute(
            sql,
            (version_id, snapshot_id, run_id, secucode, report_date, table_type, source_name,
             notice_date, revision_notice_date, revision_type, metric_hash, semantic_hash,
             next_seq, consolidate_scope, period_basis, currency, amount_unit,
             industry_type, standardized_fields, raw_fields, evidence_sufficient),
        )

        # R3：记录首次观察与 snapshot 的关联
        # A3：保存本次观察的公告/修订元信息
        self._record_observation(
            version_id, snapshot_id, run_id,
            notice_date=notice_date,
            revision_notice_date=revision_notice_date,
            revision_type=revision_type,
        )

        # 标记旧版本为替代（同分组内仅最新为 0）
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
    ) -> None:
        """R3 修复：在 statement_observation 表记录本次观察与 snapshot 的关联。

        A3 修复：保存本次观察的公告/修订元信息，不覆盖版本行的首次记录。
        snapshot_id 为 None 时不记录观察（旧数据迁移无原始响应）。
        """
        if snapshot_id is None:
            return
        import uuid
        observation_id = str(uuid.uuid4())
        self.conn.execute("""
            INSERT INTO statement_observation
            (observation_id, version_id, snapshot_id, run_id,
             notice_date, revision_notice_date, revision_type)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (observation_id, version_id, snapshot_id, run_id,
              notice_date, revision_notice_date, revision_type))

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
