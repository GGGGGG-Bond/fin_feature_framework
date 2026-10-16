"""哈希计算规范：metric_hash 与 semantic_hash 的唯一生成入口。

所有哈希使用 SHA-256，序列化方式跨环境可复现：
    json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(',', ':'))

- metric_hash：仅标准化字段值（检测数值变化）
- semantic_hash：版本身份（字段值 + 来源 + 币种/计量/scope/period_basis 等语义维度）

R6 修复：Repository 内部调用本模块计算或校验哈希，不接受调用方任意提供的哈希值，
确保哈希与负载一致，杜绝"有效格式但负载矛盾"的静默去重。
"""

import hashlib
import json
import math
import re

# SHA-256 十六进制：64 字符 [0-9a-f]
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


def canonical_json(data) -> str:
    """规范化 JSON 序列化：sort_keys + ensure_ascii=False + 紧凑分隔符。

    禁止非有限值（NaN/Infinity）：allow_nan=False，违反时抛 ValueError。
    """
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def normalize_numeric(data: dict) -> dict:
    """统一数值表示：整数值的 float 转为 int，避免 100 与 100.0 产生不同哈希。

    A6 修复：Python/JSON 数值类型差异（int vs float）不应制造内容修订。
    - float 且 is_integer() → int（如 100.0 → 100）
    - bool 不变（bool 是 int 子类，单独排除）
    - 非有限 float（NaN/Inf）不在此处理（canonical_json 会拒绝）
    - 其他类型原样返回
    """
    result = {}
    for k, v in data.items():
        if isinstance(v, bool):
            result[k] = v
        elif isinstance(v, float) and not math.isnan(v) and not math.isinf(v) and v.is_integer():
            result[k] = int(v)
        else:
            result[k] = v
    return result


def compute_metric_hash(standardized_fields: dict) -> str:
    """计算 metric_hash：仅标准化字段值。

    standardized_fields 中的 None 值会被过滤（与迁移逻辑一致）。
    A6 修复：过滤后统一数值表示，避免 100 与 100.0 产生不同哈希。
    """
    filtered = {k: v for k, v in standardized_fields.items() if v is not None}
    normalized = normalize_numeric(filtered)
    serialized = canonical_json(normalized)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def compute_semantic_hash(
    standardized_fields: dict,
    source_name: str = "eastmoney",
    currency: str = "unknown",
    consolidate_scope: str = "unknown",
    period_basis: str = "unknown",
    amount_unit: str = "unknown",
    industry_type: str = "unknown",
) -> str:
    """计算 semantic_hash：版本身份（字段值 + 语义维度）。

    覆盖范围：standardized_fields + source_name + currency + consolidate_scope
    + period_basis + amount_unit + industry_type。
    元数据变化（如 notice_date、observed_at）不影响版本身份。
    A6 修复：字段值统一数值表示，避免 100 与 100.0 产生不同哈希。
    """
    filtered = {k: v for k, v in standardized_fields.items() if v is not None}
    normalized = normalize_numeric(filtered)
    payload = {
        "standardized_fields": normalized,
        "source_name": source_name,
        "currency": currency,
        "consolidate_scope": consolidate_scope,
        "period_basis": period_basis,
        "amount_unit": amount_unit,
        "industry_type": industry_type,
    }
    serialized = canonical_json(payload)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def is_valid_hash(value) -> bool:
    """校验是否为有效的 64 字符 SHA-256 十六进制字符串。"""
    return isinstance(value, str) and bool(_HEX64_RE.match(value))
