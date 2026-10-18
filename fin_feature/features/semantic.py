"""字段级语义合并：按权威规则合并三张清洗表，保留字段级来源元信息。

阶段 1 契约（field-authority.md）要求：
- 禁止整体 dict.update 合并；每个字段保留其来源表的元信息
- 非权威表 None 不覆盖权威表有效值
- IS=110、FI=100 均有效 → 用 IS=110，记录 source_conflict
- IS 缺失、FI 有效且可比 → 回退 + fallback_used；不可比 → None + incomparable_basis
- notice_date 等元信息随各自来源表，不被另一表覆盖
- 同报告期跨表回退需显式可比证据；真实数据默认不可比（comparable_for_fallback=None）
"""

from dataclasses import dataclass, field

from fin_feature.storage.repository import Repository
from fin_feature.utils.logger import setup_logger

logger = setup_logger("fin_feature.features.semantic")

# 字段 -> 权威来源表
FIELD_AUTHORITY: dict[str, str] = {
    # 利润表权威
    "total_operate_income": "income_statement",
    "parent_netprofit": "income_statement",
    "operate_cost": "income_statement",
    "total_operate_cost": "income_statement",
    "operate_profit": "income_statement",
    "total_profit": "income_statement",
    "income_tax": "income_statement",
    "deduct_parent_netprofit": "income_statement",
    # 财务指标权威
    "weightavg_roe": "financial_indicator",
    "gross_margin": "financial_indicator",
    "basic_eps": "financial_indicator",
    "bps": "financial_indicator",
    # 资产负债表权威
    "total_assets": "balance_sheet",
    "total_liabilities": "balance_sheet",
    "total_equity": "balance_sheet",
    "current_ratio": "balance_sheet",
    "monetary_funds": "balance_sheet",
    "accounts_receivable": "balance_sheet",
    "inventory": "balance_sheet",
    "accounts_payable": "balance_sheet",
    "fixed_asset": "balance_sheet",
}

# 字段 -> 回退表（仅营收/利润有 FI 回退；其余无回退）
FALLBACK_TABLE: dict[str, str | None] = {
    "total_operate_income": "financial_indicator",
    "parent_netprofit": "financial_indicator",
}

# 需要合并的数值字段（排除标识和元信息字段）
_MERGE_FIELDS = [
    # 利润表
    "total_operate_income", "parent_netprofit", "operate_cost",
    "total_operate_cost", "operate_profit", "total_profit",
    "income_tax", "deduct_parent_netprofit",
    # 财务指标
    "weightavg_roe", "gross_margin", "basic_eps", "bps",
    # 资产负债表
    "total_assets", "total_liabilities", "total_equity",
    "current_ratio", "monetary_funds", "accounts_receivable",
    "inventory", "accounts_payable", "fixed_asset",
]


@dataclass
class FieldSource:
    """字段来源元信息。"""

    table_suffix: str
    notice_date: str | None = None
    record_ref: str | None = None
    comparable: bool = False  # 受控 fixture 显式声明同口径可比
    comparison_basis: str | None = None  # 已核对且绑定输入内容的字段口径


def _get_clean_record_safe(
    repo: Repository, table_suffix: str, secucode: str, report_date: str
) -> dict | None:
    """安全获取清洗记录，返回 dict 或 None。"""
    row = repo.get_clean_record(table_suffix, secucode, report_date)
    return dict(row) if row is not None else None


def merge_three_tables_semantic(
    repo: Repository, secucode: str, report_date: str
) -> tuple[dict, list[dict]]:
    """按字段权威规则合并三张清洗表。

    返回 (merged_values, diagnostics)。
    merged_values: 字段 -> 标准化值（未标准化，保留原计量）
    diagnostics: [{"field", "reason_code", "detail", ...}]

    每个字段的来源元信息存储在 merged["__sources__"][field] = FieldSource。
    notice_date 不被其他表覆盖。
    """
    # 获取三张表的清洗记录
    records = {
        "financial_indicator": _get_clean_record_safe(repo, "financial_indicator", secucode, report_date),
        "income_statement": _get_clean_record_safe(repo, "income_statement", secucode, report_date),
        "balance_sheet": _get_clean_record_safe(repo, "balance_sheet", secucode, report_date),
    }

    merged: dict = {}
    sources: dict[str, FieldSource] = {}
    diagnostics: list[dict] = []

    for field_name in _MERGE_FIELDS:
        auth_table = FIELD_AUTHORITY.get(field_name)
        if auth_table is None:
            continue

        auth_record = records.get(auth_table)
        auth_value = auth_record.get(field_name) if auth_record else None

        fallback_table = FALLBACK_TABLE.get(field_name)
        fb_record = records.get(fallback_table) if fallback_table else None
        fb_value = fb_record.get(field_name) if fb_record else None

        if auth_value is not None:
            # 权威表有有效值
            merged[field_name] = auth_value
            sources[field_name] = FieldSource(
                table_suffix=auth_table,
                notice_date=auth_record.get("notice_date") if auth_record else None,
                record_ref=auth_record.get("record_ref") if auth_record else None,
                comparison_basis=auth_record.get('__comparison_bases__', {}).get(field_name),
            )
            # 检查冲突：回退表也有有效值
            if fb_value is not None and fb_value != auth_value:
                diagnostics.append({
                    "field": field_name,
                    "reason_code": "source_conflict",
                    "detail": f"权威表 {auth_table}={auth_value} 与回退表 {fallback_table}={fb_value} 冲突，采用权威值",
                    "auth_value": auth_value,
                    "fallback_value": fb_value,
                })
        elif fb_value is not None and fallback_table is not None:
            # 权威表缺失，回退表有值
            # 阶段 1：真实数据 comparable_for_fallback=None → 拒绝回退
            # 受控 fixture 可通过设置 comparable 标记允许回退
            # 这里默认拒绝（None），由调用方通过 repo 上下文控制
            # 阶段 1 真实数据：拒绝回退
            merged[field_name] = None
            sources[field_name] = FieldSource(table_suffix=auth_table)
            diagnostics.append({
                "field": field_name,
                "reason_code": "incomparable_basis",
                "detail": f"权威表 {auth_table} 缺失，回退表 {fallback_table}={fb_value} 可比性未知，拒绝回退",
                "fallback_value": fb_value,
            })
        else:
            # 两表都缺失
            merged[field_name] = None
            sources[field_name] = FieldSource(table_suffix=auth_table)

    # 保留标识字段（从任一可用表取）
    for ident_field in ["secucode", "report_date", "security_code", "security_name", "industry_name", "industry_type"]:
        for table_suffix in ["income_statement", "financial_indicator", "balance_sheet"]:
            rec = records.get(table_suffix)
            if rec and rec.get(ident_field) is not None:
                merged[ident_field] = rec[ident_field]
                sources[ident_field] = FieldSource(
                    table_suffix=table_suffix,
                    notice_date=rec.get("notice_date"),
                    record_ref=rec.get("record_ref"),
                )
                break

    standardized = [bool(rec.get("__standardized__")) for rec in records.values() if rec]
    if any(standardized) and not all(standardized):
        raise ValueError("cannot merge legacy raw units and standardized records")
    if standardized and all(standardized):
        merged["__value_format__"] = "standard-v1"
    for d in diagnostics:
        d["report_date"] = report_date
    merged["__sources__"] = sources
    return (merged, diagnostics)


def merge_three_tables_semantic_with_fallback_flag(
    repo: Repository, secucode: str, report_date: str,
    allow_fallback: bool = False,
) -> tuple[dict, list[dict]]:
    """合并三表，可控制是否允许跨表回退。

    allow_fallback=True 时，权威表缺失且回退表有值 → 回退 + fallback_used。
    allow_fallback=False（默认）时，拒绝回退 → None + incomparable_basis。
    受控 fixture 使用 allow_fallback=True 模拟已验证同口径。
    """
    if not allow_fallback:
        return merge_three_tables_semantic(repo, secucode, report_date)

    # 允许回退模式
    records = {
        "financial_indicator": _get_clean_record_safe(repo, "financial_indicator", secucode, report_date),
        "income_statement": _get_clean_record_safe(repo, "income_statement", secucode, report_date),
        "balance_sheet": _get_clean_record_safe(repo, "balance_sheet", secucode, report_date),
    }

    merged: dict = {}
    sources: dict[str, FieldSource] = {}
    diagnostics: list[dict] = []

    for field_name in _MERGE_FIELDS:
        auth_table = FIELD_AUTHORITY.get(field_name)
        if auth_table is None:
            continue

        auth_record = records.get(auth_table)
        auth_value = auth_record.get(field_name) if auth_record else None

        fallback_table = FALLBACK_TABLE.get(field_name)
        fb_record = records.get(fallback_table) if fallback_table else None
        fb_value = fb_record.get(field_name) if fb_record else None

        if auth_value is not None:
            merged[field_name] = auth_value
            sources[field_name] = FieldSource(
                table_suffix=auth_table,
                notice_date=auth_record.get("notice_date") if auth_record else None,
                record_ref=auth_record.get("record_ref") if auth_record else None,
            )
            if fb_value is not None and fb_value != auth_value:
                diagnostics.append({
                    "field": field_name,
                    "reason_code": "source_conflict",
                    "detail": f"权威表 {auth_table}={auth_value} 与回退表 {fallback_table}={fb_value} 冲突，采用权威值",
                    "auth_value": auth_value,
                    "fallback_value": fb_value,
                })
        elif fb_value is not None and fallback_table is not None:
            merged[field_name] = fb_value
            sources[field_name] = FieldSource(
                table_suffix=fallback_table,
                notice_date=fb_record.get("notice_date") if fb_record else None,
                record_ref=fb_record.get("record_ref") if fb_record else None,
                comparable=True,  # 受控 fixture 声明同口径可比
            )
            diagnostics.append({
                "field": field_name,
                "reason_code": "fallback_used",
                "detail": f"权威表 {auth_table} 缺失，回退使用 {fallback_table}={fb_value}",
                "fallback_value": fb_value,
            })
        else:
            merged[field_name] = None
            sources[field_name] = FieldSource(table_suffix=auth_table)

    for ident_field in ["secucode", "report_date", "security_code", "security_name", "industry_name", "industry_type"]:
        for table_suffix in ["income_statement", "financial_indicator", "balance_sheet"]:
            rec = records.get(table_suffix)
            if rec and rec.get(ident_field) is not None:
                merged[ident_field] = rec[ident_field]
                sources[ident_field] = FieldSource(
                    table_suffix=table_suffix,
                    notice_date=rec.get("notice_date"),
                    record_ref=rec.get("record_ref"),
                )
                break

    standardized = [bool(rec.get("__standardized__")) for rec in records.values() if rec]
    if any(standardized) and not all(standardized):
        raise ValueError("cannot merge legacy raw units and standardized records")
    if standardized and all(standardized):
        merged["__value_format__"] = "standard-v1"
    for d in diagnostics:
        d["report_date"] = report_date
    merged["__sources__"] = sources
    return (merged, diagnostics)
