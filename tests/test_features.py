"""特征模块测试：计算正确性、除零保护、None 传播、注册机制。"""

from fin_feature.features.base import Feature, FeatureContext
from fin_feature.features.engine import _build_context, compute_all
from fin_feature.features.registry import FeatureRegistry
from fin_feature.features import profitability, growth, solvency  # noqa: F401 触发注册
from fin_feature.storage.repository import Repository


def test_safe_divide():
    """除零保护应返回 None。"""
    assert Feature.safe_divide(10, 2) == 5.0
    assert Feature.safe_divide(10, 0) is None
    assert Feature.safe_divide(None, 2) is None
    assert Feature.safe_divide(10, None) is None


def test_pct_change():
    """百分比变化率应正确计算。"""
    assert Feature.pct_change(120, 100) == 0.2
    assert Feature.pct_change(80, 100) == -0.2
    assert Feature.pct_change(100, 0) is None
    assert Feature.pct_change(None, 100) is None


def test_registry_register_and_get():
    """注册表应正确注册和查询特征。"""
    # profitability 等模块已 import，特征已注册
    all_features = FeatureRegistry.get_all()
    assert len(all_features) >= 10  # 三组共 11 个特征
    profit_features = FeatureRegistry.get_by_group("profitability")
    assert any(f.name == "net_profit_margin" for f in profit_features)
    assert FeatureRegistry.get_by_name("revenue_yoy") is not None


def test_net_profit_margin_calculation():
    """净利率 = 归母净利润 / 营业收入。"""
    feat = profitability.NetProfitMargin()
    ctx = FeatureContext(
        secucode="002501.SZ", report_date="2024-12-31",
        current={"parent_netprofit": 200.0, "total_operate_income": 1000.0},
        previous=None, year_ago=None,
    )
    assert feat.calculate(ctx) == 0.2  # 20%


def test_roe_change():
    """ROE 变化 = 当期 - 上期。"""
    feat = profitability.RoeChange()
    ctx = FeatureContext(
        secucode="002501.SZ", report_date="2024-12-31",
        current={"weightavg_roe": 16.0},
        previous={"weightavg_roe": 14.0}, year_ago=None,
    )
    assert feat.calculate(ctx) == 2.0  # +2个百分点


def test_roe_change_no_previous():
    """无上期数据时 ROE 变化应返回 None。"""
    feat = profitability.RoeChange()
    ctx = FeatureContext(
        secucode="002501.SZ", report_date="2024-12-31",
        current={"weightavg_roe": 16.0},
        previous=None, year_ago=None,
    )
    assert feat.calculate(ctx) is None


def test_revenue_yoy_uses_api_value():
    """营收同比应优先用 API 提供的增速值。"""
    feat = growth.RevenueYoY()
    ctx = FeatureContext(
        secucode="002501.SZ", report_date="2024-12-31",
        current={"revenue_yoy": 15.42},  # API 值，百分比
        previous=None, year_ago=None,
    )
    assert feat.calculate(ctx) == 0.1542  # 转为小数


def test_revenue_yoy_calculates_when_no_api():
    """API 值缺失时应自行计算同比。"""
    feat = growth.RevenueYoY()
    ctx = FeatureContext(
        secucode="002501.SZ", report_date="2024-12-31",
        current={"revenue_yoy": None, "total_operate_income": 1200.0},
        previous=None,
        year_ago={"total_operate_income": 1000.0},
    )
    assert feat.calculate(ctx) == 0.2


def test_equity_ratio():
    """权益比率 = 净资产 / 总资产。"""
    feat = solvency.EquityRatio()
    ctx = FeatureContext(
        secucode="002501.SZ", report_date="2024-12-31",
        current={"total_equity": 400.0, "total_assets": 1000.0},
        previous=None, year_ago=None,
    )
    assert feat.calculate(ctx) == 0.4


def test_compute_all(repo):
    """compute_all 应计算并写入特征。"""
    # 准备清洗数据
    repo.upsert_clean("financial_indicator", {
        "secucode": "002501.SZ", "report_date": "2024-12-31",
        "total_operate_income": 1000.0, "parent_netprofit": 200.0,
        "weightavg_roe": 16.0, "gross_margin": 30.0,
    })
    repo.upsert_clean("income_statement", {
        "secucode": "002501.SZ", "report_date": "2024-12-31",
        "total_operate_income": 1000.0, "parent_netprofit": 200.0,
    })
    repo.upsert_clean("balance_sheet", {
        "secucode": "002501.SZ", "report_date": "2024-12-31",
        "total_assets": 1000.0, "total_liabilities": 600.0,
        "total_equity": 400.0, "debt_asset_ratio": 60.0,
    })

    count = compute_all(repo)
    assert count > 0
    features = repo.get_features("002501.SZ", "2024-12-31")
    feature_names = [f["feature_name"] for f in features]
    assert "net_profit_margin" in feature_names
    assert "equity_ratio" in feature_names
    assert "gross_margin_feat" in feature_names
