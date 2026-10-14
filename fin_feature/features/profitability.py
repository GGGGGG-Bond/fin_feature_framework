"""盈利能力特征：净利率、毛利率、ROE变化、ROE变化率。"""

from fin_feature.features.base import Feature, FeatureContext
from fin_feature.features.registry import FeatureRegistry


@FeatureRegistry.register
class NetProfitMargin(Feature):
    """净利率 = 归母净利润 / 营业收入。"""

    name = "net_profit_margin"
    group = "profitability"
    description = "净利率 = 归母净利润 / 营业收入"

    def calculate(self, ctx: FeatureContext) -> float | None:
        profit = ctx.current.get("parent_netprofit")
        revenue = ctx.current.get("total_operate_income")
        return self.safe_divide(profit, revenue)


@FeatureRegistry.register
class GrossMarginFeature(Feature):
    """毛利率（取 API 提供值，百分比）。"""

    name = "gross_margin_feat"
    group = "profitability"
    description = "毛利率（来自主要财务指标接口）"

    def calculate(self, ctx: FeatureContext) -> float | None:
        return ctx.current.get("gross_margin")


@FeatureRegistry.register
class RoeChange(Feature):
    """ROE 变化 = 当期 ROE - 上期 ROE（百分点）。"""

    name = "roe_change"
    group = "profitability"
    description = "ROE 变化（当期 - 上期，百分点）"

    def calculate(self, ctx: FeatureContext) -> float | None:
        current_roe = ctx.current.get("weightavg_roe")
        if ctx.previous is None:
            return None
        prev_roe = ctx.previous.get("weightavg_roe")
        return self.difference(current_roe, prev_roe)


@FeatureRegistry.register
class RoeChangeRate(Feature):
    """ROE 变化率 = (当期 ROE - 上期 ROE) / |上期 ROE|。"""

    name = "roe_change_rate"
    group = "profitability"
    description = "ROE 变化率（百分比变化）"

    def calculate(self, ctx: FeatureContext) -> float | None:
        current_roe = ctx.current.get("weightavg_roe")
        if ctx.previous is None:
            return None
        prev_roe = ctx.previous.get("weightavg_roe")
        return self.pct_change(current_roe, prev_roe)
