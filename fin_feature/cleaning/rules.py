"""清洗规则配置：异常值阈值、缺失值策略、允许负值的金额字段。

规则集中管理，便于调整和扩展。阈值基于金融数据的合理范围设定。
支持从 config.yaml 加载配置覆盖默认值。

stage1-v1 (T14): 配置校验在写库前一次性完成——非法策略、倒置阈值、
非有限数全部拒绝并抛 ValueError，不部分更新规则。本批规则从不可变默认值
副本构造，避免模块全局状态污染后续批次。
"""

import copy
import math

# 异常值阈值：字段 -> (min, max)，超出范围视为异常，置 NULL 并记日志
# 阈值基于 A 股财务数据的合理范围（标准单位：小数为比例，金额为元）
_DEFAULT_OUTLIER_THRESHOLDS: dict[str, tuple[float, float]] = {
    "weightavg_roe": (-2.0, 2.0),       # 加权 ROE，小数（-200% 到 200%）
    "gross_margin": (-1.0, 1.0),         # 毛利率，小数（-100% 到 100%）
    "debt_asset_ratio": (0.0, 2.0),      # 资产负债率，小数（允许超过 1）
    "current_ratio": (0.0, 20.0),        # 流动比率，倍数
    "net_profit_margin": (-10.0, 10.0),  # 净利率，小数
    "bps": (-1000.0, 10000.0),           # 每股净资产，元
    "basic_eps": (-100.0, 100.0),        # 每股收益，元
    "total_operate_income": (-1e13, 1e13),  # 营收，元
    "parent_netprofit": (-1e13, 1e13),      # 归母净利润，元
    "total_assets": (0.0, 1e14),             # 总资产，元
    "total_liabilities": (0.0, 1e14),        # 总负债，元
    "total_equity": (-1e13, 1e14),           # 总权益（可能为负），元
    "operate_profit": (-1e13, 1e13),
    "total_profit": (-1e13, 1e13),
}

# 缺失值策略：drop=丢弃整条记录，zero=填 0，keep=保留 NULL
# 核心字段缺失则丢弃记录（无法计算特征）
_DEFAULT_MISSING_STRATEGY: dict[str, str] = {
    "total_operate_income": "drop",
    "parent_netprofit": "drop",
    "total_assets": "drop",
    "total_liabilities": "drop",
    "weightavg_roe": "keep",
    "gross_margin": "keep",
    "bps": "keep",
    "current_ratio": "keep",
    "debt_asset_ratio": "keep",
}

# 默认缺失值策略（未在 MISSING_STRATEGY 中明确指定的字段）
_DEFAULT_DEFAULT_MISSING_STRATEGY = "keep"

# 合法的缺失值策略
_VALID_STRATEGIES = frozenset({"drop", "zero", "keep"})

# 允许为负的金额字段（净利润、总权益等可能为负，不视为异常）
AMOUNT_ALLOW_NEGATIVE: set[str] = {
    "parent_netprofit",
    "total_equity",
    "operate_profit",
    "total_profit",
    "income_tax",
    "deduct_parent_netprofit",
    "weightavg_roe",
    "basic_eps",
    "net_profit_margin",
    "gross_margin",
    "revenue_yoy",
    "profit_yoy",
    "revenue_qoq",
    "profit_qoq",
}

# 不允许为负的金额字段（营收、资产、负债等应为正）
AMOUNT_MUST_POSITIVE: set[str] = {
    "total_operate_income",
    "total_assets",
    "total_liabilities",
    "monetary_funds",
    "inventory",
    "accounts_receivable",
    "accounts_payable",
    "fixed_asset",
}

# 本批运行时规则（从不可变默认值副本构造，避免全局状态污染）
OUTLIER_THRESHOLDS: dict[str, tuple[float, float]] = copy.deepcopy(_DEFAULT_OUTLIER_THRESHOLDS)
MISSING_STRATEGY: dict[str, str] = copy.deepcopy(_DEFAULT_MISSING_STRATEGY)
DEFAULT_MISSING_STRATEGY = _DEFAULT_DEFAULT_MISSING_STRATEGY


def _is_finite_number(value) -> bool:
    """检查值是否为有限数（非 NaN、非 Inf、非布尔）。"""
    if isinstance(value, bool):
        return False
    try:
        num = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(num)


def _validate_config(config: dict) -> None:
    """一次性校验完整清洗配置，任何非法项抛 ValueError。

    T14: 写库前拒绝——非法策略、倒置阈值、非有限数、错误类型。
    不部分更新规则：校验全部通过后才由 load_rules_config 应用。
    """
    errors: list[str] = []

    # 校验异常值阈值
    outlier_cfg = config.get("outlier_thresholds")
    if outlier_cfg:
        if not isinstance(outlier_cfg, dict):
            errors.append("outlier_thresholds 必须是字典")
        else:
            for field, bounds in outlier_cfg.items():
                if not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
                    errors.append(
                        f"outlier_thresholds.{field} 必须是长度为 2 的列表/元组，实际: {bounds!r}"
                    )
                    continue
                lo, hi = bounds
                if not _is_finite_number(lo) or not _is_finite_number(hi):
                    errors.append(
                        f"outlier_thresholds.{field} 含非有限数: {bounds!r}"
                    )
                    continue
                if float(lo) > float(hi):
                    errors.append(
                        f"outlier_thresholds.{field} 阈值倒置: min={lo} > max={hi}"
                    )

    # 校验缺失值策略
    missing_cfg = config.get("missing_strategy")
    if missing_cfg:
        if not isinstance(missing_cfg, dict):
            errors.append("missing_strategy 必须是字典")
        else:
            for field, strategy in missing_cfg.items():
                if field == "default":
                    if strategy not in _VALID_STRATEGIES:
                        errors.append(
                            f"missing_strategy.default 非法策略: {strategy!r}，合法值: {_VALID_STRATEGIES}"
                        )
                elif strategy not in _VALID_STRATEGIES:
                    errors.append(
                        f"missing_strategy.{field} 非法策略: {strategy!r}，合法值: {_VALID_STRATEGIES}"
                    )

    if errors:
        raise ValueError(
            "清洗配置校验失败（写库前拒绝，不部分应用）:\n  - "
            + "\n  - ".join(errors)
        )


def load_rules_config(config: dict | None = None) -> None:
    """从配置加载清洗规则，覆盖默认值。

    config: 配置 dict，含 cleaning.outlier_thresholds 和 cleaning.missing_strategy

    T14: 先一次性校验完整配置，全部通过后才应用；非法策略、倒置阈值、
    非有限数在写库前抛 ValueError，不部分更新规则。
    本批规则从不可变默认值副本构造，避免模块全局状态污染后续批次。
    """
    global OUTLIER_THRESHOLDS, MISSING_STRATEGY, DEFAULT_MISSING_STRATEGY

    if config is None:
        return

    # T14: 写库前一次性校验完整配置
    _validate_config(config)

    # 校验通过，从不可变默认值副本构造本批规则
    OUTLIER_THRESHOLDS = copy.deepcopy(_DEFAULT_OUTLIER_THRESHOLDS)
    MISSING_STRATEGY = copy.deepcopy(_DEFAULT_MISSING_STRATEGY)
    DEFAULT_MISSING_STRATEGY = _DEFAULT_DEFAULT_MISSING_STRATEGY

    # 加载异常值阈值
    outlier_cfg = config.get("outlier_thresholds")
    if outlier_cfg:
        for field, bounds in outlier_cfg.items():
            OUTLIER_THRESHOLDS[field] = (float(bounds[0]), float(bounds[1]))

    # 加载缺失值策略
    missing_cfg = config.get("missing_strategy")
    if missing_cfg:
        for field, strategy in missing_cfg.items():
            if field == "default":
                DEFAULT_MISSING_STRATEGY = strategy
            else:
                MISSING_STRATEGY[field] = strategy


def get_missing_strategy(field: str) -> str:
    """获取某字段的缺失值策略。"""
    return MISSING_STRATEGY.get(field, DEFAULT_MISSING_STRATEGY)


def get_outlier_threshold(field: str) -> tuple[float, float] | None:
    """获取某字段的异常值阈值，无阈值返回 None。"""
    return OUTLIER_THRESHOLDS.get(field)
