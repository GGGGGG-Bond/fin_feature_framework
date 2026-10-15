"""偿债能力特征：资产负债率变化、流动比率变化、权益比率、资产负债率。

定义版本：stage1-v1
- 资产负债率、权益比率输出小数（0.6 表示 60%）
- 变化值输出百分点（如 0.55→0.60 输出 5.0）
- 流动比率单位未知时不强行计算（unknown_unit）
- 资产负债率水平与变化均使用 BS 自算 L/A，口径一致
- 不猜测单位；输入由标准化入口保证
"""

from fin_feature.features.base import Feature, FeatureContext
from fin_feature.features.registry import FeatureRegistry


@FeatureRegistry.register
class DebtAssetRatioChange(Feature):
    """资产负债率变化 = 当期 - 上期（百分点）。

    定义版本：stage1-v1
    使用 BS 自算的 debt_asset_ratio（L/A 小数），与水平定义一致
    输入为小数时输出百分点差值：(0.60 - 0.55) * 100 = 5.0
    不猜测单位；debt_asset_ratio 由上下文层预算
    """

    name = "debt_asset_ratio_change"
    group = "solvency"
    description = "资产负债率变化（当期 - 上期，百分点，使用自算 L/A）"

    def calculate(self, ctx: FeatureContext) -> float | None:
        current = ctx.current.get("debt_asset_ratio")
        if ctx.previous is None:
            return None
        previous = ctx.previous.get("debt_asset_ratio")
        if current is None or previous is None:
            return None
        # 自算 L/A 为小数：(0.60 - 0.55) * 100 = 5.0 个百分点
        return (current - previous) * 100


@FeatureRegistry.register
class CurrentRatioChange(Feature):
    """流动比率变化 = 当期 - 上期（倍数差）。

    定义版本：stage1-v1
    流动比率单位需预先验证，未知单位时不计算（unknown_unit）
    标准化入口对 current_ratio 未知单位返回 None
    """

    name = "current_ratio_change"
    group = "solvency"
    description = "流动比率变化（当期 - 上期，倍数差）"

    def calculate(self, ctx: FeatureContext) -> float | None:
        current = ctx.current.get("current_ratio")
        if ctx.previous is None:
            return None
        previous = ctx.previous.get("current_ratio")
        if current is None or previous is None:
            return None
        return current - previous


@FeatureRegistry.register
class EquityRatio(Feature):
    """权益比率 = 总权益 / 总资产（小数）。

    定义版本：stage1-v1
    允许权益为负，结果可为负
    """

    name = "equity_ratio"
    group = "solvency"
    description = "权益比率 = 总权益 / 总资产（小数，可为负）"

    def calculate(self, ctx: FeatureContext) -> float | None:
        equity = ctx.current.get("total_equity")
        assets = ctx.current.get("total_assets")
        # stage1-v1: 总资产（分母）必须 > 0
        value, reason = self.safe_ratio(equity, assets, require_positive_denominator=True)
        if reason and ctx.diagnostics is not None:
            ctx.diagnostics.append({
                "field": "total_assets",
                "feature": "equity_ratio",
                "reason_code": reason,
            })
        return value


@FeatureRegistry.register
class DebtAssetRatio(Feature):
    """资产负债率 = 总负债 / 总资产（小数）。

    定义版本：stage1-v1
    使用 BS 自算值（由上下文层预算），允许结果超过 1（资不抵债）
    """

    name = "debt_asset_ratio"
    group = "solvency"
    description = "资产负债率 = 总负债 / 总资产（小数，可超过 1，自算）"

    def calculate(self, ctx: FeatureContext) -> float | None:
        # 使用上下文层预算的自算值
        return ctx.current.get("debt_asset_ratio")
