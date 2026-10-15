"""定义版本隔离测试：definition_version 标记、查询过滤、legacy 拒覆盖。

对应契约 T34。
"""

from fin_feature.features.engine import DEFINITION_VERSION
from fin_feature.storage.database import Database
from fin_feature.storage.repository import Repository


def test_definition_version_constant():
    """引擎定义版本常量为 stage1-v1。"""
    assert DEFINITION_VERSION == "stage1-v1"


def test_upsert_feature_marks_version(repo):
    """新写入特征应标记 definition_version。"""
    repo.upsert_feature("002501.SZ", "2024-12-31", "roe_change", 2.0, "profitability")
    rows = repo.conn.execute(
        "SELECT definition_version FROM features WHERE secucode = ? AND feature_name = ?",
        ("002501.SZ", "roe_change"),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "stage1-v1"


def test_get_features_filters_by_version(repo):
    """T34: 查询只返回 stage1-v1，不混读 legacy。"""
    # 写入 legacy 行（无 definition_version）——用不同公司避免触发拒覆盖
    repo.conn.execute(
        "INSERT INTO features (secucode, report_date, feature_name, feature_value, feature_group) "
        "VALUES (?, ?, ?, ?, ?)",
        ("LEGACY.SZ", "2024-12-31", "gross_margin_feat", 30.0, "profitability"),
    )
    # 写入 stage1-v1 行
    repo.upsert_feature("002501.SZ", "2024-12-31", "gross_margin_feat", 0.3, "profitability")
    repo.conn.commit()

    # 查询 002501.SZ 应只返回 stage1-v1（0.3）
    rows = repo.get_features("002501.SZ", "2024-12-31")
    assert len(rows) == 1
    assert rows[0]["feature_value"] == 0.3
    assert rows[0]["definition_version"] == "stage1-v1"

    # 查询 LEGACY.SZ 应返回 0 行（legacy 被过滤）
    legacy_rows = repo.get_features("LEGACY.SZ", "2024-12-31")
    assert len(legacy_rows) == 0


def test_legacy_overwrite_rejected(repo):
    """T34: 同主键 legacy 行被 stage1-v1 覆盖时默认拒写。"""
    # 写入 legacy 行
    repo.conn.execute(
        "INSERT INTO features (secucode, report_date, feature_name, feature_value, feature_group) "
        "VALUES (?, ?, ?, ?, ?)",
        ("002501.SZ", "2024-12-31", "roe_change", 99.0, "profitability"),
    )
    repo.conn.commit()

    # 尝试用 stage1-v1 覆盖 → 应被拒绝
    repo.upsert_feature("002501.SZ", "2024-12-31", "roe_change", 2.0, "profitability")

    # legacy 行不变
    row = repo.conn.execute(
        "SELECT feature_value, definition_version FROM features "
        "WHERE secucode = ? AND feature_name = ?",
        ("002501.SZ", "roe_change"),
    ).fetchone()
    assert row[0] == 99.0  # 旧值保留
    assert row[1] is None   # 仍为 legacy


def test_idempotent_column_migration(tmp_path):
    """已有库幂等加列 definition_version。"""
    db_path = str(tmp_path / "test.db")
    db = Database(db_path)
    db.init()
    db.close()

    # 再次 init 应幂等（不报错）
    db2 = Database(db_path)
    db2.init()
    # 验证列存在
    cols = {r[1] for r in db2.conn.execute("PRAGMA table_info(features)").fetchall()}
    assert "definition_version" in cols
    db2.close()
