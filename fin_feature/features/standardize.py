"""唯一标准化入口：将来源原计量转换为运行时标准单位。

阶段 1 契约（stage1-v1）要求：
- 比例值统一小数（0.12 表示 12%），原百分值只转换一次
- 金额字段不转换
- current_ratio 单位未知时保留原值、输出 None + unknown_unit
- debt_asset_ratio 由 BS 自算，原 API 值仅对照
- 不能靠数值大小猜测单位

clean 表保留原计量；本模块在内存中对原值标准化，供清洗阈值判断和特征上下文使用。
"""

from dataclasses import dataclass, field

# 字段 -> 单位类型
# percent: 原值为百分数（如 16.75 表示 16.75%），标准化为小数（÷100）
# amount: 金额字段，不转换
# unknown: 单位未经验证，标准化返回 None + unknown_unit（如 current_ratio）
# self_computed: 由其他字段自算（如 debt_asset_ratio = L/A），不在此转换
# passthrough: 其他字段，原样返回
FIELD_UNITS: dict[str, str] = {
    # 百分数字段（来源为百分值）
    "weightavg_roe": "percent",
    "gross_margin": "percent",
    # 金额字段（不转换）
    "total_operate_income": "amount",
    "parent_netprofit": "amount",
    "operate_cost": "amount",
    "total_operate_cost": "amount",
    "operate_profit": "amount",
    "total_profit": "amount",
    "income_tax": "amount",
    "total_assets": "amount",
    "total_liabilities": "amount",
    "total_equity": "amount",
    "monetary_funds": "amount",
    "accounts_receivable": "amount",
    "inventory": "amount",
    "accounts_payable": "amount",
    "fixed_asset": "amount",
    "bps": "amount",
    "basic_eps": "amount",
    # 单位未经验证
    "current_ratio": "unknown",
    # 自算字段
    "debt_asset_ratio": "self_computed",
}

# 运行时单位覆盖（供受控 fixture 声明已验证的单位）
# 例如 fixture 可声明 current_ratio -> "percent_ratio" 或 "times"
_unit_overrides: dict[str, str] = {}


def set_unit_override(field: str, unit: str) -> None:
    """设置字段单位覆盖（供受控 fixture 使用）。

    unit 可选值：percent, percent_ratio, times, amount, unknown, self_computed
    - percent / percent_ratio: 原值为百分数，÷100
    - times: 原值为倍数，不转换
    - amount: 金额，不转换
    - unknown: 单位未知，返回 None
    """
    _unit_overrides[field] = unit


def clear_unit_overrides() -> None:
    """清除所有单位覆盖（测试间隔离）。"""
    _unit_overrides.clear()


def get_unit(field: str) -> str:
    """获取字段的单位类型（考虑覆盖）。"""
    if field in _unit_overrides:
        return _unit_overrides[field]
    return FIELD_UNITS.get(field, "passthrough")


def standardize(field: str, value, unit_map: dict[str, str] | None = None) -> tuple[float | None, str | None]:
    """将原值标准化为运行时标准单位。

    返回 (standard_value, reason_code)。
    - 成功转换: (标准值, None)
    - 原值为 None: (None, "missing_input")
    - 单位未知: (None, "unknown_unit")
    - 非有限数: (None, "invalid_value")

    unit_map: 显式固定单位映射（优先于 _unit_overrides 和 FIELD_UNITS）。
    供迁移等需要确定性、不受运行时 fixture 污染的场景使用。
    """
    import math

    if value is None:
        return (None, "missing_input")

    # 布尔值不作为数值
    if isinstance(value, bool):
        return (None, "invalid_value")

    # 尝试转为 float
    try:
        num = float(value)
    except (TypeError, ValueError):
        return (None, "invalid_value")

    # 检查 NaN/Inf
    if math.isnan(num) or math.isinf(num):
        return (None, "invalid_value")

    if unit_map is not None:
        unit = unit_map.get(field, "passthrough")
    else:
        unit = get_unit(field)

    if unit in ("percent", "percent_ratio"):
        # 百分数 -> 小数，只转换一次
        return (num / 100.0, None)
    elif unit in ("amount", "times", "passthrough"):
        # 金额、倍数、其他：不转换
        return (num, None)
    elif unit == "unknown":
        # 单位未经验证，不猜测
        return (None, "unknown_unit")
    elif unit == "self_computed":
        # 自算字段不在此转换（由上下文层预算）
        return (num, None)
    else:
        return (num, None)


def standardize_record(
    record: dict, fields: list[str] | None = None,
    unit_map: dict[str, str] | None = None,
) -> tuple[dict, list[dict]]:
    """标准化记录中的多个字段。

    返回 (标准化值字典, 诊断列表)。
    诊断元素: {"field": ..., "reason_code": ..., "original_value": ...}
    """
    if fields is None:
        fields = list(record.keys())

    standardized: dict = {}
    diagnostics: list[dict] = []

    for f in fields:
        original = record.get(f)
        std_val, reason = standardize(f, original, unit_map=unit_map)
        standardized[f] = std_val
        if reason is not None and reason != "missing_input":
            # missing_input 是常见情况，不单独诊断（特征层处理）
            # 但 unknown_unit / invalid_value 需要诊断
            diagnostics.append({
                "field": f,
                "reason_code": reason,
                "original_value": original,
            })
        elif reason == "missing_input" and original is not None:
            # original 非 None 但标准化失败
            diagnostics.append({
                "field": f,
                "reason_code": reason,
                "original_value": original,
            })

    return (standardized, diagnostics)


@dataclass
class SemanticRecord:
    """轻量语义记录：封装字段值与来源元信息。

    阶段 1 最小实现：currency/scope 为 unknown，不声称完整历史。
    comparable_for_fallback 控制跨表回退是否允许：
    - True: 受控 fixture 明确标注同口径，允许回退
    - None: 真实数据，可比性未知，拒绝回退
    """

    table_suffix: str
    metrics: dict = field(default_factory=dict)
    notice_date: str | None = None
    period_basis: str = "unknown"
    industry_type: str = "unknown"
    comparable_for_fallback: bool | None = None
    record_ref: str | None = None
