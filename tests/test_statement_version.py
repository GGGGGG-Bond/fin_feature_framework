"""statement_version 版本模型 CRUD 测试：版本创建、哈希比较、修订不覆盖证据、幂等。"""

import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from fin_feature.storage.database import get_connection
from fin_feature.storage.migrations import run_migrations
from fin_feature.storage.repository import Repository


@pytest.fixture
def repo():
    """创建带迁移的临时数据库和 Repository。"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    conn = get_connection(db_path)
    run_migrations(conn)
    repo = Repository(conn)
    yield repo, conn, db_path
    conn.close()
    Path(db_path).unlink(missing_ok=True)


class TestStatementVersionCrud:
    """statement_version CRUD 测试。"""

    def test_create_statement_version(self, repo):
        """版本创建成功，observation_seq 自增。"""
        r, conn, _ = repo

        version_id = r.create_statement_version(
            secucode="000001.SZ",
            report_date="2024-03-31",
            table_type="financial_indicator",
            standardized_fields=json.dumps({"total_operate_income": 1000000.0}),
            metric_hash="abc123",
            semantic_hash="abc123",
        )

        row = conn.execute(
            "SELECT * FROM statement_version WHERE version_id = ?", (version_id,)
        ).fetchone()

        assert row is not None
        assert row["secucode"] == "000001.SZ"
        assert row["report_date"] == "2024-03-31"
        assert row["observation_seq"] == 1
        assert row["is_superseded"] == 0

    def test_same_date_content_change_creates_new_version(self, repo):
        """同一日期内容变化 → 新版本，旧版本 is_superseded=1（T2-05，验收标准 3）。"""
        r, conn, _ = repo

        # 第一版
        v1 = r.create_statement_version(
            secucode="000001.SZ",
            report_date="2024-03-31",
            table_type="financial_indicator",
            standardized_fields=json.dumps({"total_operate_income": 1000000.0}),
            metric_hash="hash_v1",
            semantic_hash="hash_v1",
        )

        # 第二版（内容变化）
        v2 = r.create_statement_version(
            secucode="000001.SZ",
            report_date="2024-03-31",
            table_type="financial_indicator",
            standardized_fields=json.dumps({"total_operate_income": 1200000.0}),
            metric_hash="hash_v2",
            semantic_hash="hash_v2",
        )

        # 验证 v1 被替代
        row_v1 = conn.execute(
            "SELECT is_superseded, superseded_by FROM statement_version WHERE version_id = ?", (v1,)
        ).fetchone()
        assert row_v1["is_superseded"] == 1
        assert row_v1["superseded_by"] == v2

        # 验证 v2 是当前有效
        row_v2 = conn.execute(
            "SELECT is_superseded FROM statement_version WHERE version_id = ?", (v2,)
        ).fetchone()
        assert row_v2["is_superseded"] == 0

    def test_same_content_idempotent(self, repo):
        """同内容幂等：metric_hash + semantic_hash 相同不创建新版本（T2-06，验收标准 3）。"""
        r, conn, _ = repo

        # 第一次创建
        v1 = r.create_statement_version(
            secucode="000001.SZ",
            report_date="2024-03-31",
            table_type="financial_indicator",
            standardized_fields=json.dumps({"total_operate_income": 1000000.0}),
            metric_hash="same_hash",
            semantic_hash="same_hash",
        )

        # 第二次相同内容应返回相同 version_id（幂等）
        v2 = r.create_statement_version(
            secucode="000001.SZ",
            report_date="2024-03-31",
            table_type="financial_indicator",
            standardized_fields=json.dumps({"total_operate_income": 1000000.0}),
            metric_hash="same_hash",
            semantic_hash="same_hash",
        )

        # 幂等：v1 == v2
        assert v1 == v2

        # 只有一条版本记录
        rows = conn.execute(
            "SELECT COUNT(*) FROM statement_version WHERE secucode = ? AND report_date = ? AND table_type = ?",
            ("000001.SZ", "2024-03-31", "financial_indicator")
        ).fetchone()
        assert rows[0] == 1

    def test_get_latest_statement_version(self, repo):
        """get_latest_statement_version 返回最新非替代版本。"""
        r, conn, _ = repo

        # 创建多个版本
        r.create_statement_version(
            secucode="000001.SZ",
            report_date="2024-03-31",
            table_type="financial_indicator",
            standardized_fields=json.dumps({"total_operate_income": 1000000.0}),
            metric_hash="hash_v1",
            semantic_hash="hash_v1",
        )
        r.create_statement_version(
            secucode="000001.SZ",
            report_date="2024-03-31",
            table_type="financial_indicator",
            standardized_fields=json.dumps({"total_operate_income": 1200000.0}),
            metric_hash="hash_v2",
            semantic_hash="hash_v2",
        )

        latest = r.get_latest_statement_version(
            "000001.SZ", "2024-03-31", "financial_indicator"
        )

        assert latest is not None
        assert latest["is_superseded"] == 0
        assert latest["observation_seq"] == 2

    def test_observation_seq_unique_constraint(self, repo):
        """observation_seq 分组内唯一约束生效（重复序号写入被拒绝）。"""
        r, conn, _ = repo

        # 创建第一个版本
        r.create_statement_version(
            secucode="000001.SZ",
            report_date="2024-03-31",
            table_type="financial_indicator",
            standardized_fields=json.dumps({"total_operate_income": 1000000.0}),
            metric_hash="hash_v1",
            semantic_hash="hash_v1",
        )

        # 直接尝试插入重复 observation_seq（绕过 repository）
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("""
                INSERT INTO statement_version
                (version_id, secucode, report_date, table_type, source_name,
                 metric_hash, semantic_hash, observation_seq, is_superseded)
                VALUES ('test_v2', '000001.SZ', '2024-03-31', 'financial_indicator', 'eastmoney',
                 'hash_v2', 'hash_v2', 1, 0)
            """)
            conn.commit()


class TestSemanticHashChange:
    """语义哈希变化测试（A2 修复）。"""

    def test_currency_change_creates_new_version(self, repo):
        """口径变化（CNY→USD）→ semantic_hash 不同 → 新版本。"""
        r, conn, _ = repo

        # CNY 版本
        v1 = r.create_statement_version(
            secucode="000001.SZ",
            report_date="2024-03-31",
            table_type="financial_indicator",
            standardized_fields=json.dumps({"total_operate_income": 1000000.0}),
            metric_hash="hash_cny",
            semantic_hash="hash_cny",  # 实际应包含 currency
            currency="CNY",
        )

        # USD 版本（口径变化）
        v2 = r.create_statement_version(
            secucode="000001.SZ",
            report_date="2024-03-31",
            table_type="financial_indicator",
            standardized_fields=json.dumps({"total_operate_income": 150000.0}),  # USD 值
            metric_hash="hash_usd",  # 数值不同
            semantic_hash="hash_usd",  # 语义不同（CNY vs USD）
            currency="USD",
        )

        # 验证两个版本都存在
        rows = conn.execute("""
            SELECT version_id, currency, semantic_hash FROM statement_version
            WHERE secucode = '000001.SZ' AND report_date = '2024-03-31'
            ORDER BY observation_seq
        """).fetchall()

        assert len(rows) == 2
        assert rows[0]["currency"] == "CNY"
        assert rows[1]["currency"] == "USD"


class TestGetLatestVersionsForReport:
    """三表关联接口测试（O-1 修复）。"""

    def test_get_latest_versions_for_report(self, repo):
        """get_latest_versions_for_report 返回三表各自最新非替代版本。"""
        r, conn, _ = repo

        # 创建三表版本
        for table_type in ["financial_indicator", "income_statement", "balance_sheet"]:
            r.create_statement_version(
                secucode="000001.SZ",
                report_date="2024-03-31",
                table_type=table_type,
                standardized_fields=json.dumps({"value": 100}),
                metric_hash=f"hash_{table_type}",
                semantic_hash=f"hash_{table_type}",
            )

        result = r.get_latest_versions_for_report("000001.SZ", "2024-03-31")

        assert len(result) == 3
        assert "financial_indicator" in result
        assert "income_statement" in result
        assert "balance_sheet" in result

        for table_type, row in result.items():
            assert row["table_type"] == table_type
            assert row["is_superseded"] == 0
