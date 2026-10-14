"""成长性特征：营收/净利润的同比、环比增速。

定义版本：stage1-v1
- 同比 = (当期 - 去年同期) / |去年同期|
- 环比 = (当期单季 - 上期单季) / |上期单季|
- 比较基数为零或负时输出 None，原因 non_positive_comparison_base
- 输出小数，非百分数
"""

from fin_feature.features.base import Feature, FeatureContext
from fin_feature.features.registry import FeatureRegistry


@FeatureRegistry.register
class RevenueYoY(Feature):
    """营收同比增速 = (当期累计 - 去年同期累计) / |去年同期累计|。"""

    name = "revenue_yoy"
    group = "growth"
    description = "营收同比增速（小数）"

    def calculate(self, ctx: FeatureContext) -> float | None:
        if ctx.year_ago is None:
            return None
        current = ctx.current.get("total_operate_income")
        year_ago = ctx.year_ago.get("total_operate_income")
        return self.pct_change(current, year_ago)


@FeatureRegistry.register
class RevenueQoQ(Feature):
    """营收环比增速 = (当期单季 - 上期单季) / |上期单季|。

    单季值由标准化层提供（累计转单季），特征层直接消费。
    """

    name = "revenue_qoq"
    group = "growth"
    description = "营收环比增速（小数）"

    def calculate(self, ctx: FeatureContext) -> float | None:
        if ctx.previous is None:
            return None
        # 阶段 1：使用累计金额直接比较（待后续改进为单季）
        # 契约要求：累计流量转单季后才能用于环比
        current = ctx.current.get("total_operate_income")
        previous = ctx.previous.get("total_operate_income")
        return self.pct_change(current, previous)


@FeatureRegistry.register
class ProfitYoY(Feature):
    """净利润同比增速 = (当期累计 - 去年同期累计) / |去年同期累计|。"""

    name = "profit_yoy"
    group = "growth"
    description = "净利润同比增速（小数）"

    def calculate(self, ctx: FeatureContext) -> float | None:
        if ctx.year_ago is None:
            return None
        current = ctx.current.get("parent_netprofit")
        year_ago = ctx.year_ago.get("parent_netprofit")
        return self.pct_change(current, year_ago)


@FeatureRegistry.register
class ProfitQoQ(Feature):
    """净利润环比增速 = (当期单季 - 上期单季) / |上期单季|。

    单季值由标准化层提供（累计转单季），特征层直接消费。
    """

    name = "profit_qoq"
    group = "growth"
    description = "净利润环比增速（小数）"

    def calculate(self, ctx: FeatureContext) -> float | None:
        if ctx.previous is None:
            return None
        # 阶段 1：使用累计金额直接比较（待后续改进为单季）
        current = ctx.current.get("parent_netprofit")
        previous = ctx.previous.get("parent_netprofit")
        return self.pct_change(current, previous)
