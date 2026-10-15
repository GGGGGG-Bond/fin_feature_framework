"""特征模块测试：计算正确性、除零保护、None 传播、注册机制。

测试基于 stage1-v1 特征定义。
"""

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
    """百分比变化率应正确计算。

    stage1-v1: 比较基数为零或负时返回 None（non_positive_comparison_base）。
    不使用绝对分母。当期为负、基数为正时可计算负增长。
    """
    assert Feature.pct_change(120, 100) == 0.2
    assert Feature.pct_change(80, 100) == -0.2
    assert Feature.pct_change(100, 0) is None
    assert Feature.pct_change(None, 100) is None
    # 负基数返回 None（不使用绝对分母）
    assert Feature.pct_change(10, -10) is None
    # 负 current、正基数：(-5 - 10) / 10 = -1.5
    assert Feature.pct_change(-5, 10) == -1.5


def test_registry_register_and_get():
    """注册表应正确注册和查询特征。"""
    # profitability 等模块已 import，特征已注册
    all_features = FeatureRegistry.get_all()
    assert len(all_features) >= 10  # 三组共 11 个特征
    profit_features = FeatureRegistry.get_by_group("profitability")
    assert any(f.name == "net_profit_margin" for f in profit_features)
    assert FeatureRegistry.get_by_name("revenue_yoy") is not None


def test_net_profit_margin_calculation():
    """净利率 = 归母净利润 / 营业收入（小数）。"""
    feat = profitability.NetProfitMargin()
    ctx = FeatureContext(
        secucode="002501.SZ", report_date="2024-12-31",
        current={"parent_netprofit": 200.0, "total_operate_income": 1000.0},
        previous=None, year_ago=None,
    )
    assert feat.calculate(ctx) == 0.2  # 20% 小数


def test_roe_change():
    """ROE 变化 = 当期 - 去年同期（百分点）。

    stage1-v1: 使用去年同期（year_ago）而非上一期（previous）
    输入为标准化小数，输出为百分点：(0.12 - 0.10) * 100 = 2.0
    不猜测单位；输入由标准化入口保证为小数
    """
    feat = profitability.RoeChange()
    ctx = FeatureContext(
        secucode="002501.SZ", report_date="2024-12-31",
        current={"weightavg_roe": 0.12},  # 小数 12%
        previous={"weightavg_roe": 0.14},  # 不使用
        year_ago={"weightavg_roe": 0.10},  # 去年同期 10%
    )
    assert abs(feat.calculate(ctx) - 2.0) < 1e-9  # +2 个百分点


def test_roe_change_no_year_ago():
    """无去年同期数据时 ROE 变化应返回 None。"""
    feat = profitability.RoeChange()
    ctx = FeatureContext(
        secucode="002501.SZ", report_date="2024-12-31",
        current={"weightavg_roe": 0.16},
        previous={"weightavg_roe": 0.14},
        year_ago=None,
    )
    assert feat.calculate(ctx) is None


def test_roe_change_rate():
    """ROE 变化率 = (当期 - 去年同期) / 去年同期（小数）。

    stage1-v1: 使用去年同期（year_ago）；非正基数返回 None
    """
    feat = profitability.RoeChangeRate()
    ctx = FeatureContext(
        secucode="002501.SZ", report_date="2024-12-31",
        current={"weightavg_roe": 0.12},
        previous=None,
        year_ago={"weightavg_roe": 0.10},
    )
    assert abs(feat.calculate(ctx) - 0.2) < 1e-9  # (0.12-0.10)/0.10 = 0.2


def test_revenue_yoy_calculates():
    """营收同比 = (当期 - 去年同期) / |去年同期|（小数）。

    stage1-v1: 自算增速，不使用 API 值
    """
    feat = growth.RevenueYoY()
    ctx = FeatureContext(
        secucode="002501.SZ", report_date="2024-12-31",
        current={"total_operate_income": 1200.0},
        previous=None,
        year_ago={"total_operate_income": 1000.0},
    )
    assert feat.calculate(ctx) == 0.2


def test_revenue_yoy_no_comparison_base():
    """无去年同期或基数非正时应返回 None。"""
    feat = growth.RevenueYoY()
    # 无去年同期
    ctx1 = FeatureContext(
        secucode="002501.SZ", report_date="2024-12-31",
        current={"total_operate_income": 1200.0},
        previous=None, year_ago=None,
    )
    assert feat.calculate(ctx1) is None

    # 去年同期为 0
    ctx2 = FeatureContext(
        secucode="002501.SZ", report_date="2024-12-31",
        current={"total_operate_income": 1200.0},
        previous=None, year_ago={"total_operate_income": 0.0},
    )
    assert feat.calculate(ctx2) is None


def test_profit_yoy_calculates():
    """净利润同比 = (当期 - 去年同期) / |去年同期|（小数）。"""
    feat = growth.ProfitYoY()
    ctx = FeatureContext(
        secucode="002501.SZ", report_date="2024-12-31",
        current={"parent_netprofit": 120.0},
        previous=None,
        year_ago={"parent_netprofit": 100.0},
    )
    assert feat.calculate(ctx) == 0.2


def test_profit_yoy_negative_growth():
    """净利润从正转负应返回负增长。"""
    feat = growth.ProfitYoY()
    ctx = FeatureContext(
        secucode="002501.SZ", report_date="2024-12-31",
        current={"parent_netprofit": -5.0},
        previous=None,
        year_ago={"parent_netprofit": 10.0},
    )
    assert feat.calculate(ctx) == -1.5  # 10 到 -5 为 -150%


def test_equity_ratio():
    """权益比率 = 总权益 / 总资产（小数，可为负）。"""
    feat = solvency.EquityRatio()
    ctx = FeatureContext(
        secucode="002501.SZ", report_date="2024-12-31",
        current={"total_equity": 400.0, "total_assets": 1000.0},
        previous=None, year_ago=None,
    )
    assert feat.calculate(ctx) == 0.4

    # 权益为负
    ctx2 = FeatureContext(
        secucode="002501.SZ", report_date="2024-12-31",
        current={"total_equity": -20.0, "total_assets": 100.0},
        previous=None, year_ago=None,
    )
    assert feat.calculate(ctx2) == -0.2


def test_debt_asset_ratio():
    """资产负债率 = 总负债 / 总资产（小数，可超过 1）。

    stage1-v1: 使用上下文层预算的自算值（debt_asset_ratio 字段）。
    """
    feat = solvency.DebtAssetRatio()
    ctx = FeatureContext(
        secucode="002501.SZ", report_date="2024-12-31",
        current={"debt_asset_ratio": 0.6, "total_liabilities": 60.0, "total_assets": 100.0},
        previous=None, year_ago=None,
    )
    assert feat.calculate(ctx) == 0.6

    # 资不抵债
    ctx2 = FeatureContext(
        secucode="002501.SZ", report_date="2024-12-31",
        current={"debt_asset_ratio": 1.2, "total_liabilities": 120.0, "total_assets": 100.0},
        previous=None, year_ago=None,
    )
    assert feat.calculate(ctx2) == 1.2


def test_debt_asset_ratio_change():
    """资产负债率变化 = 当期 - 上期（百分点）。

    stage1-v1: 使用自算 L/A（小数），输出百分点：(0.60 - 0.55) * 100 = 5.0
    不猜测单位；debt_asset_ratio 由上下文层预算
    """
    feat = solvency.DebtAssetRatioChange()
    ctx = FeatureContext(
        secucode="002501.SZ", report_date="2024-12-31",
        current={"debt_asset_ratio": 0.60},
        previous={"debt_asset_ratio": 0.55},
        year_ago=None,
    )
    assert abs(feat.calculate(ctx) - 5.0) < 1e-9  # 5 个百分点


def test_compute_all(repo):
    """compute_all 应计算并写入特征。"""
    # 准备清洗数据（使用 stage1-v1 标准单位：小数）
    repo.upsert_clean("financial_indicator", {
        "secucode": "002501.SZ", "report_date": "2024-12-31",
        "total_operate_income": 1000.0, "parent_netprofit": 200.0,
        "weightavg_roe": 0.16, "gross_margin": 0.30,
    })
    repo.upsert_clean("income_statement", {
        "secucode": "002501.SZ", "report_date": "2024-12-31",
        "total_operate_income": 1000.0, "parent_netprofit": 200.0,
    })
    repo.upsert_clean("balance_sheet", {
        "secucode": "002501.SZ", "report_date": "2024-12-31",
        "total_assets": 1000.0, "total_liabilities": 600.0,
        "total_equity": 400.0, "debt_asset_ratio": 0.6,
    })

    count = compute_all(repo)
    assert count > 0
    features = repo.get_features("002501.SZ", "2024-12-31")
    feature_names = [f["feature_name"] for f in features]
    assert "net_profit_margin" in feature_names
    assert "equity_ratio" in feature_names
    assert "gross_margin_feat" in feature_names


# ---------------------------------------------------------------------------
# 单季环比、负基数、定义版本回归测试（T20-T25, T34）
# ---------------------------------------------------------------------------

def test_profit_yoy_negative_current_positive_base():
    """T20: 当期 -5、基数 10 → profit_yoy = -1.5（负增长合法）。"""
    feat = growth.ProfitYoY()
    ctx = FeatureContext(
        secucode="002501.SZ", report_date="2024-12-31",
        current={"parent_netprofit": -5.0},
        previous=None,
        year_ago={"parent_netprofit": 10.0},
    )
    assert feat.calculate(ctx) == -1.5


def test_profit_yoy_non_positive_base():
    """T20: 比较基数为 0 或负 → None（non_positive_comparison_base）。"""
    feat = growth.ProfitYoY()
    # 基数为 0
    ctx1 = FeatureContext(
        secucode="002501.SZ", report_date="2024-12-31",
        current={"parent_netprofit": 120.0},
        previous=None,
        year_ago={"parent_netprofit": 0.0},
    )
    assert feat.calculate(ctx1) is None

    # 基数为负
    ctx2 = FeatureContext(
        secucode="002501.SZ", report_date="2024-12-31",
        current={"parent_netprofit": 120.0},
        previous=None,
        year_ago={"parent_netprofit": -10.0},
    )
    assert feat.calculate(ctx2) is None


def test_revenue_yoy_non_positive_base():
    """T20: 营收同比基数为负 → None。"""
    feat = growth.RevenueYoY()
    ctx = FeatureContext(
        secucode="002501.SZ", report_date="2024-12-31",
        current={"total_operate_income": 1200.0},
        previous=None,
        year_ago={"total_operate_income": -100.0},
    )
    assert feat.calculate(ctx) is None


def test_revenue_qoq_uses_single_quarter():
    """T21: Q1=100、半年=250 → Q2单季=150，环比=0.5（不能按累计得 1.5）。

    使用 current_sq/previous_sq 单季值。
    """
    feat = growth.RevenueQoQ()
    ctx = FeatureContext(
        secucode="002501.SZ", report_date="2024-06-30",
        current={"total_operate_income": 250.0},  # 半年累计
        previous={"total_operate_income": 100.0},  # Q1 累计
        year_ago=None,
        current_sq={"total_operate_income": 150.0},   # Q2 单季 = 250-100
        previous_sq={"total_operate_income": 100.0},  # Q1 单季
    )
    assert feat.calculate(ctx) == 0.5  # (150-100)/100 = 0.5


def test_revenue_qoq_missing_single_quarter():
    """T23: 缺单季值 → None（missing_comparison_period）。"""
    feat = growth.RevenueQoQ()
    ctx = FeatureContext(
        secucode="002501.SZ", report_date="2024-06-30",
        current={"total_operate_income": 250.0},
        previous={"total_operate_income": 100.0},
        year_ago=None,
        current_sq=None,  # 缺单季
        previous_sq=None,
    )
    assert feat.calculate(ctx) is None


def test_profit_qoq_uses_single_quarter():
    """T21: 净利润环比使用单季值。"""
    feat = growth.ProfitQoQ()
    ctx = FeatureContext(
        secucode="002501.SZ", report_date="2024-06-30",
        current={"parent_netprofit": 50.0},
        previous={"parent_netprofit": 20.0},
        year_ago=None,
        current_sq={"parent_netprofit": 30.0},  # Q2 单季
        previous_sq={"parent_netprofit": 20.0},  # Q1 单季
    )
    assert feat.calculate(ctx) == 0.5  # (30-20)/20 = 0.5


def test_current_ratio_unknown_unit():
    """T28: current_ratio 单位未知 → None（unknown_unit）。"""
    feat = solvency.CurrentRatioChange()
    ctx = FeatureContext(
        secucode="002501.SZ", report_date="2024-12-31",
        current={"current_ratio": None},  # 标准化后 None（unknown_unit）
        previous={"current_ratio": 2.0},
        year_ago=None,
    )
    assert feat.calculate(ctx) is None


def test_current_ratio_known_unit():
    """T27: current_ratio 单位明确（times）→ 正常计算变化。"""
    feat = solvency.CurrentRatioChange()
    ctx = FeatureContext(
        secucode="002501.SZ", report_date="2024-12-31",
        current={"current_ratio": 2.5},
        previous={"current_ratio": 2.0},
        year_ago=None,
    )
    assert feat.calculate(ctx) == 0.5  # 2.5 - 2.0 = 0.5


def test_debt_asset_ratio_change_uses_self_computed():
    """T26: 负债率变化使用自算 L/A，不猜测单位。"""
    feat = solvency.DebtAssetRatioChange()
    ctx = FeatureContext(
        secucode="002501.SZ", report_date="2024-12-31",
        current={"debt_asset_ratio": 0.60},  # 自算 L/A
        previous={"debt_asset_ratio": 0.55},
        year_ago=None,
    )
    assert abs(feat.calculate(ctx) - 5.0) < 1e-9  # (0.60-0.55)*100 = 5.0 个百分点


def test_compute_all_marks_definition_version(repo):
    """compute_all 写入的特征应标记 definition_version=stage1-v1。"""
    repo.upsert_clean("financial_indicator", {
        "secucode": "002501.SZ", "report_date": "2024-12-31",
        "total_operate_income": 1000.0, "parent_netprofit": 200.0,
        "weightavg_roe": 0.16, "gross_margin": 0.30,
    })
    repo.upsert_clean("income_statement", {
        "secucode": "002501.SZ", "report_date": "2024-12-31",
        "total_operate_income": 1000.0, "parent_netprofit": 200.0,
    })
    repo.upsert_clean("balance_sheet", {
        "secucode": "002501.SZ", "report_date": "2024-12-31",
        "total_assets": 1000.0, "total_liabilities": 600.0,
        "total_equity": 400.0,
    })

    compute_all(repo)

    rows = repo.conn.execute(
        "SELECT DISTINCT definition_version FROM features"
    ).fetchall()
    versions = [r[0] for r in rows]
    assert "stage1-v1" in versions
    assert all(v == "stage1-v1" for v in versions if v is not None)

