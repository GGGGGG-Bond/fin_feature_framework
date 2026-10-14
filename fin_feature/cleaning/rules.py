"""清洗规则配置：异常值阈值、缺失值策略、允许负值的金额字段。

规则集中管理，便于调整和扩展。阈值基于金融数据的合理范围设定。
"""

# 异常值阈值：字段 -> (min, max)，超出范围视为异常，置 NULL 并记日志
# 阈值基于 A 股财务数据的合理范围
OUTLIER_THRESHOLDS: dict[str, tuple[float, float]] = {
    "weightavg_roe": (-200.0, 200.0),       # 加权ROE，百分比
    "gross_margin": (-100.0, 100.0),         # 毛利率，百分比
    "debt_asset_ratio": (0.0, 100.0),        # 资产负债率，百分比
    "current_ratio": (0.0, 100.0),           # 流动比率
    "net_profit_margin": (-100.0, 100.0),    # 净利率，百分比
    "bps": (-1000.0, 100000.0),              # 每股净资产
    "basic_eps": (-1000.0, 1000.0),          # 每股收益
    "total_operate_income": (-1.0e13, 1.0e13),  # 营收，单位元
    "parent_netprofit": (-1.0e13, 1.0e13),      # 归母净利润
    "total_assets": (0.0, 1.0e14),             # 总资产
    "total_liabilities": (0.0, 1.0e14),        # 总负债
    "total_equity": (-1.0e13, 1.0e14),         # 总权益（可能为负）
    "operate_profit": (-1.0e13, 1.0e13),
    "total_profit": (-1.0e13, 1.0e13),
}

# 缺失值策略：drop=丢弃整条记录, zero=填0, keep=保留NULL
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
    "debt_asset_ratio",
    "current_ratio",
    "bps",
}


def get_missing_strategy(field: str) -> str:
    """获取某字段的缺失值策略。"""
    return MISSING_STRATEGY.get(field, DEFAULT_MISSING_STRATEGY)


def get_outlier_threshold(field: str) -> tuple[float, float] | None:
    """获取某字段的异常值阈值，无阈值返回 None。"""
    return OUTLIER_THRESHOLDS.get(field)
