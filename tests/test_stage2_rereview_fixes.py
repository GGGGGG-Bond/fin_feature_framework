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

from evidence_helpers import EvidenceEnv


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

    def test_consistent_snapshot_accepted(self, db_conn, tmp_path):
        """R5：来源/公司/报表一致、且 v4 证据齐备的 snapshot 被接受。

        2B 起 evidence_sufficient=1 需要真实临时文件、已校验页与单元完整性证明，
        因此本 fixture 改为真实证据环境（保留原断言，不弱化）。
        """
        conn, _repo = db_conn
        env = EvidenceEnv(conn=conn, raw_root=str(tmp_path / "raw"))
        run = env.new_run()
        unit = env.new_unit()
        captured = env.capture(unit, rows=[{"TOTAL_OPERATE_INCOME": 100}])

        vid = env.create_version(unit, captured, evidence_sufficient=1)
        row = conn.execute(
            "SELECT evidence_sufficient, run_id FROM statement_version WHERE version_id=?",
            (vid,),
        ).fetchone()
        assert row["evidence_sufficient"] == 1
        assert row["run_id"] == run

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
        latest = max(v for v, _, _ in m.MIGRATIONS)
        assert m._get_current_version(conn) == latest

        def failing_migration(c):
            c.execute("CREATE TABLE test_rollback (id INTEGER)")
            raise RuntimeError("模拟故障")

        with pytest.raises(RuntimeError, match="模拟故障"):
            m._apply_migration(conn, latest + 1, "failing", failing_migration)

        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "test_rollback" not in tables
        assert m._get_current_version(conn) == latest
        conn.close()


# ===========================================================================
# A1: 嵌套事务 — unit_of_work 不提交/回滚外层事务
# ===========================================================================

class TestA1NestedTransaction:
    """A1 修复：嵌套的 unit_of_work 使用 savepoint，不破坏调用方原子性。"""

    def test_inner_uow_does_not_commit_outer_with_conn(self, db_conn):
        """A1：with conn 外层 + unit_of_work 内层，内层成功后外层异常 → 全部回滚。"""
        conn, repo = db_conn
        try:
            with conn:
                _make_version(repo)
                with repo.unit_of_work():
                    repo.update_watermark("eastmoney", "000001.SZ", "2024-04-15")
                raise RuntimeError("outer fail")
        except RuntimeError:
            pass

        assert conn.execute("SELECT count(*) FROM statement_version").fetchone()[0] == 0
        assert repo.get_watermark("eastmoney", "000001.SZ") is None

    def test_inner_uow_does_not_commit_outer_uow(self, db_conn):
        """A1：外层、内层均 unit_of_work，内层成功后外层异常 → 全部回滚。"""
        conn, repo = db_conn
        try:
            with repo.unit_of_work():
                _make_version(repo)
                with repo.unit_of_work():
                    repo.update_watermark("eastmoney", "000001.SZ", "2024-04-15")
                raise RuntimeError("outer fail")
        except RuntimeError:
            pass

        assert conn.execute("SELECT count(*) FROM statement_version").fetchone()[0] == 0
        assert repo.get_watermark("eastmoney", "000001.SZ") is None

    def test_inner_uow_failure_preserves_outer(self, db_conn):
        """A1：内层 unit_of_work 失败回滚后，外层数据保留。"""
        conn, repo = db_conn
        with repo.unit_of_work():
            _make_version(repo)
            try:
                with repo.unit_of_work():
                    repo.update_watermark("eastmoney", "000001.SZ", "2024-04-15")
                    raise RuntimeError("inner fail")
            except RuntimeError:
                pass

        # 外层版本保留，内层水位回滚
        assert conn.execute("SELECT count(*) FROM statement_version").fetchone()[0] == 1
        assert repo.get_watermark("eastmoney", "000001.SZ") is None


# ===========================================================================
# A2: 旧 v2 草稿库升级 — 缺少 statement_observation 表的结构被补齐
# ===========================================================================

# 5390dac 草案的 statement_version DDL（无 CHECK、无 observation 表、evidence 默认 1）
_LEGACY_V2_STATEMENT_VERSION = """
CREATE TABLE statement_version (
    version_id           TEXT PRIMARY KEY,
    snapshot_id          TEXT,
    run_id               TEXT,
    secucode             TEXT NOT NULL,
    report_date          TEXT NOT NULL,
    table_type           TEXT NOT NULL,
    source_name          TEXT NOT NULL DEFAULT 'eastmoney',
    notice_date          TEXT,
    revision_notice_date TEXT,
    revision_type        TEXT DEFAULT 'unknown',
    observed_at          TEXT DEFAULT (datetime('now', 'localtime')),
    metric_hash          TEXT NOT NULL,
    semantic_hash        TEXT NOT NULL,
    observation_seq      INTEGER NOT NULL,
    consolidate_scope    TEXT DEFAULT 'unknown',
    period_basis         TEXT DEFAULT 'unknown',
    currency             TEXT DEFAULT 'unknown',
    amount_unit          TEXT DEFAULT 'unknown',
    industry_type        TEXT DEFAULT 'unknown',
    standardized_fields  TEXT,
    raw_fields           TEXT,
    evidence_sufficient  INTEGER DEFAULT 1,
    is_superseded        INTEGER DEFAULT 0,
    superseded_by        TEXT,
    FOREIGN KEY (snapshot_id) REFERENCES raw_snapshot(snapshot_id),
    FOREIGN KEY (run_id) REFERENCES ingest_run(run_id),
    UNIQUE (secucode, report_date, table_type, source_name, observation_seq)
);
"""

_LEGACY_V2_INGEST_RUN = """
CREATE TABLE ingest_run (
    run_id TEXT PRIMARY KEY, source_name TEXT NOT NULL, request_scope TEXT,
    started_at TEXT DEFAULT (datetime('now','localtime')), ended_at TEXT,
    status TEXT NOT NULL DEFAULT 'running', failed_units INTEGER DEFAULT 0,
    crawler_version TEXT, detail TEXT
);
"""

_LEGACY_V2_RAW_SNAPSHOT = """
CREATE TABLE raw_snapshot (
    snapshot_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, source_name TEXT NOT NULL,
    report_name TEXT NOT NULL, secucode TEXT, page_number INTEGER,
    request_params TEXT, fetched_at TEXT DEFAULT (datetime('now','localtime')),
    raw_content_hash TEXT NOT NULL, file_path TEXT NOT NULL, record_count INTEGER,
    FOREIGN KEY (run_id) REFERENCES ingest_run(run_id)
);
"""


def _make_legacy_v2_draft_db() -> sqlite3.Connection:
    """构造 5390dac 草案 v2 库：schema_version=2，但无 statement_observation 表。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
        "applied_at TEXT DEFAULT (datetime('now','localtime')))"
    )
    conn.execute(_LEGACY_V2_INGEST_RUN)
    conn.execute(_LEGACY_V2_RAW_SNAPSHOT)
    conn.execute(_LEGACY_V2_STATEMENT_VERSION)
    conn.execute("INSERT INTO schema_version (version, name) VALUES (1, 'v1_baseline')")
    conn.execute("INSERT INTO schema_version (version, name) VALUES (2, 'v2_stage2')")
    conn.commit()
    return conn


class TestA2LegacyV2DraftUpgrade:
    """A2 修复：旧 v2 草稿库被 v3 迁移升级，不再静默接受缺表结构。"""

    def test_legacy_v2_draft_gets_observation_table(self):
        """A2：5390dac 草案 v2 库升级后 statement_observation 存在，观察可写入。"""
        conn = _make_legacy_v2_draft_db()
        assert m._table_exists(conn, "statement_version")
        assert not m._table_exists(conn, "statement_observation")

        run_migrations(conn)

        assert m._table_exists(conn, "statement_observation")
        assert m._get_current_version(conn) == max(v for v, _, _ in m.MIGRATIONS)

        # 观察写入不再抛 no such table
        repo = Repository(conn)
        run = repo.create_ingest_run("eastmoney")
        snap = repo.save_raw_snapshot(
            run, "eastmoney", "RPT_LICO_FN_CPD", "000001.SZ", 1,
            "{}", "a" * 64, "/tmp/x.json.gz", 1,
        )
        vid = repo.create_statement_version(
            secucode="000001.SZ", report_date="2024-03-31",
            table_type="financial_indicator", snapshot_id=snap, run_id=run,
            standardized_fields=json.dumps({"total_operate_income": 100}),
        )
        assert len(repo.get_observations_for_version(vid)) == 1
        conn.close()

    def test_empty_db_reaches_latest_with_observation(self):
        """A2：空库迁移到最新版本，含 statement_observation 表和 A3 列。"""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        run_migrations(conn)

        assert m._table_exists(conn, "statement_observation")
        assert m._get_current_version(conn) == max(v for v, _, _ in m.MIGRATIONS)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(statement_observation)").fetchall()}
        for col in ["notice_date", "revision_notice_date", "revision_type"]:
            assert col in cols

        # 幂等：再次运行不报错、不重复
        run_migrations(conn)
        rows = conn.execute(
            "SELECT version, COUNT(*) FROM schema_version GROUP BY version"
        ).fetchall()
        for row in rows:
            assert row[1] == 1
        conn.close()

    def test_181c4b6_style_draft_upgraded(self):
        """A2：181c4b6 风格 v2 库（有旧 observation 表/旧 GLOB）升级出新约束和 A3 列。"""
        conn = _make_legacy_v2_draft_db()
        # 模拟 181c4b6：已有旧 observation 表（无 A3 列）
        conn.execute(
            "CREATE TABLE statement_observation ("
            "observation_id TEXT PRIMARY KEY, version_id TEXT NOT NULL, "
            "snapshot_id TEXT, run_id TEXT, "
            "observed_at TEXT DEFAULT (datetime('now','localtime')), "
            "FOREIGN KEY (version_id) REFERENCES statement_version(version_id))"
        )
        conn.commit()

        run_migrations(conn)

        # statement_version 重建后含新约束
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='statement_version'"
        ).fetchone()[0]
        assert "NOT GLOB" in ddl
        # observation 补出 A3 列
        cols = {r[1] for r in conn.execute("PRAGMA table_info(statement_observation)").fetchall()}
        for col in ["notice_date", "revision_notice_date", "revision_type"]:
            assert col in cols
        assert m._get_current_version(conn) == max(v for v, _, _ in m.MIGRATIONS)
        conn.close()

    def test_nonempty_incompatible_draft_rejected(self):
        """A2：含数据的旧草稿库无法自动升级时，明确拒绝而非静默接受。"""
        conn = _make_legacy_v2_draft_db()
        conn.execute(
            "INSERT INTO statement_version (version_id, secucode, report_date, "
            "table_type, source_name, metric_hash, semantic_hash, observation_seq) "
            "VALUES ('old1', '000001.SZ', '2024-03-31', 'financial_indicator', "
            "'eastmoney', 'abc', 'def', 1)"
        )
        conn.commit()

        with pytest.raises(RuntimeError, match="无法自动升级"):
            run_migrations(conn)
        # 原数据未被删除
        assert conn.execute("SELECT COUNT(*) FROM statement_version").fetchone()[0] == 1
        conn.close()

    def test_stage1_db_upgrades_and_preserves_data(self):
        """A2：阶段 1 库（无 schema_version）升级后旧表数据保留并迁移。"""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(s.DDL_RAW_FINANCIAL_INDICATOR)
        conn.execute(
            "INSERT INTO raw_financial_indicator(secucode,report_date,notice_date,"
            "crawled_at,weightavg_roe) VALUES ('000001.SZ','2024-03-31',"
            "'2024-04-15','2024-04-15 10:00:00',16.75)"
        )
        conn.commit()

        run_migrations(conn)

        assert m._table_exists(conn, "statement_observation")
        # 旧表数据保留
        raw_count = conn.execute("SELECT COUNT(*) FROM raw_financial_indicator").fetchone()[0]
        assert raw_count == 1
        # 迁移到 statement_version
        sv_count = conn.execute("SELECT COUNT(*) FROM statement_version").fetchone()[0]
        assert sv_count == 1
        conn.close()


# ===========================================================================
# A3: 观察元信息 — 后续公告/修订元信息存于观察行，版本保留首次
# ===========================================================================

class TestA3ObservationMetadata:
    """A3 修复：观察行保存本次公告/修订元信息，版本行保留首次披露。"""

    def test_same_content_different_notice_keeps_both(self, db_conn):
        """A3：同内容不同公告/修订 → 同一版本，两条观察各保留元信息。"""
        conn, repo = db_conn
        run = repo.create_ingest_run("eastmoney")
        s1 = repo.save_raw_snapshot(
            run, "eastmoney", "RPT_LICO_FN_CPD", "000001.SZ", 1,
            "{}", "a" * 64, "/tmp/1.json.gz", 1,
        )
        s2 = repo.save_raw_snapshot(
            run, "eastmoney", "RPT_LICO_FN_CPD", "000001.SZ", 2,
            "{}", "b" * 64, "/tmp/2.json.gz", 1,
        )
        f = json.dumps({"total_operate_income": 100})

        v1 = repo.create_statement_version(
            secucode="000001.SZ", report_date="2024-03-31",
            table_type="financial_indicator", snapshot_id=s1, run_id=run,
            notice_date="2024-04-15", revision_type="initial",
            standardized_fields=f,
        )
        v2 = repo.create_statement_version(
            secucode="000001.SZ", report_date="2024-03-31",
            table_type="financial_indicator", snapshot_id=s2, run_id=run,
            notice_date="2024-04-20", revision_notice_date="2024-04-20",
            revision_type="revision", standardized_fields=f,
        )

        assert v1 == v2
        obs = repo.get_observations_for_version(v1)
        assert len(obs) == 2
        by_type = {o["revision_type"]: o for o in obs}
        assert by_type["initial"]["notice_date"] == "2024-04-15"
        assert by_type["revision"]["notice_date"] == "2024-04-20"
        assert by_type["revision"]["revision_notice_date"] == "2024-04-20"

    def test_version_keeps_first_notice_metadata(self, db_conn):
        """A3：版本行保留首次观察的 notice/revision_type，不被后续覆盖。"""
        conn, repo = db_conn
        run = repo.create_ingest_run("eastmoney")
        s1 = repo.save_raw_snapshot(
            run, "eastmoney", "RPT_LICO_FN_CPD", "000001.SZ", 1,
            "{}", "a" * 64, "/tmp/1.json.gz", 1,
        )
        s2 = repo.save_raw_snapshot(
            run, "eastmoney", "RPT_LICO_FN_CPD", "000001.SZ", 2,
            "{}", "b" * 64, "/tmp/2.json.gz", 1,
        )
        f = json.dumps({"total_operate_income": 100})

        vid = repo.create_statement_version(
            secucode="000001.SZ", report_date="2024-03-31",
            table_type="financial_indicator", snapshot_id=s1, run_id=run,
            notice_date="2024-04-15", revision_type="initial",
            standardized_fields=f,
        )
        repo.create_statement_version(
            secucode="000001.SZ", report_date="2024-03-31",
            table_type="financial_indicator", snapshot_id=s2, run_id=run,
            notice_date="2024-04-20", revision_type="revision",
            standardized_fields=f,
        )

        row = conn.execute(
            "SELECT notice_date, revision_type FROM statement_version WHERE version_id=?",
            (vid,),
        ).fetchone()
        assert row["notice_date"] == "2024-04-15"
        assert row["revision_type"] == "initial"

    def test_observation_metadata_defaults(self, db_conn):
        """A3：未提供元信息时观察行默认 revision_type=unknown。"""
        conn, repo = db_conn
        run = repo.create_ingest_run("eastmoney")
        snap = repo.save_raw_snapshot(
            run, "eastmoney", "RPT_LICO_FN_CPD", "000001.SZ", 1,
            "{}", "a" * 64, "/tmp/x.json.gz", 1,
        )
        vid = _make_version(repo, snapshot_id=snap, run_id=run)
        obs = repo.get_observations_for_version(vid)
        assert obs[0]["revision_type"] == "unknown"
        assert obs[0]["notice_date"] is None


# ===========================================================================
# A4: run 一致性 — 快照与版本的采集批次必须一致
# ===========================================================================

class TestA4RunConsistency:
    """A4 修复：校验快照 run 与版本 run 一致，创建快照时校验来源。"""

    def test_run_mismatch_rejected(self, db_conn):
        """A4：快照 run A + 版本 run B → 拒绝。"""
        conn, repo = db_conn
        run_a = repo.create_ingest_run("eastmoney")
        run_b = repo.create_ingest_run("eastmoney")
        snap = repo.save_raw_snapshot(
            run_a, "eastmoney", "RPT_LICO_FN_CPD", "000001.SZ", 1,
            "{}", "a" * 64, "/tmp/x.json.gz", 1,
        )
        with pytest.raises(ValueError, match="采集批次不一致"):
            _make_version(repo, snapshot_id=snap, run_id=run_b, evidence_sufficient=1)

    def test_run_match_accepted(self, db_conn, tmp_path):
        """A4：快照 run A + 版本 run A → 接受（真实临时文件证据）。"""
        conn, _repo = db_conn
        env = EvidenceEnv(conn=conn, raw_root=str(tmp_path / "raw"))
        run = env.new_run()
        unit = env.new_unit()
        captured = env.capture(unit, rows=[{"TOTAL_OPERATE_INCOME": 100}])

        vid = env.create_version(unit, captured, evidence_sufficient=1)
        row = conn.execute(
            "SELECT run_id FROM statement_version WHERE version_id=?", (vid,)
        ).fetchone()
        assert row["run_id"] == run

    def test_snapshot_source_mismatch_rejected(self, db_conn):
        """A4：save_raw_snapshot 来源与 run 不一致 → 拒绝。"""
        conn, repo = db_conn
        run = repo.create_ingest_run("eastmoney")
        with pytest.raises(ValueError, match="来源不一致"):
            repo.save_raw_snapshot(
                run, "other_source", "RPT_LICO_FN_CPD", "000001.SZ", 1,
                "{}", "a" * 64, "/tmp/x.json.gz", 1,
            )

    def test_snapshot_unknown_run_rejected(self, db_conn):
        """A4：save_raw_snapshot 引用不存在的 run → 拒绝。"""
        conn, repo = db_conn
        with pytest.raises(ValueError, match="不存在于 ingest_run"):
            repo.save_raw_snapshot(
                "no-such-run", "eastmoney", "RPT_LICO_FN_CPD", "000001.SZ", 1,
                "{}", "a" * 64, "/tmp/x.json.gz", 1,
            )


# ===========================================================================
# A5: 哈希 GLOB 约束 — 全串十六进制，非仅首字符
# ===========================================================================

class TestA5HashGlobConstraint:
    """A5 修复：CHECK 约束校验全串十六进制，非仅首字符。"""

    def _fresh_conn(self):
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        run_migrations(c)
        return c

    def _insert_hash(self, conn, metric_hash, semantic_hash, vid):
        conn.execute(
            "INSERT INTO statement_version "
            "(version_id, secucode, report_date, table_type, source_name, "
            "metric_hash, semantic_hash, observation_seq) "
            "VALUES (?, '000001.SZ', '2024-03-31', 'financial_indicator', 'eastmoney', ?, ?, 1)",
            (vid, metric_hash, semantic_hash),
        )

    def test_valid_hex_accepted(self):
        """A5：全小写十六进制 64 字符被接受。"""
        c = self._fresh_conn()
        h = "a" * 64
        self._insert_hash(c, h, h, "v1")
        assert c.execute("SELECT COUNT(*) FROM statement_version").fetchone()[0] == 1
        c.close()

    def test_middle_non_hex_rejected(self):
        """A5：首字符合法、中间非十六进制 → 拒绝。"""
        c = self._fresh_conn()
        bad = "a" + "z" + "0" * 62
        with pytest.raises(sqlite3.IntegrityError):
            self._insert_hash(c, bad, "a" * 64, "v2")
        c.close()

    def test_end_non_hex_rejected(self):
        """A5：首字符合法、末尾非十六进制 → 拒绝。"""
        c = self._fresh_conn()
        bad = "0" * 63 + "z"
        with pytest.raises(sqlite3.IntegrityError):
            self._insert_hash(c, "a" * 64, bad, "v3")
        c.close()

    def test_uppercase_rejected(self):
        """A5：大写十六进制 → 拒绝（与 is_valid_hash 的 [0-9a-f] 一致）。"""
        c = self._fresh_conn()
        with pytest.raises(sqlite3.IntegrityError):
            self._insert_hash(c, "A" * 64, "a" * 64, "v4")
        c.close()

    def test_wrong_length_rejected(self):
        """A5：长度非 64 → 拒绝。"""
        c = self._fresh_conn()
        with pytest.raises(sqlite3.IntegrityError):
            self._insert_hash(c, "a" * 63, "a" * 64, "v5")
        c.close()


# ===========================================================================
# A6: 数值表示规范化 — 100 与 100.0 不产生新版本
# ===========================================================================

class TestA6NumericNormalization:
    """A6 修复：统一数值表示，int/float 同值不制造内容修订。"""

    def test_int_and_float_same_version(self, db_conn):
        """A6：100 与 100.0 → 同一版本。"""
        conn, repo = db_conn
        v1 = repo.create_statement_version(
            secucode="000001.SZ", report_date="2024-03-31",
            table_type="financial_indicator",
            standardized_fields=json.dumps({"total_operate_income": 100}),
        )
        v2 = repo.create_statement_version(
            secucode="000001.SZ", report_date="2024-03-31",
            table_type="financial_indicator",
            standardized_fields=json.dumps({"total_operate_income": 100.0}),
        )
        assert v1 == v2
        assert conn.execute("SELECT COUNT(*) FROM statement_version").fetchone()[0] == 1

    def test_stored_fields_normalized(self, db_conn):
        """A6：存储的标准化字段被规范化为整型文本（与哈希一致）。"""
        conn, repo = db_conn
        vid = repo.create_statement_version(
            secucode="000001.SZ", report_date="2024-03-31",
            table_type="financial_indicator",
            standardized_fields=json.dumps({"total_operate_income": 100.0}),
        )
        row = conn.execute(
            "SELECT standardized_fields FROM statement_version WHERE version_id=?",
            (vid,),
        ).fetchone()
        # 存储为 100 而非 100.0
        assert row["standardized_fields"] == '{"total_operate_income":100}'

    def test_key_order_same_hash(self, db_conn):
        """A6：键序变化 → 同一版本（sort_keys 保证）。"""
        conn, repo = db_conn
        v1 = repo.create_statement_version(
            secucode="000001.SZ", report_date="2024-03-31",
            table_type="financial_indicator",
            standardized_fields=json.dumps({"a": 100, "b": 200}),
        )
        v2 = repo.create_statement_version(
            secucode="000001.SZ", report_date="2024-03-31",
            table_type="financial_indicator",
            standardized_fields=json.dumps({"b": 200, "a": 100}),
        )
        assert v1 == v2

    def test_value_change_creates_new_version(self, db_conn):
        """A6：真实值变化（100→120）→ 新版本。"""
        conn, repo = db_conn
        v1 = repo.create_statement_version(
            secucode="000001.SZ", report_date="2024-03-31",
            table_type="financial_indicator",
            standardized_fields=json.dumps({"total_operate_income": 100}),
        )
        v2 = repo.create_statement_version(
            secucode="000001.SZ", report_date="2024-03-31",
            table_type="financial_indicator",
            standardized_fields=json.dumps({"total_operate_income": 120}),
        )
        assert v1 != v2

    def test_non_integer_float_preserved(self, db_conn):
        """A6：非整数浮点（如 0.1675）不被截断。"""
        conn, repo = db_conn
        vid = repo.create_statement_version(
            secucode="000001.SZ", report_date="2024-03-31",
            table_type="financial_indicator",
            standardized_fields=json.dumps({"weightavg_roe": 0.1675}),
        )
        row = conn.execute(
            "SELECT standardized_fields FROM statement_version WHERE version_id=?",
            (vid,),
        ).fetchone()
        assert json.loads(row["standardized_fields"])["weightavg_roe"] == 0.1675


# ===========================================================================
# B1: 迁移当前投影与阶段一选值规则对齐
# ===========================================================================

@pytest.fixture
def stage1_db_factory():
    """构造阶段一形状的库（v1 基线 + raw_* 数据），迁移尚未运行。

    返回工厂函数：f(rows_by_table) -> (conn, db_path)。
    测试结束后统一关闭连接并删除临时文件。
    """
    created = []

    def _make(rows_by_table):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        conn = get_connection(db_path)
        m.migrate_v1_baseline(conn)
        for table, rows in rows_by_table.items():
            for row in rows:
                cols = ", ".join(row.keys())
                placeholders = ", ".join(["?"] * len(row))
                conn.execute(
                    f"INSERT INTO {table} ({cols}) VALUES ({placeholders})",
                    tuple(row.values()),
                )
        conn.commit()
        created.append((conn, db_path))
        return conn, db_path

    yield _make

    for conn, db_path in created:
        conn.close()
        Path(db_path).unlink(missing_ok=True)


def _raw_count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _stage1_selected(conn, raw_table, table_suffix, secucode, report_date):
    """真实调用阶段一已验收方法 deduplicate，返回该组的选中记录。"""
    from fin_feature.cleaning.dedup import deduplicate

    rows = conn.execute(f"SELECT * FROM {raw_table}").fetchall()
    result = deduplicate(Repository(conn), table_suffix, rows)
    chosen = [
        dict(r) for r in result
        if r["secucode"] == secucode and r["report_date"] == report_date
    ]
    assert len(chosen) == 1, f"阶段一应恰好选中一条，实际 {len(chosen)}"
    return chosen[0]


def _current_rows(conn, secucode, report_date, table_type):
    """取组内当前投影行（不设空条件跳过，断言组存在）。"""
    rows = conn.execute(
        """
        SELECT * FROM statement_version
        WHERE secucode = ? AND report_date = ? AND table_type = ?
        """,
        (secucode, report_date, table_type),
    ).fetchall()
    assert rows, f"组 ({secucode}, {report_date}, {table_type}) 应有迁移记录"
    current = [r for r in rows if r["is_superseded"] == 0]
    assert len(current) == 1, f"当前投影应恰好一条，实际 {len(current)}"
    return rows, current[0]


def _raw_value(row, field):
    return json.loads(row["raw_fields"]).get(field)


# 反例 1：未知公告日的记录采集更晚
COUNTEREXAMPLE_1 = [
    {"secucode": "000001.SZ", "security_code": "000001", "report_date": "2024-03-31",
     "notice_date": "2024-04-15", "entry_time": None, "crawled_at": "2024-05-20 10:00:00",
     "total_operate_income": 120.0},
    {"secucode": "000001.SZ", "security_code": "000001", "report_date": "2024-03-31",
     "notice_date": None, "entry_time": None, "crawled_at": "2024-06-20 10:00:00",
     "total_operate_income": 100.0},
]

# 反例 2：双方公告日未知，entry_time 与采集先后相反
COUNTEREXAMPLE_2 = [
    {"secucode": "000002.SZ", "security_code": "000002", "report_date": "2024-03-31",
     "notice_date": None, "entry_time": "2024-06-01 09:00:00",
     "crawled_at": "2024-04-20 10:00:00", "total_operate_income": 120.0},
    {"secucode": "000002.SZ", "security_code": "000002", "report_date": "2024-03-31",
     "notice_date": None, "entry_time": "2024-05-01 09:00:00",
     "crawled_at": "2024-06-30 10:00:00", "total_operate_income": 100.0},
]


class TestB1MigrationSelectionAlignment:
    """B1 修复：迁移当前投影读完整元信息（含 entry_time），复用阶段一选值规则。

    覆盖复审给出的两条真实方法反例，以及当前唯一性、历史保留、重跑幂等、
    歧义保留和插入顺序无关性。
    """

    def test_known_notice_not_superseded_by_unknown_notice_crawl_time(
        self, stage1_db_factory
    ):
        """反例 1：已发布公告的 120 不再被未知公告日、采集更晚的 100 顶替。

        旧投影用 COALESCE(notice_date, crawled_at) 排序：未知公告日的记录
        会因采集时间更晚而获得公告优先级。修复后未知公告不得借采集时间取胜。
        """
        conn, _ = stage1_db_factory({"raw_financial_indicator": COUNTEREXAMPLE_1})

        # 反例前提：未知公告日那条采集更晚（旧代码会因此选中它）
        known, unknown = COUNTEREXAMPLE_1
        assert unknown["crawled_at"] > known["crawled_at"]
        assert unknown["notice_date"] is None

        stage1 = _stage1_selected(
            conn, "raw_financial_indicator", "financial_indicator", "000001.SZ", "2024-03-31"
        )
        assert stage1["total_operate_income"] == 120.0, "阶段一应选已知公告日的 120"

        run_migrations(conn)

        rows, current = _current_rows(conn, "000001.SZ", "2024-03-31", "financial_indicator")
        assert len(rows) == 2, "两条历史记录都应保留"
        assert _raw_value(current, "total_operate_income") == 120.0
        assert _raw_value(current, "total_operate_income") == stage1["total_operate_income"]
        assert current["notice_date"] == "2024-04-15"

        superseded = [r for r in rows if r["is_superseded"] == 1]
        assert len(superseded) == 1
        assert _raw_value(superseded[0], "total_operate_income") == 100.0

    def test_unknown_notice_uses_entry_time_not_crawl_order(self, stage1_db_factory):
        """反例 2：双方公告日未知时按 entry_time 选值，不按采集先后。"""
        conn, _ = stage1_db_factory({"raw_financial_indicator": COUNTEREXAMPLE_2})

        later_crawled = max(COUNTEREXAMPLE_2, key=lambda r: r["crawled_at"])
        assert later_crawled["total_operate_income"] == 100.0, "采集更晚的是 100"
        latest_entry = max(COUNTEREXAMPLE_2, key=lambda r: r["entry_time"])
        assert latest_entry["total_operate_income"] == 120.0, "entry_time 更晚的是 120"

        stage1 = _stage1_selected(
            conn, "raw_financial_indicator", "financial_indicator", "000002.SZ", "2024-03-31"
        )
        assert stage1["total_operate_income"] == 120.0

        run_migrations(conn)

        rows, current = _current_rows(conn, "000002.SZ", "2024-03-31", "financial_indicator")
        assert len(rows) == 2
        assert _raw_value(current, "total_operate_income") == 120.0
        assert _raw_value(current, "total_operate_income") == stage1["total_operate_income"]
        # 不制造日期证据：公告日未知保持 NULL，不借用采集时间补齐
        assert all(r["notice_date"] is None for r in rows)

    def test_both_unknown_uses_deterministic_fallback_and_keeps_ambiguity(
        self, stage1_db_factory
    ):
        """公告日与 entry_time 都未知：按内容摘要确定性回退，并保留歧义记录。"""
        rows_in = [
            {"secucode": "000001.SZ", "security_code": "000001", "report_date": "2024-03-31",
             "notice_date": None, "entry_time": None, "crawled_at": "2024-06-30 10:00:00",
             "total_operate_income": 100.0},
            {"secucode": "000001.SZ", "security_code": "000001", "report_date": "2024-03-31",
             "notice_date": None, "entry_time": None, "crawled_at": "2024-04-20 10:00:00",
             "total_operate_income": 120.0},
        ]
        conn, _ = stage1_db_factory({"raw_financial_indicator": rows_in})

        stage1 = _stage1_selected(
            conn, "raw_financial_indicator", "financial_indicator", "000001.SZ", "2024-03-31"
        )

        run_migrations(conn)

        rows, current = _current_rows(conn, "000001.SZ", "2024-03-31", "financial_indicator")
        assert len(rows) == 2
        assert _raw_value(current, "total_operate_income") == stage1["total_operate_income"]

        # 每次观察时间取自该系统实际的采集时间，不冒充公告时间
        observed = sorted(r["observed_at"] for r in rows)
        assert observed == ["2024-04-20 10:00:00", "2024-06-30 10:00:00"]
        assert all(r["notice_date"] is None for r in rows)

        # 保留歧义：既不声称恢复真实修订先后，也不静默丢弃
        logs = conn.execute(
            "SELECT detail FROM clean_log WHERE action = 'dedup_ambiguous' "
            "AND detail LIKE ?",
            (f"{m.MIGRATION_AMBIGUITY_PREFIX}%",),
        ).fetchall()
        assert len(logs) == 1, "应保留一条迁移投影歧义记录"
        assert "不声称恢复真实修订先后" in logs[0][0]

    def test_selection_independent_of_insertion_order(self, stage1_db_factory):
        """同一分组两行内容不同时，投影结果与插入顺序无关。"""
        forward = stage1_db_factory({"raw_financial_indicator": COUNTEREXAMPLE_2})
        reversed_rows = list(reversed(COUNTEREXAMPLE_2))
        backward = stage1_db_factory({"raw_financial_indicator": reversed_rows})

        chosen = []
        for conn, _ in (forward, backward):
            run_migrations(conn)
            _, current = _current_rows(conn, "000002.SZ", "2024-03-31", "financial_indicator")
            chosen.append(_raw_value(current, "total_operate_income"))
        assert chosen[0] == chosen[1] == 120.0

    def test_current_projection_preserves_history_and_matches_stage1(
        self, stage1_db_factory
    ):
        """混合数据集：当前金额与阶段一一致、当前唯一、全部历史保留。"""
        fi_rows = COUNTEREXAMPLE_1 + COUNTEREXAMPLE_2 + [
            # 三次正常修订：按公告日选取最后一条
            {"secucode": "000003.SZ", "security_code": "000003", "report_date": "2024-06-30",
             "notice_date": "2024-07-15", "entry_time": None, "crawled_at": "2024-07-16 10:00:00",
             "total_operate_income": 10.0},
            {"secucode": "000003.SZ", "security_code": "000003", "report_date": "2024-06-30",
             "notice_date": "2024-08-15", "entry_time": None, "crawled_at": "2024-07-17 10:00:00",
             "total_operate_income": 30.0},
            {"secucode": "000003.SZ", "security_code": "000003", "report_date": "2024-06-30",
             "notice_date": "2024-07-20", "entry_time": None, "crawled_at": "2024-07-18 10:00:00",
             "total_operate_income": 20.0},
        ]
        bs_rows = [
            # 同一天、同 entry_time、公告日均未知（raw_* 主键含 notice_date，
            # 同日非空公告日的重复行由原始层主键阻止，这里用真实可存的形态）：
            # 按内容摘要选取并标歧义
            {"secucode": "000001.SZ", "security_code": "000001", "report_date": "2024-03-31",
             "notice_date": None, "entry_time": "2024-04-30 12:00:00",
             "crawled_at": "2024-05-01 10:00:00", "total_assets": 1000.0},
            {"secucode": "000001.SZ", "security_code": "000001", "report_date": "2024-03-31",
             "notice_date": None, "entry_time": "2024-04-30 12:00:00",
             "crawled_at": "2024-05-02 10:00:00", "total_assets": 1200.0},
        ]
        raw_counts = {
            "raw_financial_indicator": len(fi_rows),
            "raw_balance_sheet": len(bs_rows),
        }
        conn, _ = stage1_db_factory({
            "raw_financial_indicator": fi_rows,
            "raw_balance_sheet": bs_rows,
        })

        # 迁移前的当前值：真实调用阶段一方法
        stage1_fi = {}
        for secucode, report_date in [("000001.SZ", "2024-03-31"), ("000002.SZ", "2024-03-31"),
                                      ("000003.SZ", "2024-06-30")]:
            stage1_fi[(secucode, report_date)] = _stage1_selected(
                conn, "raw_financial_indicator", "financial_indicator", secucode, report_date
            )["total_operate_income"]
        stage1_bs = _stage1_selected(
            conn, "raw_balance_sheet", "balance_sheet", "000001.SZ", "2024-03-31"
        )["total_assets"]

        run_migrations(conn)

        # 1) 全部历史记录保留（raw_* 不删除，且 1:1 投影）
        for table, expected in raw_counts.items():
            assert _raw_count(conn, table) == expected, f"{table} 原始记录不应被删除"
        sv_count = conn.execute("SELECT COUNT(*) FROM statement_version").fetchone()[0]
        assert sv_count == sum(raw_counts.values()), "statement_version 应 1:1 保留全部历史"

        # 2) 当前金额与阶段一选值一致
        for (secucode, report_date), expected in stage1_fi.items():
            _, current = _current_rows(conn, secucode, report_date, "financial_indicator")
            assert _raw_value(current, "total_operate_income") == expected
        _, current_bs = _current_rows(conn, "000001.SZ", "2024-03-31", "balance_sheet")
        assert _raw_value(current_bs, "total_assets") == stage1_bs
        assert _raw_value(current_bs, "total_assets") == 1200.0, "阶段一按内容摘要选中 1200"

        # 3) 当前唯一性：每组恰好一条 is_superseded=0（不设空条件跳过）
        groups = conn.execute("""
            SELECT secucode, report_date, table_type, source_name,
                   SUM(CASE WHEN is_superseded = 0 THEN 1 ELSE 0 END) AS current_count
            FROM statement_version
            GROUP BY secucode, report_date, table_type, source_name
        """).fetchall()
        assert len(groups) == 4, "应有 4 个分组：3 个 FI + 1 个 BS"
        assert all(g["current_count"] == 1 for g in groups)

        # 4) 迁移验证指标与事实一致
        metrics = m.verify_migration(conn)
        assert metrics["sv_count"] == sv_count
        assert metrics["sv_current_count"] == 4
        assert metrics["sv_groups_not_single_current"] == 0
        assert metrics["migration_ambiguous_selections"] == 1  # BS 组同日同 entry_time

    def test_rerun_migrations_is_idempotent(self, stage1_db_factory):
        """重跑迁移不新增、不改写既有投影（幂等）。"""
        rows = COUNTEREXAMPLE_1 + COUNTEREXAMPLE_2
        conn, _ = stage1_db_factory({"raw_financial_indicator": rows})

        run_migrations(conn)
        snapshot_sql = """
            SELECT version_id, observation_seq, is_superseded, notice_date,
                   observed_at, raw_fields
            FROM statement_version ORDER BY version_id
        """
        before = [tuple(r) for r in conn.execute(snapshot_sql).fetchall()]
        ambiguity_before = conn.execute(
            "SELECT COUNT(*) FROM clean_log WHERE detail LIKE ?",
            (f"{m.MIGRATION_AMBIGUITY_PREFIX}%",),
        ).fetchone()[0]
        raw_before = _raw_count(conn, "raw_financial_indicator")

        run_migrations(conn)

        after = [tuple(r) for r in conn.execute(snapshot_sql).fetchall()]
        assert after == before, "重跑迁移不应改变或新增投影"
        assert _raw_count(conn, "raw_financial_indicator") == raw_before
        ambiguity_after = conn.execute(
            "SELECT COUNT(*) FROM clean_log WHERE detail LIKE ?",
            (f"{m.MIGRATION_AMBIGUITY_PREFIX}%",),
        ).fetchone()[0]
        assert ambiguity_after == ambiguity_before

        # 当前金额不变，仍与阶段一一致
        _, current = _current_rows(conn, "000001.SZ", "2024-03-31", "financial_indicator")
        assert _raw_value(current, "total_operate_income") == 120.0
