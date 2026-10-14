"""成长性特征：营收/净利润的同比、环比增速。

同比 = (当期 - 去年同期) / |去年同期|
环比 = (当期 - 上一报告期) / |上一报告期|

优先使用 API 直接提供的增速字段（YSTZ/SJLTZ/YSHZ/SJLHZ），
若缺失则用原始金额自行计算，保证数据完整性。
"""

from fin_feature.features.base import Feature, FeatureContext
from fin_feature.features.registry import FeatureRegistry


@FeatureRegistry.register
class RevenueYoY(Feature):
    """营收同比增速。优先用 API 的 revenue_yoy，否则自行计算。"""

    name = "revenue_yoy"
    group = "growth"
    description = "营收同比增速"

    def calculate(self, ctx: FeatureContext) -> float | None:
        # 优先用 API 提供的同比增速
        api_yoy = ctx.current.get("revenue_yoy")
        if api_yoy is not None:
            # API 返回的是百分比数值（如 15.42 表示 15.42%），转为小数
            return api_yoy / 100.0
        # 自行计算
        if ctx.year_ago is None:
            return None
        current = ctx.current.get("total_operate_income")
        year_ago = ctx.year_ago.get("total_operate_income")
        return self.pct_change(current, year_ago)


@FeatureRegistry.register
class RevenueQoQ(Feature):
    """营收环比增速。优先用 API 的 revenue_qoq，否则自行计算。"""

    name = "revenue_qoq"
    group = "growth"
    description = "营收环比增速"

    def calculate(self, ctx: FeatureContext) -> float | None:
        api_qoq = ctx.current.get("revenue_qoq")
        if api_qoq is not None:
            return api_qoq / 100.0
        if ctx.previous is None:
            return None
        current = ctx.current.get("total_operate_income")
        previous = ctx.previous.get("total_operate_income")
        return self.pct_change(current, previous)


@FeatureRegistry.register
class ProfitYoY(Feature):
    """净利润同比增速。优先用 API 的 profit_yoy，否则自行计算。"""

    name = "profit_yoy"
    group = "growth"
    description = "净利润同比增速"

    def calculate(self, ctx: FeatureContext) -> float | None:
        api_yoy = ctx.current.get("profit_yoy")
        if api_yoy is not None:
            return api_yoy / 100.0
        if ctx.year_ago is None:
            return None
        current = ctx.current.get("parent_netprofit")
        year_ago = ctx.year_ago.get("parent_netprofit")
        return self.pct_change(current, year_ago)


@FeatureRegistry.register
class ProfitQoQ(Feature):
    """净利润环比增速。优先用 API 的 profit_qoq，否则自行计算。"""

    name = "profit_qoq"
    group = "growth"
    description = "净利润环比增速"

    def calculate(self, ctx: FeatureContext) -> float | None:
        api_qoq = ctx.current.get("profit_qoq")
        if api_qoq is not None:
            return api_qoq / 100.0
        if ctx.previous is None:
            return None
        current = ctx.current.get("parent_netprofit")
        previous = ctx.previous.get("parent_netprofit")
        return self.pct_change(current, previous)
