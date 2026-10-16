"""pytest 公共 fixture：临时数据库、内存配置。"""

import pytest

from fin_feature.storage.database import Database, get_connection
from fin_feature.storage.migrations import run_migrations
from fin_feature.storage.repository import Repository


@pytest.fixture
def db(tmp_path):
    """临时数据库，测试后自动清理。"""
    db_path = tmp_path / "test.db"
    database = Database(str(db_path))
    database.init()
    yield database
    database.close()


@pytest.fixture
def repo(db):
    """数据访问层，基于临时数据库。"""
    return Repository(db.conn)


@pytest.fixture
def evidence_env(tmp_path):
    """2B-1 真实证据环境：临时文件库 + 临时 raw_root（临时真实文件，非假路径）。"""
    from evidence_helpers import EvidenceEnv

    conn = get_connection(str(tmp_path / "evidence.db"))
    run_migrations(conn)
    env = EvidenceEnv(conn=conn, raw_root=str(tmp_path / "raw"))
    yield env
    conn.close()
