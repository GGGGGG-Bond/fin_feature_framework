"""R1-R7 复审修复验证测试：事务所有权、观察关联、哈希一致性、证据校验。

覆盖 MR !4 复审 6 项发现（4 P1 + 2 P2）的正例与反例。
"""

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from fin_feature.storage.database import get_connection
from fin_feature.storage import migrations as m, schema as s
from fin_feature.storage.hashing import (
    compute_metric_hash, compute_semantic_hash, is_valid_hash, canonical_json,
)
from fin_feature.storage.migrations import run_migrations
from fin_feature.storage.repository import Repository
from fin_feature.features.standardize import set_unit_override, clear_unit_overrides


@pytest.fixture
def db_conn():
    """内存数据库 + 迁移，返回 (conn, repo)。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    run_migrations(conn)
    repo = Repository(conn)
    yield conn, repo
    conn.close()


@pytest.fixture
def file_db():
    """临时文件数据库，测试后自动清理。返回 (conn, repo, db_path)。"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    conn = get_connection(db_path)
    run_migrations(conn)
    repo = Repository(conn)
    yield conn, repo, db_path
    conn.close()
    Path(db_path).unlink(missing_ok=True)


def _make_version(repo, value=100, **kw):
    """创建版本记录的辅助函数。"""
    f = json.dumps({"total_operate_income": value}, sort_keys=True)
    return repo.create_statement_version(
        secucode="000001.SZ",
        report_date="2024-03-31",
        table_type="financial_indicator",
        standardized_fields=f,
        **kw,
    )


# ===========================================================================
# R1: 事务所有权 — 水位持久化、原子回滚、log_clean_action 不提前提交
# ===========================================================================

class TestR1TransactionOwnership:
    """R1 修复：事务所有权明确，写入可组合为原子单元。"""

    def test_watermark_persists_after_close(self, file_db):
        """R1：增量采集后关闭重连，水位持久化。"""
        conn, repo, db_path = file_db
        from fin_feature.addons.incremental import crawl_incremental
        from fin_feature.constants import REPORT_FINANCIAL_INDICATOR

        class FixtureCrawler:
            def fetch_all(self, *a, **kw):
                return [{"SECUCODE": "000001.SZ", "SECURITY_CODE": "000001",
                         "REPORTDATE": "2024-03-31", "NOTICE_DATE": "2024-04-15",
                         "TOTAL_OPERATE_INCOME": 100}]

        stats = crawl_incremental(
            FixtureCrawler(), repo, ["000001.SZ"],
            report_names=[REPORT_FINANCIAL_INDICATOR],
        )
        before = repo.get_watermark(REPORT_FINANCIAL_INDICATOR, "000001.SZ")
        conn.close()

        # 重连后水位仍在
        conn2 = get_connection(db_path)
        repo2 = Repository(conn2)
        after = repo2.get_watermark(REPORT_FINANCIAL_INDICATOR, "000001.SZ")
        raw_count = conn2.execute(
            "SELECT count(*) FROM raw_financial_indicator"
        ).fetchone()[0]
        conn2.close()

        assert before == "2024-04-15"
        assert after == "2024-04-15"
        assert raw_count == 1

    def test_outer_transaction_rollback(self, db_conn):
        """R1：外层 with conn 异常时，版本+水位+日志一起回滚。"""
        conn, repo = db_conn

        try:
            with conn:
                _make_version(repo)
                repo.update_watermark("eastmoney", "000001.SZ", "2024-04-15")
                repo.log_clean_action("000001.SZ", "2024-03-31", "fixture",
                                      "fixture", "controlled")
                raise RuntimeError("fail")
        except RuntimeError:
            pass

        versions = conn.execute(
            "SELECT count(*) FROM statement_version"
        ).fetchone()[0]
        watermark = repo.get_watermark("eastmoney", "000001.SZ")
        logs = conn.execute("SELECT count(*) FROM clean_log").fetchone()[0]

        # 全部回滚：log_clean_action 不再提前提交
        assert versions == 0
        assert watermark is None
        assert logs == 0

    def test_unit_of_work_commits_on_success(self, db_conn):
        """R1：unit_of_work 成功时提交。"""
        conn, repo = db_conn

        with repo.unit_of_work():
            repo.update_watermark("eastmoney", "000001.SZ", "2024-04-15")

        # 提交后可读
        assert repo.get_watermark("eastmoney", "000001.SZ") == "2024-04-15"

    def test_unit_of_work_rolls_back_on_exception(self, db_conn):
        """R1：unit_of_work 异常时回滚。"""
        conn, repo = db_conn

        with pytest.raises(RuntimeError):
            with repo.unit_of_work():
                repo.update_watermark("eastmoney", "000001.SZ", "2024-04-15")
                raise RuntimeError("fail")

        assert repo.get_watermark("eastmoney", "000001.SZ") is None


# ===========================================================================
# R3: 观察关联 — 同内容重复观察保存 snapshot 关联
# ===========================================================================

class TestR3ObservationAssociation:
    """R3 修复：statement_observation 表保存版本与 snapshot 的观察关联。"""

    def test_observation_table_exists(self, db_conn):
        """R3：statement_observation 表存在。"""
        conn, _ = db_conn
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "statement_observation" in tables

    def test_new_version_creates_observation(self, db_conn):
        """R3：新版本创建时记录首次观察。"""
        conn, repo = db_conn
        run = repo.create_ingest_run("eastmoney")
        snap = repo.save_raw_snapshot(
            run, "eastmoney", "RPT_LICO_FN_CPD", "000001.SZ", 1,
            "{}", "a" * 64, "/tmp/x.json.gz", 1,
        )
        vid = _make_version(repo, snapshot_id=snap, run_id=run)

        obs = repo.get_observations_for_version(vid)
        assert len(obs) == 1
        assert obs[0]["snapshot_id"] == snap

    def test_duplicate_content_associates_new_snapshot(self, db_conn):
        """R3：同内容重复观察，新 snapshot 关联到同一版本。"""
        conn, repo = db_conn
        run = repo.create_ingest_run("eastmoney")
        p1 = repo.save_raw_snapshot(
            run, "eastmoney", "RPT_LICO_FN_CPD", "000001.SZ", 1,
            "{}", "a" * 64, "/tmp/x.json.gz", 1,
        )
        p2 = repo.save_raw_snapshot(
            run, "eastmoney", "RPT_LICO_FN_CPD", "000001.SZ", 2,
            "{}", "b" * 64, "/tmp/y.json.gz", 1,
        )

        a = _make_version(repo, snapshot_id=p1, run_id=run)
        b = _make_version(repo, snapshot_id=p2, run_id=run)

        # 同内容返回同一 version_id
        assert a == b

        # 两个 snapshot 都关联到该版本
        obs = repo.get_observations_for_version(a)
        snapshot_ids = {row["snapshot_id"] for row in obs}
        assert p1 in snapshot_ids
        assert p2 in snapshot_ids

    def test_no_observation_without_snapshot(self, db_conn):
        """R3：无 snapshot_id 时不记录观察。"""
        conn, repo = db_conn
        vid = _make_version(repo)

        obs = repo.get_observations_for_version(vid)
        assert len(obs) == 0


# ===========================================================================
# R4-a: 币种一致性 — 哈希与 INSERT 使用同一 currency
# ===========================================================================

class TestR4aCurrencyConsistency:
    """R4-a 修复：迁移 currency 统一 unknown，哈希可从存储字段重算。"""

    def test_migrated_currency_is_unknown(self):
        """R4-a：旧数据迁移后 currency=unknown（不推断 CNY）。"""
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.execute(s.DDL_RAW_FINANCIAL_INDICATOR)
        c.execute(
            "INSERT INTO raw_financial_indicator(secucode,report_date,notice_date,"
            "crawled_at,weightavg_roe) VALUES ('000001.SZ','2024-03-31',"
            "'2024-04-15','2024-04-15 10:00:00',16.75)"
        )
        c.commit()
        run_migrations(c)
        row = c.execute("SELECT * FROM statement_version").fetchone()
        c.close()

        assert row["currency"] == "unknown"

    def test_semantic_hash_recomputable_from_stored_fields(self):
        """R4-a：用存储字段重算 semantic_hash 一致。"""
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.execute(s.DDL_RAW_FINANCIAL_INDICATOR)
        c.execute(
            "INSERT INTO raw_financial_indicator(secucode,report_date,notice_date,"
            "crawled_at,weightavg_roe) VALUES ('000001.SZ','2024-03-31',"
            "'2024-04-15','2024-04-15 10:00:00',16.75)"
        )
        c.commit()
        run_migrations(c)
        row = c.execute("SELECT * FROM statement_version").fetchone()
        c.close()

        std = json.loads(row["standardized_fields"])
        recomputed = compute_semantic_hash(
            std,
            source_name=row["source_name"],
            currency=row["currency"],
            consolidate_scope=row["consolidate_scope"],
            period_basis=row["period_basis"],
            amount_unit=row["amount_unit"],
            industry_type=row["industry_type"],
        )
        assert recomputed == row["semantic_hash"]

    def test_metric_hash_recomputable_from_stored_fields(self):
        """R4-a：用存储字段重算 metric_hash 一致。"""
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.execute(s.DDL_RAW_FINANCIAL_INDICATOR)
        c.execute(
            "INSERT INTO raw_financial_indicator(secucode,report_date,notice_date,"
            "crawled_at,weightavg_roe) VALUES ('000001.SZ','2024-03-31',"
            "'2024-04-15','2024-04-15 10:00:00',16.75)"
        )
        c.commit()
        run_migrations(c)
        row = c.execute("SELECT * FROM statement_version").fetchone()
        c.close()

        std = json.loads(row["standardized_fields"])
        recomputed = compute_metric_hash(std)
        assert recomputed == row["metric_hash"]


# ===========================================================================
# R4-b: 非有限数 — Infinity 不进入标准字段与哈希
# ===========================================================================

class TestR4bNonFiniteRejection:
    """R4-b 修复：迁移使用统一标准化入口，非有限数被拒绝。"""

    def test_inf_rejected_from_standardized_fields(self):
        """R4-b：weightavg_roe=inf 不进入 standardized_fields。"""
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.execute(s.DDL_RAW_FINANCIAL_INDICATOR)
        c.execute(
            "INSERT INTO raw_financial_indicator(secucode,report_date,notice_date,"
            "crawled_at,weightavg_roe) VALUES ('000001.SZ','2024-03-31',"
            "'2024-04-15','2024-04-15 10:00:00',?)",
            (float("inf"),),
        )
        c.commit()
        run_migrations(c)
        row = c.execute("SELECT * FROM statement_version").fetchone()
        c.close()

        std = json.loads(row["standardized_fields"])
        # inf 被标准化为 None 并过滤
        assert "weightavg_roe" not in std

    def test_standardized_fields_strict_json(self):
        """R4-b：standardized_fields 是严格 JSON（无 NaN/Infinity）。"""
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.execute(s.DDL_RAW_FINANCIAL_INDICATOR)
        c.execute(
            "INSERT INTO raw_financial_indicator(secucode,report_date,notice_date,"
            "crawled_at,weightavg_roe) VALUES ('000001.SZ','2024-03-31',"
            "'2024-04-15','2024-04-15 10:00:00',?)",
            (float("inf"),),
        )
        c.commit()
        run_migrations(c)
        row = c.execute("SELECT standardized_fields FROM statement_version").fetchone()
        c.close()

        # 严格 JSON 解析不抛异常
        parsed = json.loads(row["standardized_fields"])
        json.dumps(parsed, allow_nan=False)  # 不抛 ValueError

    def test_migration_uses_unified_standardize(self):
        """R4-b：迁移使用 standardize_record（受 unit_override 影响验证）。"""
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.execute(s.DDL_RAW_FINANCIAL_INDICATOR)
        c.execute(
            "INSERT INTO raw_financial_indicator(secucode,report_date,notice_date,"
            "crawled_at,weightavg_roe) VALUES ('000001.SZ','2024-03-31',"
            "'2024-04-15','2024-04-15 10:00:00',16.75)"
        )
        c.commit()
        # 设置 times 覆盖（模拟 fixture 污染）
        set_unit_override("weightavg_roe", "times")
        try:
            run_migrations(c)
        finally:
            clear_unit_overrides()
        row = c.execute("SELECT * FROM statement_version").fetchone()
        c.close()

        std = json.loads(row["standardized_fields"])
        # times 覆盖不应影响迁移（使用固定 FIELD_UNITS）
        # weightavg_roe=16.75 应被 percent 转换为 0.1675
        assert std.get("weightavg_roe") == 0.1675


# ===========================================================================
# R5: 证据一致性 — 错来源/公司/表的 snapshot 被拒绝
# ===========================================================================

class TestR5EvidenceConsistency:
    """R5 修复：evidence_sufficient=1 时校验 snapshot 来源/公司/报表一致。"""

    def test_wrong_source_rejected(self, db_conn):
        """R5：snapshot 来源不一致时拒绝。"""
        conn, repo = db_conn
        run = repo.create_ingest_run("other_source")
        snap = repo.save_raw_snapshot(
            run, "other_source", "RPT_DMSK_FN_BALANCE", "999999.SZ", 1,
            "{}", "a" * 64, "/tmp/x.json.gz", 1,
        )
        with pytest.raises(ValueError, match="来源不一致"):
            _make_version(repo, snapshot_id=snap, run_id=run, evidence_sufficient=1)

    def test_wrong_company_rejected(self, db_conn):
        """R5：snapshot 公司不一致时拒绝。"""
        conn, repo = db_conn
        run = repo.create_ingest_run("eastmoney")
        snap = repo.save_raw_snapshot(
            run, "eastmoney", "RPT_LICO_FN_CPD", "999999.SZ", 1,
            "{}", "a" * 64, "/tmp/x.json.gz", 1,
        )
        with pytest.raises(ValueError, match="公司不一致"):
            _make_version(repo, snapshot_id=snap, run_id=run, evidence_sufficient=1)

    def test_wrong_report_rejected(self, db_conn):
        """R5：snapshot 报表不一致时拒绝。"""
        conn, repo = db_conn
        run = repo.create_ingest_run("eastmoney")
        snap = repo.save_raw_snapshot(
            run, "eastmoney", "RPT_DMSK_FN_BALANCE", "000001.SZ", 1,
            "{}", "a" * 64, "/tmp/x.json.gz", 1,
        )
        with pytest.raises(ValueError, match="报表不一致"):
            _make_version(repo, snapshot_id=snap, run_id=run, evidence_sufficient=1)

    def test_consistent_snapshot_accepted(self, db_conn):
        """R5：来源/公司/报表一致的 snapshot 被接受。"""
        conn, repo = db_conn
        run = repo.create_ingest_run("eastmoney")
        snap = repo.save_raw_snapshot(
            run, "eastmoney", "RPT_LICO_FN_CPD", "000001.SZ", 1,
            "{}", "a" * 64, "/tmp/x.json.gz", 1,
        )
        vid = _make_version(repo, snapshot_id=snap, run_id=run, evidence_sufficient=1)
        row = conn.execute(
            "SELECT evidence_sufficient FROM statement_version WHERE version_id=?",
            (vid,),
        ).fetchone()
        assert row[0] == 1

    def test_default_insufficient_without_snapshot(self, db_conn):
        """R5：无 snapshot 时默认 evidence_sufficient=0。"""
        conn, repo = db_conn
        vid = _make_version(repo)
        row = conn.execute(
            "SELECT evidence_sufficient FROM statement_version WHERE version_id=?",
            (vid,),
        ).fetchone()
        assert row[0] == 0

    def test_evidence_sufficient_1_without_snapshot_rejected(self, db_conn):
        """R5：evidence_sufficient=1 但无 snapshot 时拒绝。"""
        conn, repo = db_conn
        with pytest.raises(ValueError, match="必须提供有效 snapshot_id"):
            _make_version(repo, evidence_sufficient=1)


# ===========================================================================
# R6: 哈希校验 — Repository 内部计算，不接受任意哈希
# ===========================================================================

class TestR6HashVerification:
    """R6 修复：哈希由 Repository 内部计算，确保与负载一致。"""

    def test_repository_ignores_caller_hash(self, db_conn):
        """R6：Repository 忽略调用方提供的哈希，内部计算。"""
        conn, repo = db_conn
        f = json.dumps({"total_operate_income": 100}, sort_keys=True)
        # 传入错误的哈希，Repository 应忽略并计算正确的
        vid = repo.create_statement_version(
            secucode="000001.SZ", report_date="2024-03-31",
            table_type="financial_indicator", standardized_fields=f,
            metric_hash="z" * 64, semantic_hash="z" * 64,
        )
        row = conn.execute(
            "SELECT metric_hash, semantic_hash FROM statement_version WHERE version_id=?",
            (vid,),
        ).fetchone()
        std = json.loads(f)
        expected_metric = compute_metric_hash(std)
        expected_semantic = compute_semantic_hash(std)
        assert row["metric_hash"] == expected_metric
        assert row["semantic_hash"] == expected_semantic

    def test_conflicting_hash_not_silently_deduped(self, db_conn):
        """R6：不同内容用相同哈希不会静默去重。"""
        conn, repo = db_conn
        f1 = json.dumps({"total_operate_income": 100}, sort_keys=True)
        f2 = json.dumps({"total_operate_income": 120}, sort_keys=True)

        v1 = repo.create_statement_version(
            secucode="000001.SZ", report_date="2024-03-31",
            table_type="financial_indicator", standardized_fields=f1,
            metric_hash="a" * 64, semantic_hash="a" * 64, currency="CNY",
        )
        v2 = repo.create_statement_version(
            secucode="000001.SZ", report_date="2024-03-31",
            table_type="financial_indicator", standardized_fields=f2,
            metric_hash="a" * 64, semantic_hash="a" * 64, currency="USD",
        )
        # 不同内容 → 不同版本
        assert v1 != v2

    def test_same_content_same_hash_deduped(self, db_conn):
        """R6：相同内容（相同标准化字段+语义维度）→ 同一版本（幂等）。"""
        conn, repo = db_conn
        f = json.dumps({"total_operate_income": 100}, sort_keys=True)

        v1 = repo.create_statement_version(
            secucode="000001.SZ", report_date="2024-03-31",
            table_type="financial_indicator", standardized_fields=f,
        )
        v2 = repo.create_statement_version(
            secucode="000001.SZ", report_date="2024-03-31",
            table_type="financial_indicator", standardized_fields=f,
        )
        assert v1 == v2

    def test_semantic_change_creates_new_version(self, db_conn):
        """R6：相同金额但语义变化（CNY→USD）→ 新版本。"""
        conn, repo = db_conn
        f = json.dumps({"total_operate_income": 100}, sort_keys=True)

        v1 = repo.create_statement_version(
            secucode="000001.SZ", report_date="2024-03-31",
            table_type="financial_indicator", standardized_fields=f,
            currency="CNY",
        )
        v2 = repo.create_statement_version(
            secucode="000001.SZ", report_date="2024-03-31",
            table_type="financial_indicator", standardized_fields=f,
            currency="USD",
        )
        assert v1 != v2

    def test_is_valid_hash_rejects_non_hex(self):
        """R6：is_valid_hash 拒绝非十六进制 64 字符串。"""
        assert not is_valid_hash("z" * 64)
        assert not is_valid_hash("abc123")
        assert not is_valid_hash(None)
        assert not is_valid_hash(123)

    def test_is_valid_hash_accepts_hex64(self):
        """R6：is_valid_hash 接受有效 SHA-256 十六进制。"""
        import hashlib
        h = hashlib.sha256(b"test").hexdigest()
        assert is_valid_hash(h)

    def test_canonical_json_rejects_non_finite(self):
        """R4-b：canonical_json 拒绝 NaN/Infinity。"""
        with pytest.raises(ValueError):
            canonical_json({"v": float("inf")})
        with pytest.raises(ValueError):
            canonical_json({"v": float("nan")})


# ===========================================================================
# R7: 迁移 savepoint 回滚
# ===========================================================================

class TestR7MigrationRollback:
    """R7 修复：迁移失败 savepoint 回滚不残留。"""

    def test_migration_failure_rollback(self):
        """R7：迁移中途失败，savepoint 回滚不残留 DDL。"""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        run_migrations(conn)
        assert m._get_current_version(conn) == 2

        def failing_migration(c):
            c.execute("CREATE TABLE test_rollback (id INTEGER)")
            raise RuntimeError("模拟故障")

        with pytest.raises(RuntimeError, match="模拟故障"):
            m._apply_migration(conn, 3, "failing", failing_migration)

        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "test_rollback" not in tables
        assert m._get_current_version(conn) == 2
        conn.close()
