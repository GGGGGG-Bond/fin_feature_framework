"""回测框架（分组分析）测试：分组逻辑、组间差异、数据不足处理。

stage1-v1: 输出为盈利增长分析，非投资收益回测。
"""

from fin_feature.addons.backtest import format_report, run_backtest, BacktestResult
from fin_feature.storage.repository import Repository


def _seed_features(repo, formation_date="2024-12-31"):
    """构造分组日的特征数据 + 下期收益代理。"""
    # 10 家公司，特征值 1-10，下期 profit_yoy 递增
    for i in range(1, 11):
        secucode = f"00000{i}.SZ"
        repo.upsert_feature(secucode, "2024-12-31", "roe_change", float(i), "profitability")
        # 下期（2025-03-31）的 profit_yoy 作为收益代理
        repo.upsert_feature(secucode, "2025-03-31", "profit_yoy", float(i) * 0.1, "growth")


def test_backtest_5_groups(repo):
    """10 个样本分 5 组，每组 2 个。"""
    _seed_features(repo)
    result = run_backtest(repo, "roe_change", "2024-12-31", n_groups=5)
    assert result.total_samples == 10
    assert len(result.group_stats) == 5
    # 每组 2 个样本
    for stat in result.group_stats:
        assert stat["size"] == 2


def test_backtest_long_short():
    """组间差异 = 最高组平均 - 最低组平均。"""
    from fin_feature.storage.database import Database
    from fin_feature.storage.repository import Repository
    import tempfile
    import os

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = Database(db_path)
        db.init()
        repo = Repository(db.conn)
        _seed_features(repo)
        result = run_backtest(repo, "roe_change", "2024-12-31", n_groups=5)
        # 最低组特征值 1,2 -> 收益 0.1,0.2 -> 平均 0.15
        # 最高组特征值 9,10 -> 收益 0.9,1.0 -> 平均 0.95
        assert result.long_short is not None
        assert abs(result.long_short - 0.8) < 0.001
        db.close()
    finally:
        db.close()
        os.unlink(db_path)


def test_backtest_insufficient_samples(repo):
    """样本不足时应返回空结果。"""
    repo.upsert_feature("000001.SZ", "2024-12-31", "roe_change", 1.0, "profitability")
    result = run_backtest(repo, "roe_change", "2024-12-31", n_groups=5)
    assert result.total_samples == 1
    assert len(result.group_stats) == 0  # 无法分组


def test_backtest_no_data(repo):
    """无数据时应返回空结果。"""
    result = run_backtest(repo, "roe_change", "2024-12-31", n_groups=5)
    assert result.total_samples == 0
    assert len(result.group_stats) == 0


def test_format_report():
    """报告格式化应包含关键信息。

    stage1-v1: 使用"组间差异"和"盈利增长"术语，非"多空收益"。
    """
    result = BacktestResult(
        feature_name="roe_change", formation_date="2024-12-31", n_groups=5,
        total_samples=10,
        group_stats=[
            {"group": 1, "size": 2, "valid_returns": 2, "avg_return": 0.15,
             "feature_range": (1.0, 2.0)},
        ],
        long_short=0.8,
    )
    text = format_report(result)
    assert "roe_change" in text
    assert "2024-12-31" in text
    assert "组间差异" in text  # stage1-v1 术语
    assert "盈利增长" in text  # stage1-v1 术语
