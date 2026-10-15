"""特征模块测试：计算正确性、除零保护、None 传播、注册机制。

测试基于 stage1-v1 特征定义。
"""

from fin_feature.features.base import Feature, FeatureContext
from fin_feature.features.engine import _build_context, compute_all
from fin_feature.features.registry import FeatureRegistry
from fin_feature.features.semantic import merge_three_tables_semantic
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


# ---------------------------------------------------------------------------
# 合法输入条件测试（P1 #5：负资产、负营收仍生成有限比率）
# ---------------------------------------------------------------------------

def test_net_profit_margin_zero_revenue_returns_none():
    """P1 #5: 营收为 0（分母非正）→ None（non_positive_base）。"""
    feat = profitability.NetProfitMargin()
    ctx = FeatureContext(
        secucode="002501.SZ", report_date="2024-12-31",
        current={"parent_netprofit": 200.0, "total_operate_income": 0.0},
        previous=None, year_ago=None,
    )
    assert feat.calculate(ctx) is None


def test_net_profit_margin_negative_revenue_returns_none():
    """P1 #5: 营收为负（分母非正）→ None。"""
    feat = profitability.NetProfitMargin()
    ctx = FeatureContext(
        secucode="002501.SZ", report_date="2024-12-31",
        current={"parent_netprofit": 200.0, "total_operate_income": -1000.0},
        previous=None, year_ago=None,
    )
    assert feat.calculate(ctx) is None


def test_equity_ratio_zero_assets_returns_none():
    """P1 #5: 总资产为 0（分母非正）→ None。"""
    feat = solvency.EquityRatio()
    ctx = FeatureContext(
        secucode="002501.SZ", report_date="2024-12-31",
        current={"total_equity": 400.0, "total_assets": 0.0},
        previous=None, year_ago=None,
    )
    assert feat.calculate(ctx) is None


def test_equity_ratio_negative_assets_returns_none():
    """P1 #5: 总资产为负（非法负资产）→ None。"""
    feat = solvency.EquityRatio()
    ctx = FeatureContext(
        secucode="002501.SZ", report_date="2024-12-31",
        current={"total_equity": 400.0, "total_assets": -1000.0},
        previous=None, year_ago=None,
    )
    assert feat.calculate(ctx) is None


def test_debt_asset_ratio_negative_assets_returns_none():
    """P1 #5: 负资产时 debt_asset_ratio 应为 None（引擎层预算）。"""
    from fin_feature.features.engine import _compute_self_calculated_ratios
    std_values = {"total_liabilities": 600.0, "total_assets": -1000.0}
    result = _compute_self_calculated_ratios(std_values)
    assert result["debt_asset_ratio"] is None


def test_debt_asset_ratio_negative_liabilities_returns_none():
    """P1 #5: 负负债时 debt_asset_ratio 应为 None。"""
    from fin_feature.features.engine import _compute_self_calculated_ratios
    std_values = {"total_liabilities": -600.0, "total_assets": 1000.0}
    result = _compute_self_calculated_ratios(std_values)
    assert result["debt_asset_ratio"] is None


def test_debt_asset_ratio_valid_positive_values():
    """P1 #5: 正常正资产、非负负债 → 正常计算。"""
    from fin_feature.features.engine import _compute_self_calculated_ratios
    std_values = {"total_liabilities": 600.0, "total_assets": 1000.0}
    result = _compute_self_calculated_ratios(std_values)
    assert result["debt_asset_ratio"] == 0.6


# ---------------------------------------------------------------------------
# pct_change reason_code 区分测试
# ---------------------------------------------------------------------------

def test_pct_change_with_reason_missing_input():
    """pct_change_with_reason: 输入缺失 → (None, missing_input)。"""
    value, reason = Feature.pct_change_with_reason(None, 100.0)
    assert value is None
    assert reason == "missing_input"

    value, reason = Feature.pct_change_with_reason(120.0, None)
    assert value is None
    assert reason == "missing_input"


def test_pct_change_with_reason_non_positive_base():
    """pct_change_with_reason: 基数非正 → (None, non_positive_comparison_base)。"""
    value, reason = Feature.pct_change_with_reason(120.0, 0.0)
    assert value is None
    assert reason == "non_positive_comparison_base"

    value, reason = Feature.pct_change_with_reason(120.0, -10.0)
    assert value is None
    assert reason == "non_positive_comparison_base"


def test_pct_change_with_reason_valid():
    """pct_change_with_reason: 正常计算 → (值, None)。"""
    value, reason = Feature.pct_change_with_reason(120.0, 100.0)
    assert value == 0.2
    assert reason is None


# ---------------------------------------------------------------------------
# 语义可比性门控测试（P1 #1：跨周期来源不一致）
# ---------------------------------------------------------------------------

def test_period_comparability_different_source_tables(repo):
    """P1 #1: 当期与去年同期同一字段来源表不同 → incomparable_basis 诊断且拒算。

    审阅要求：不能以"不崩溃、诊断是 list"作为门禁验收；必须断言具体增长值被拒绝。
    """
    # 当期：营收来自 IS
    repo.upsert_clean("income_statement", {
        "secucode": "002501.SZ", "report_date": "2024-12-31",
        "total_operate_income": 1200.0, "parent_netprofit": 200.0,
    })
    # 去年同期：IS 缺失，营收来自 FI 回退（但默认拒绝回退 → None）
    # 这里构造 IS 有值但来源不同的情况：去年同期只有 FI
    repo.upsert_clean("financial_indicator", {
        "secucode": "002501.SZ", "report_date": "2023-12-31",
        "total_operate_income": 1000.0, "parent_netprofit": 150.0,
        "weightavg_roe": 0.10,
    })
    # 当期也写 FI（这样 weightavg_roe 在两期都来自 FI）
    repo.upsert_clean("financial_indicator", {
        "secucode": "002501.SZ", "report_date": "2024-12-31",
        "weightavg_roe": 0.12,
    })

    ctx = _build_context(repo, "002501.SZ", "2024-12-31")
    assert ctx is not None
    # weightavg_roe 两期都来自 FI，但口径未验证 → 应阻断
    if ctx.diagnostics:
        incomparable = [
            d for d in ctx.diagnostics
            if d.get("reason_code") == "incomparable_basis"
        ]
        assert len(incomparable) >= 1
    # 去年同期营收因 IS 缺失且回退被拒 → None，revenue_yoy 应为 None
    feat = growth.RevenueYoY()
    assert feat.calculate(ctx) is None


def test_period_comparability_same_source_still_blocks(repo):
    """P1 #1: 来源表相同但口径未验证 → 仍阻断并输出 incomparable_basis。

    审阅更正：来源相同不构成财务可比性证据，阶段 1 unknown 时拒算。
    """
    # 两期都有 IS 营收（来源一致）
    repo.upsert_clean("income_statement", {
        "secucode": "002501.SZ", "report_date": "2024-12-31",
        "total_operate_income": 1200.0, "parent_netprofit": 200.0,
    })
    repo.upsert_clean("income_statement", {
        "secucode": "002501.SZ", "report_date": "2023-12-31",
        "total_operate_income": 1000.0, "parent_netprofit": 150.0,
    })

    ctx = _build_context(repo, "002501.SZ", "2024-12-31")
    assert ctx is not None
    # 同表但口径未验证 → 应阻断
    assert ctx.diagnostics is not None
    incomparable = [
        d for d in ctx.diagnostics
        if d.get("reason_code") == "incomparable_basis"
        and d.get("field") == "total_operate_income"
    ]
    assert len(incomparable) == 1  # 来源相同但仍阻断
    # revenue_yoy 应为 None（去年同期营收被阻断置 None）
    feat = growth.RevenueYoY()
    assert feat.calculate(ctx) is None


def test_period_comparability_explicit_comparable_allows(repo):
    """P1 #1: 受控 fixture 显式声明可比 → 放行计算。

    使用 merge_three_tables_semantic_with_fallback_flag(allow_fallback=True)
    标记 comparable=True，跨期可比性检查放行。
    """
    from fin_feature.features.semantic import merge_three_tables_semantic_with_fallback_flag

    # 当期 IS 有营收
    repo.upsert_clean("income_statement", {
        "secucode": "002501.SZ", "report_date": "2024-12-31",
        "total_operate_income": 1200.0, "parent_netprofit": 200.0,
    })
    # 去年同期 IS 缺失，FI 有营收（回退源）
    repo.upsert_clean("financial_indicator", {
        "secucode": "002501.SZ", "report_date": "2023-12-31",
        "total_operate_income": 1000.0, "parent_netprofit": 150.0,
    })

    # 用 allow_fallback=True 合并去年同期（标记 comparable）
    ya_merged, _ = merge_three_tables_semantic_with_fallback_flag(
        repo, "002501.SZ", "2023-12-31", allow_fallback=True
    )
    ya_sources = ya_merged.get("__sources__", {})
    # 回退字段应标记 comparable=True
    assert ya_sources["total_operate_income"].comparable is True


def test_sources_preserved_in_context(repo):
    """P1 #1: __sources__ 应贯穿到计算上下文中。"""
    repo.upsert_clean("income_statement", {
        "secucode": "002501.SZ", "report_date": "2024-12-31",
        "total_operate_income": 1200.0, "parent_netprofit": 200.0,
    })
    repo.upsert_clean("balance_sheet", {
        "secucode": "002501.SZ", "report_date": "2024-12-31",
        "total_assets": 1000.0, "total_liabilities": 600.0,
        "total_equity": 400.0,
    })

    ctx = _build_context(repo, "002501.SZ", "2024-12-31")
    assert ctx is not None
    # __sources__ 应保留在 current 中
    assert "__sources__" in ctx.current
    sources = ctx.current["__sources__"]
    assert "total_operate_income" in sources
    assert sources["total_operate_income"].table_suffix == "income_statement"


def test_incomparable_blocks_revenue_yoy_value(repo):
    """P1 #1: 已报不可比的字段不得产生数值。

    审阅复现：当期 120 标 IS、比较期 100 标 FI，检查函数生成 incomparable_basis，
    RevenueYoY.calculate 仍输出 0.2 是错误的。修复后应为 None。
    """
    # 当期 IS 营收
    repo.upsert_clean("income_statement", {
        "secucode": "002501.SZ", "report_date": "2024-12-31",
        "total_operate_income": 120.0, "parent_netprofit": 20.0,
    })
    # 去年同期 IS 缺失，FI 有营收（回退被拒 → None）
    repo.upsert_clean("financial_indicator", {
        "secucode": "002501.SZ", "report_date": "2023-12-31",
        "total_operate_income": 100.0, "parent_netprofit": 15.0,
    })

    ctx = _build_context(repo, "002501.SZ", "2024-12-31")
    assert ctx is not None
    # revenue_yoy 应为 None（去年同期营收被阻断）
    feat = growth.RevenueYoY()
    assert feat.calculate(ctx) is None


def test_q1_qoq_requires_prev_year_diff_chain(repo):
    """P1 #1: Q1 环比需要上一年全年与 Q3 的差分链。

    Q1(03-31) 的上一报告期是上一年 FY(12-31)。
    Q4单季 = 上一年全年累计 - 上一年 Q3 累计。
    缺上一年 Q3 → previous_sq 缺失 → QoQ 返回 None。
    """
    # 当期 Q1
    repo.upsert_clean("income_statement", {
        "secucode": "002501.SZ", "report_date": "2024-03-31",
        "total_operate_income": 100.0, "parent_netprofit": 20.0,
    })
    # 上一报告期 = 2023-12-31（FY），有全年累计
    repo.upsert_clean("income_statement", {
        "secucode": "002501.SZ", "report_date": "2023-12-31",
        "total_operate_income": 450.0, "parent_netprofit": 90.0,
    })
    # 缺 2023-09-30（Q3）→ prev_prev 缺失 → Q4单季无法差分

    ctx = _build_context(repo, "002501.SZ", "2024-03-31")
    assert ctx is not None
    # previous_sq 应为 None（缺 prev_prev Q3）
    # QoQ 应为 None（missing_comparison_period）
    feat = growth.RevenueQoQ()
    assert feat.calculate(ctx) is None


# ---------------------------------------------------------------------------
# 引擎最终诊断统计测试（P2 #3：计算原因进入最终统计）
# ---------------------------------------------------------------------------

def test_compute_all_collects_feature_diagnostics(repo, caplog):
    """P2 #3: 特征计算时追加的原因应进入最终诊断统计。

    审阅复现：当期利润 10、去年同期 -10，profit_yoy 返回 None，
    最终日志统计应含 non_positive_comparison_base，而非只有 missing_comparison_period。

    注意：可比性门控会先阻断同表未验证口径的字段。这里用受控 FeatureContext
    直接验证特征计算时追加的原因能进入最终统计。
    """
    import logging
    from fin_feature.features.engine import _build_context

    # 用受控上下文直接测试：去年同期利润为负，且显式标记可比
    from fin_feature.features.semantic import FieldSource

    ctx = FeatureContext(
        secucode="002501.SZ", report_date="2024-12-31",
        current={
            "parent_netprofit": 10.0,
            "__sources__": {"parent_netprofit": FieldSource("income_statement", comparable=True)},
        },
        previous=None,
        year_ago={
            "parent_netprofit": -10.0,
            "__sources__": {"parent_netprofit": FieldSource("income_statement", comparable=True)},
        },
        current_sq=None, previous_sq=None,
        diagnostics=[],
    )

    feat = growth.ProfitYoY()
    value = feat.calculate(ctx)
    assert value is None  # 非正基数 → None
    # 诊断应被追加到 ctx.diagnostics
    assert ctx.diagnostics is not None
    reasons = [d["reason_code"] for d in ctx.diagnostics]
    assert "non_positive_comparison_base" in reasons


def test_diagnostics_initialized_non_none_in_context(repo):
    """P2 #3: FeatureContext.diagnostics 应统一初始化为非 None list。

    审阅指出：默认 None 的上下文会直接丢弃原因。
    """
    repo.upsert_clean("income_statement", {
        "secucode": "002501.SZ", "report_date": "2024-12-31",
        "total_operate_income": 1000.0, "parent_netprofit": 200.0,
    })

    ctx = _build_context(repo, "002501.SZ", "2024-12-31")
    assert ctx is not None
    # diagnostics 应为 list（非 None），特征计算时可追加
    assert ctx.diagnostics is not None
    assert isinstance(ctx.diagnostics, list)


# ---------------------------------------------------------------------------
# 派生值与差分链门禁测试（第四轮 P1：路径 A 和路径 B）
# ---------------------------------------------------------------------------

def test_debt_asset_ratio_change_blocked_when_incomparable(repo):
    """路径 A: 原始资产/负债被阻断后，派生负债率变化必须为 None。

    审阅复现：两期 BS 资产/负债来源未验证（comparable=False），
    DebtAssetRatioChange 仍输出约 5 个百分点是错误的。
    修复后：原始字段被阻断 → 派生比率同步失效 → 变化为 None。
    """
    # 当期 BS
    repo.upsert_clean("balance_sheet", {
        "secucode": "002501.SZ", "report_date": "2024-06-30",
        "total_assets": 100.0, "total_liabilities": 60.0,
        "total_equity": 40.0,
    })
    # 上一报告期 BS（来源未验证）
    repo.upsert_clean("balance_sheet", {
        "secucode": "002501.SZ", "report_date": "2024-03-31",
        "total_assets": 100.0, "total_liabilities": 55.0,
        "total_equity": 45.0,
    })

    ctx = _build_context(repo, "002501.SZ", "2024-06-30")
    assert ctx is not None

    # 原始字段被阻断
    assert ctx.previous is not None
    assert ctx.previous.get("total_assets") is None
    assert ctx.previous.get("total_liabilities") is None
    # 派生比率应同步失效（路径 A 修复）
    assert ctx.previous.get("debt_asset_ratio") is None

    # 跨期负债率变化必须为 None
    feat = solvency.DebtAssetRatioChange()
    assert feat.calculate(ctx) is None

    # 当期负债率仍可独立计算（不破坏当期合法值）
    feat_level = solvency.DebtAssetRatio()
    assert feat_level.calculate(ctx) == 0.6


def _make_comparable_merge(repo, secucode, report_date, table_suffix, **fields):
    """构造可比的语义合并结果（供受控 fixture 经真实 _build_context 门禁验证）。

    写入 clean 记录并返回一个 mock merge 函数，该函数对指定报告期返回
    带 comparable=True 的 FieldSource，模拟已验证同口径。
    """
    from unittest.mock import patch
    from fin_feature.features.semantic import FieldSource

    record = {"secucode": secucode, "report_date": report_date}
    record.update(fields)
    repo.upsert_clean(table_suffix, record)

    # 返回 (patcher, merge_fn) —— merge_fn 模拟语义合并，标记字段可比
    real_merge = merge_three_tables_semantic

    def merge_fn(r, sc, rd):
        merged, diags = real_merge(r, sc, rd)
        sources = merged.get("__sources__", {})
        for f, src in sources.items():
            if hasattr(src, "comparable"):
                src.comparable = True
        return merged, diags

    return merge_fn


def test_debt_asset_ratio_change_comparable(repo):
    """路径 A: 显式可比的 55%→60% 应为 5.0（经真实 _build_context 门禁）。

    审阅建议：正例应经过引擎门禁，而非直接构造最终 FeatureContext。
    """
    from unittest.mock import patch

    # 当期 BS
    repo.upsert_clean("balance_sheet", {
        "secucode": "002501.SZ", "report_date": "2024-06-30",
        "total_assets": 100.0, "total_liabilities": 60.0,
        "total_equity": 40.0,
    })
    # 上一报告期 BS
    repo.upsert_clean("balance_sheet", {
        "secucode": "002501.SZ", "report_date": "2024-03-31",
        "total_assets": 100.0, "total_liabilities": 55.0,
        "total_equity": 45.0,
    })

    merge_fn = _make_comparable_merge(
        repo, "002501.SZ", "2024-06-30", "balance_sheet",
        total_assets=100.0, total_liabilities=60.0,
    )

    with patch("fin_feature.features.engine.merge_three_tables_semantic", side_effect=merge_fn):
        ctx = _build_context(repo, "002501.SZ", "2024-06-30")
    assert ctx is not None

    # 显式可比 → 派生比率保留，变化为 5.0
    feat = solvency.DebtAssetRatioChange()
    result = feat.calculate(ctx)
    assert result is not None
    assert abs(result - 5.0) < 1e-9


def test_q1_qoq_blocked_when_q3_incomparable(repo):
    """路径 B: Q3 存在但口径未知时 Q1 QoQ 为 None（经真实 _build_context 门禁）。

    审阅建议：前两期应显式放行，仅切换 Q3 标记，确保是路径 B 门禁拦截。
    """
    from unittest.mock import patch

    # 当期 Q1
    repo.upsert_clean("income_statement", {
        "secucode": "002501.SZ", "report_date": "2024-03-31",
        "total_operate_income": 180.0, "parent_netprofit": 20.0,
    })
    # 上一报告期 = 2023-12-31（FY）
    repo.upsert_clean("income_statement", {
        "secucode": "002501.SZ", "report_date": "2023-12-31",
        "total_operate_income": 450.0, "parent_netprofit": 90.0,
    })
    # prev_prev = 2023-09-30（Q3），存在但口径未知
    repo.upsert_clean("income_statement", {
        "secucode": "002501.SZ", "report_date": "2023-09-30",
        "total_operate_income": 300.0, "parent_netprofit": 60.0,
    })

    # 前两期（当期 Q1 和 FY）显式可比，仅 Q3 保持不可比
    real_merge = merge_three_tables_semantic
    comparable_dates = {"2024-03-31", "2023-12-31"}

    def merge_fn(r, sc, rd):
        merged, diags = real_merge(r, sc, rd)
        if rd in comparable_dates:
            sources = merged.get("__sources__", {})
            for f, src in sources.items():
                if hasattr(src, "comparable"):
                    src.comparable = True
        return merged, diags

    with patch("fin_feature.features.engine.merge_three_tables_semantic", side_effect=merge_fn):
        ctx = _build_context(repo, "002501.SZ", "2024-03-31")
    assert ctx is not None

    # Q3 不可比 → previous_sq 差分被阻断 → QoQ 为 None
    feat = growth.RevenueQoQ()
    assert feat.calculate(ctx) is None


def test_q1_qoq_comparable_chain(repo):
    """路径 B: 全部必要输入显式可比时，300/450/180 的例子应为 0.2（经真实 _build_context 门禁）。

    Q1=180, FY去年=450, Q3去年=300 → Q4单季=150 → QoQ=(180-150)/150=0.2
    审阅建议：正例应经过引擎门禁。
    """
    from unittest.mock import patch

    # 当期 Q1
    repo.upsert_clean("income_statement", {
        "secucode": "002501.SZ", "report_date": "2024-03-31",
        "total_operate_income": 180.0, "parent_netprofit": 20.0,
    })
    # 上一报告期 = 2023-12-31（FY）
    repo.upsert_clean("income_statement", {
        "secucode": "002501.SZ", "report_date": "2023-12-31",
        "total_operate_income": 450.0, "parent_netprofit": 90.0,
    })
    # prev_prev = 2023-09-30（Q3）
    repo.upsert_clean("income_statement", {
        "secucode": "002501.SZ", "report_date": "2023-09-30",
        "total_operate_income": 300.0, "parent_netprofit": 60.0,
    })

    # 全部三期显式可比
    real_merge = merge_three_tables_semantic

    def merge_fn(r, sc, rd):
        merged, diags = real_merge(r, sc, rd)
        sources = merged.get("__sources__", {})
        for f, src in sources.items():
            if hasattr(src, "comparable"):
                src.comparable = True
        return merged, diags

    with patch("fin_feature.features.engine.merge_three_tables_semantic", side_effect=merge_fn):
        ctx = _build_context(repo, "002501.SZ", "2024-03-31")
    assert ctx is not None

    # 全部可比 → Q4单季=450-300=150 → QoQ=(180-150)/150=0.2
    feat = growth.RevenueQoQ()
    result = feat.calculate(ctx)
    assert result is not None
    assert abs(result - 0.2) < 1e-9

