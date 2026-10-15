"""feature_compute_log 不可变计算记录 CRUD 测试（A3 修复）。"""

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


class TestComputeLogCrud:
    """feature_compute_log CRUD 测试。"""

    def test_save_compute_log(self, repo):
        """写入计算记录，以 result_id 唯一标识。"""
        r, conn, _ = repo

        # 先创建 ingest_run（外键约束要求）
        run_id = r.create_ingest_run(source_name="eastmoney")

        result_id = r.save_compute_log(
            compute_run_id=run_id,
            secucode="000001.SZ",
            report_date="2024-03-31",
            feature_name="net_profit_margin",
            definition_version="stage1-v1",
            input_version_ids=json.dumps({
                "current": {"financial_indicator": "v1", "income_statement": "v2", "balance_sheet": "v3"},
                "year_ago": {"financial_indicator": "v4", "income_statement": "v5", "balance_sheet": "v6"},
            }),
            feature_value=0.05,
            not_computable_reason=None,
            code_commit="abc123",
            config_snapshot=json.dumps({"cleaning": {"outlier_thresholds": {}}}),
            as_of_date="2024-04-15",
        )

        row = conn.execute(
            "SELECT * FROM feature_compute_log WHERE result_id = ?", (result_id,)
        ).fetchone()

        assert row is not None
        assert row["result_id"] == result_id
        assert row["compute_run_id"] == run_id  # 实际是 create_ingest_run 返回的 UUID
        assert row["secucode"] == "000001.SZ"
        assert row["feature_name"] == "net_profit_margin"
        assert row["definition_version"] == "stage1-v1"
        assert row["feature_value"] == 0.05
        assert row["code_commit"] == "abc123"

    def test_get_compute_log(self, repo):
        """get_compute_log 按 result_id 读取 input_version_ids。"""
        r, conn, _ = repo

        # 先创建 ingest_run（外键约束要求）
        run_id = r.create_ingest_run(source_name="eastmoney")

        input_version_ids = json.dumps({
            "current": {"financial_indicator": "v1"},
            "year_ago": {"financial_indicator": "v2"},
        })

        result_id = r.save_compute_log(
            compute_run_id=run_id,
            secucode="000001.SZ",
            report_date="2024-03-31",
            feature_name="roe_change",
            definition_version="stage1-v1",
            input_version_ids=input_version_ids,
            feature_value=0.02,
            not_computable_reason=None,
            code_commit="abc123",
            config_snapshot=None,
            as_of_date="2024-04-15",
        )

        row = r.get_compute_log(result_id)

        assert row is not None
        assert row["input_version_ids"] == input_version_ids
        assert row["feature_value"] == 0.02

    def test_old_result_preserved_after_new_computation(self, repo):
        """新版本入库后旧 result_id 记录保留不覆盖（重放旧结果仍可定位旧输入）。"""
        r, conn, _ = repo

        # 先创建 ingest_run（外键约束要求）
        run_id_1 = r.create_ingest_run(source_name="eastmoney")
        run_id_2 = r.create_ingest_run(source_name="eastmoney")

        # 第一次计算
        result_id_1 = r.save_compute_log(
            compute_run_id=run_id_1,
            secucode="000001.SZ",
            report_date="2024-03-31",
            feature_name="net_profit_margin",
            definition_version="stage1-v1",
            input_version_ids=json.dumps({"current": {"financial_indicator": "v1"}}),
            feature_value=0.05,
            not_computable_reason=None,
            code_commit="abc123",
            config_snapshot=None,
            as_of_date="2024-04-15",
        )

        # 第二次计算（重算，不同输入）
        result_id_2 = r.save_compute_log(
            compute_run_id=run_id_2,
            secucode="000001.SZ",
            report_date="2024-03-31",
            feature_name="net_profit_margin",
            definition_version="stage1-v1",
            input_version_ids=json.dumps({"current": {"financial_indicator": "v2"}}),
            feature_value=0.06,
            not_computable_reason=None,
            code_commit="def456",
            config_snapshot=None,
            as_of_date="2024-04-20",
        )

        # 验证两条记录都存在
        row_1 = r.get_compute_log(result_id_1)
        row_2 = r.get_compute_log(result_id_2)

        assert row_1 is not None
        assert row_2 is not None
        assert row_1["feature_value"] == 0.05
        assert row_2["feature_value"] == 0.06
        assert row_1["code_commit"] == "abc123"
        assert row_2["code_commit"] == "def456"

        # 验证 result_id 唯一性
        rows = conn.execute("""
            SELECT result_id, COUNT(*) as cnt FROM feature_compute_log
            GROUP BY result_id HAVING cnt > 1
        """).fetchall()
        assert len(rows) == 0, "result_id 应唯一"

    def test_compute_log_not_computable(self, repo):
        """无法计算时记录 not_computable_reason。"""
        r, conn, _ = repo

        # 先创建 ingest_run（外键约束要求）
        run_id = r.create_ingest_run(source_name="eastmoney")

        result_id = r.save_compute_log(
            compute_run_id=run_id,
            secucode="000001.SZ",
            report_date="2024-03-31",
            feature_name="gross_margin_feat",
            definition_version="stage1-v1",
            input_version_ids=json.dumps({"current": {}}),
            feature_value=None,
            not_computable_reason="not_applicable",
            code_commit="abc123",
            config_snapshot=None,
            as_of_date="2024-04-15",
        )

        row = r.get_compute_log(result_id)

        assert row is not None
        assert row["feature_value"] is None
        assert row["not_computable_reason"] == "not_applicable"
