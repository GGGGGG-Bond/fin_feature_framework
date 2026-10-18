"""盈利能力特征：净利率、毛利率、ROE 变化、ROE 变化率。

定义版本：stage1-v1
- ROE 比较改为去年同期（year_ago），而非上一期（previous）
- 毛利率输出小数（0.3 表示 30%），非百分数
- 所有特征消费标准化入口产生的标准小数输入，不猜测单位
"""

from fin_feature.features.base import Feature, FeatureContext
from fin_feature.features.registry import FeatureRegistry


@FeatureRegistry.register
class NetProfitMargin(Feature):
    """净利率 = 归母净利润 / 营业收入（小数）。"""

    name = "net_profit_margin"
    group = "profitability"
    description = "净利率 = 归母净利润 / 营业总收入（小数）"

    def calculate(self, ctx: FeatureContext) -> float | None:
        profit = ctx.current.get("parent_netprofit")
        revenue = ctx.current.get("total_operate_income")
        # stage1-v1: 营收（分母）必须 > 0，否则输出 None + non_positive_base
        value, reason = self.safe_ratio(
            profit, revenue, require_positive_denominator=True,
        )
        if reason and ctx.diagnostics is not None:
            ctx.diagnostics.append({
                "field": "total_operate_income",
                "feature": "net_profit_margin",
                "reason_code": reason,
            })
        return value


@FeatureRegistry.register
class GrossMarginFeature(Feature):
    """经过核验的累计销售毛利率（标准化小数）。"""

    name = "gross_margin_feat"
    group = "profitability"
    description = "销售毛利率（经单位和定义核验的累计小数）"

    def calculate(self, ctx: FeatureContext) -> float | None:
        # T16: 金融企业不套用工业企业毛利率定义，返回 None + not_applicable
        if ctx.industry_type == "financial":
            if ctx.diagnostics is not None:
                ctx.diagnostics.append({
                    "field": "gross_margin",
                    "feature": "gross_margin_feat",
                    "reason_code": "not_applicable",
                    "detail": "金融企业不适用工业企业毛利率定义",
                })
            return None
        return ctx.current.get("gross_margin")


@FeatureRegistry.register
class RoeChange(Feature):
    """ROE 变化 = 当期 - 去年同期（百分点）。

    定义版本：stage1-v1
    使用去年同期（year_ago）而非上一期（previous）
    输入为标准化小数，输出百分点：(0.12 - 0.10) * 100 = 2.0
    不猜测单位；输入由标准化入口保证为小数
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
        # 标准化输入为小数：(0.12 - 0.10) * 100 = 2.0 个百分点
        return (current_roe - year_ago_roe) * 100


@FeatureRegistry.register
class RoeChangeRate(Feature):
    """ROE 变化率 = (当期 - 去年同期) / 去年同期（小数）。

    定义版本：stage1-v1
    使用去年同期（year_ago）而非上一期（previous）
    比较基数为零或负时返回 None（non_positive_comparison_base）
    """

    name = "roe_change_rate"
    group = "profitability"
    description = "ROE 变化率（小数，(当期 - 去年同期) / 去年同期）"

    def calculate(self, ctx: FeatureContext) -> float | None:
        current_roe = ctx.current.get("weightavg_roe")
        if ctx.year_ago is None:
            return None
        year_ago_roe = ctx.year_ago.get("weightavg_roe")
        value, reason = self.pct_change_with_reason(current_roe, year_ago_roe)
        if reason and ctx.diagnostics is not None:
            ctx.diagnostics.append({
                "field": "weightavg_roe",
                "feature": "roe_change_rate",
                "reason_code": reason,
            })
        return value
