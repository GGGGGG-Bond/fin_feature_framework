"""ingest_run 采集批次 CRUD 测试：批次状态跟踪、失败不伪装成功。"""

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


class TestIngestRunCrud:
    """ingest_run CRUD 测试。"""

    def test_create_ingest_run(self, repo):
        """创建采集批次返回 run_id。"""
        r, conn, _ = repo

        run_id = r.create_ingest_run(
            source_name="eastmoney",
            request_scope='{"report_names": ["RPT_LICO_FN_CPD"], "recent_periods": 4}',
            crawler_version="v1.0.0",
        )

        row = conn.execute(
            "SELECT * FROM ingest_run WHERE run_id = ?", (run_id,)
        ).fetchone()

        assert row is not None
        assert row["source_name"] == "eastmoney"
        assert row["status"] == "running"
        assert row["crawler_version"] == "v1.0.0"

    def test_finish_ingest_run_success(self, repo):
        """更新批次状态为 success。"""
        r, conn, _ = repo

        run_id = r.create_ingest_run(source_name="eastmoney")
        r.finish_ingest_run(run_id, "success", failed_units=0)

        row = conn.execute(
            "SELECT status, failed_units, ended_at FROM ingest_run WHERE run_id = ?",
            (run_id,),
        ).fetchone()

        assert row["status"] == "success"
        assert row["failed_units"] == 0
        assert row["ended_at"] is not None

    def test_finish_ingest_run_partial_failure(self, repo):
        """部分失败 status='partial_failure'（T2-08，验收标准 4）。"""
        r, conn, _ = repo

        run_id = r.create_ingest_run(source_name="eastmoney")
        r.finish_ingest_run(run_id, "partial_failure", failed_units=3, detail="3 个公司采集失败")

        row = conn.execute(
            "SELECT status, failed_units, detail FROM ingest_run WHERE run_id = ?",
            (run_id,),
        ).fetchone()

        assert row["status"] == "partial_failure"
        assert row["failed_units"] == 3
        assert row["detail"] == "3 个公司采集失败"

    def test_finish_ingest_run_all_failed(self, repo):
        """全部失败 status='failed'（T2-07，验收标准 4）。"""
        r, conn, _ = repo

        run_id = r.create_ingest_run(source_name="eastmoney")
        r.finish_ingest_run(run_id, "failed", failed_units=100, detail="全部采集失败")

        row = conn.execute(
            "SELECT status, failed_units FROM ingest_run WHERE run_id = ?",
            (run_id,),
        ).fetchone()

        assert row["status"] == "failed"
        assert row["failed_units"] == 100


class TestIngestRunStatusTransition:
    """批次状态转换测试。"""

    def test_running_to_success(self, repo):
        """running → success 状态转换正确。"""
        r, conn, _ = repo

        run_id = r.create_ingest_run(source_name="eastmoney")

        # 初始状态
        row = conn.execute("SELECT status FROM ingest_run WHERE run_id = ?", (run_id,)).fetchone()
        assert row["status"] == "running"

        # 完成
        r.finish_ingest_run(run_id, "success")
        row = conn.execute("SELECT status FROM ingest_run WHERE run_id = ?", (run_id,)).fetchone()
        assert row["status"] == "success"

    def test_running_to_failed(self, repo):
        """running → failed 状态转换正确。"""
        r, conn, _ = repo

        run_id = r.create_ingest_run(source_name="eastmoney")
        r.finish_ingest_run(run_id, "failed", failed_units=10)

        row = conn.execute("SELECT status FROM ingest_run WHERE run_id = ?", (run_id,)).fetchone()
        assert row["status"] == "failed"

    def test_failed_units_counted(self, repo):
        """failed_units 统计正确。"""
        r, conn, _ = repo

        run_id = r.create_ingest_run(source_name="eastmoney")
        r.finish_ingest_run(run_id, "partial_failure", failed_units=5)

        row = conn.execute("SELECT failed_units FROM ingest_run WHERE run_id = ?", (run_id,)).fetchone()
        assert row["failed_units"] == 5
