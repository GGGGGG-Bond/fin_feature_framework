"""pytest 公共 fixture：临时数据库、内存配置。"""

import pytest

from fin_feature.storage.database import Database
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
