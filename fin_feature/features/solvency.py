"""偿债能力特征：资产负债率变化、流动比率变化、权益比率。"""

from fin_feature.features.base import Feature, FeatureContext
from fin_feature.features.registry import FeatureRegistry


@FeatureRegistry.register
class DebtAssetRatioChange(Feature):
    """资产负债率变化 = 当期 - 上期（百分点）。"""

    name = "debt_asset_ratio_change"
    group = "solvency"
    description = "资产负债率变化（当期 - 上期，百分点）"

    def calculate(self, ctx: FeatureContext) -> float | None:
        current = ctx.current.get("debt_asset_ratio")
        if ctx.previous is None:
            return None
        previous = ctx.previous.get("debt_asset_ratio")
        return self.difference(current, previous)


@FeatureRegistry.register
class CurrentRatioChange(Feature):
    """流动比率变化 = 当期 - 上期。"""

    name = "current_ratio_change"
    group = "solvency"
    description = "流动比率变化（当期 - 上期）"

    def calculate(self, ctx: FeatureContext) -> float | None:
        current = ctx.current.get("current_ratio")
        if ctx.previous is None:
            return None
        previous = ctx.previous.get("current_ratio")
        return self.difference(current, previous)


@FeatureRegistry.register
class EquityRatio(Feature):
    """权益比率 = 净资产 / 总资产。"""

    name = "equity_ratio"
    group = "solvency"
    description = "权益比率 = 净资产 / 总资产"

    def calculate(self, ctx: FeatureContext) -> float | None:
        equity = ctx.current.get("total_equity")
        assets = ctx.current.get("total_assets")
        return self.safe_divide(equity, assets)


@FeatureRegistry.register
class DebtAssetRatio(Feature):
    """资产负债率（当期水平值）。"""

    name = "debt_asset_ratio"
    group = "solvency"
    description = "资产负债率（当期水平值）"

    def calculate(self, ctx: FeatureContext) -> float | None:
        return ctx.current.get("debt_asset_ratio")
