"""盈利能力特征：净利率、毛利率、ROE 变化、ROE 变化率。

定义版本：stage1-v1
- ROE 比较改为去年同期（year_ago），而非上一期（previous）
- 毛利率输出小数（0.3 表示 30%），非百分数
"""

from fin_feature.features.base import Feature, FeatureContext
from fin_feature.features.registry import FeatureRegistry


@FeatureRegistry.register
class NetProfitMargin(Feature):
    """净利率 = 归母净利润 / 营业收入（小数）。"""

    name = "net_profit_margin"
    group = "profitability"
    description = "净利率 = 归母净利润 / 营业收入（小数）"

    def calculate(self, ctx: FeatureContext) -> float | None:
        profit = ctx.current.get("parent_netprofit")
        revenue = ctx.current.get("total_operate_income")
        return self.safe_divide(profit, revenue)


@FeatureRegistry.register
class GrossMarginFeature(Feature):
    """毛利率（来自 API 的 XSMLL 字段，已转换为小数）。"""

    name = "gross_margin_feat"
    group = "profitability"
    description = "销售毛利率（小数，来自主要财务指标接口）"

    def calculate(self, ctx: FeatureContext) -> float | None:
        return ctx.current.get("gross_margin")


@FeatureRegistry.register
class RoeChange(Feature):
    """ROE 变化 = 当期 - 去年同期（百分点）。

    定义版本：stage1-v1
    使用去年同期（year_ago）而非上一期（previous）
    输入为小数时输出百分点：(0.12 - 0.10) * 100 = 2.0
    兼容旧百分数输入：16.0 - 14.0 = 2.0
    """

    name = "roe_change"
    group = "profitability"
    description = "ROE 变化（当期 - 去年同期，百分点）"

    def calculate(self, ctx: FeatureContext) -> float | None:
        current_roe = ctx.current.get("weightavg_roe")
        if ctx.year_ago is None:
            return None
        year_ago_roe = ctx.year_ago.get("weightavg_roe")
        if current_roe is None or year_ago_roe is None:
            return None
        # 检测输入格式：>1 可能是百分数，<=1 可能是小数
        if abs(current_roe) <= 1.0 and abs(year_ago_roe) <= 1.0:
            # 小数输入：(0.12 - 0.10) * 100 = 2.0 个百分点
            return (current_roe - year_ago_roe) * 100
        else:
            # 百分数输入兼容：16.0 - 14.0 = 2.0 个百分点
            return current_roe - year_ago_roe


@FeatureRegistry.register
class RoeChangeRate(Feature):
    """ROE 变化率 = (当期 - 去年同期) / |去年同期|（小数）。

    定义版本：stage1-v1
    使用去年同期（year_ago）而非上一期（previous）
    """

    name = "roe_change_rate"
    group = "profitability"
    description = "ROE 变化率（小数，(当期 - 去年同期) / |去年同期|）"

    def calculate(self, ctx: FeatureContext) -> float | None:
        current_roe = ctx.current.get("weightavg_roe")
        if ctx.year_ago is None:
            return None
        year_ago_roe = ctx.year_ago.get("weightavg_roe")
        return self.pct_change(current_roe, year_ago_roe)
