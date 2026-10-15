"""清洗规则配置：异常值阈值、缺失值策略、允许负值的金额字段。

规则集中管理，便于调整和扩展。阈值基于金融数据的合理范围设定。
支持从 config.yaml 加载配置覆盖默认值。
"""

# 异常值阈值：字段 -> (min, max)，超出范围视为异常，置 NULL 并记日志
# 阈值基于 A 股财务数据的合理范围（标准单位：小数为比例，金额为元）
OUTLIER_THRESHOLDS: dict[str, tuple[float, float]] = {
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
MISSING_STRATEGY: dict[str, str] = {
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
DEFAULT_MISSING_STRATEGY = "keep"

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

# 运行时配置（可通过 load_config 覆盖）
_config_loaded = False


def load_rules_config(config: dict | None = None) -> None:
    """从配置加载清洗规则，覆盖默认值。

    config: 配置 dict，含 cleaning.outlier_thresholds 和 cleaning.missing_strategy
    """
    global _config_loaded
    if config is None:
        return

    # 加载异常值阈值
    outlier_cfg = config.get("outlier_thresholds")
    if outlier_cfg:
        for field, bounds in outlier_cfg.items():
            if isinstance(bounds, (list, tuple)) and len(bounds) == 2:
                OUTLIER_THRESHOLDS[field] = (float(bounds[0]), float(bounds[1]))

    # 加载缺失值策略
    missing_cfg = config.get("missing_strategy")
    if missing_cfg:
        for field, strategy in missing_cfg.items():
            if strategy in ("drop", "zero", "keep"):
                MISSING_STRATEGY[field] = strategy
        # 更新默认策略
        if "default" in missing_cfg:
            global DEFAULT_MISSING_STRATEGY
            DEFAULT_MISSING_STRATEGY = missing_cfg["default"]

    _config_loaded = True


def get_missing_strategy(field: str) -> str:
    """获取某字段的缺失值策略。"""
    return MISSING_STRATEGY.get(field, DEFAULT_MISSING_STRATEGY)


def get_outlier_threshold(field: str) -> tuple[float, float] | None:
    """获取某字段的异常值阈值，无阈值返回 None。"""
    return OUTLIER_THRESHOLDS.get(field)
