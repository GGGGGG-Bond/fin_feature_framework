"""特征计算引擎：遍历所有 (secucode, report_date)，构建上下文，执行所有已注册特征。

上下文构建：
- current/previous/year_ago: 标准化后的累计值（供同比特征）
- current_sq/previous_sq: 标准化后的单季值（供环比特征）
- 语义合并保留字段级来源，空值不覆盖有效值，冲突/回退有诊断

引擎通过 import 三组特征模块触发注册，无需手动注册。
"""

from fin_feature.features import profitability, growth, solvency  # noqa: F401 触发注册
from fin_feature.features.base import FeatureContext
from fin_feature.features.registry import FeatureRegistry
from fin_feature.features.semantic import (
    merge_three_tables_semantic,
    merge_three_tables_semantic_with_fallback_flag,
)
from fin_feature.features.standardize import standardize, standardize_record
from fin_feature.storage.repository import Repository
from fin_feature.utils.date_utils import (
    previous_report_date,
    year_ago_report_date,
)
from fin_feature.utils.logger import setup_logger

logger = setup_logger("fin_feature.features.engine")

# 阶段 1 定义版本
DEFINITION_VERSION = "stage1-v1"

# 累计流量字段（需要转单季）
_FLOW_YTD_FIELDS = ["total_operate_income", "parent_netprofit"]

# 时点余额字段（不差分，直接复制）
_STOCK_END_FIELDS = [
    "total_assets", "total_liabilities", "total_equity",
    "current_ratio", "monetary_funds", "accounts_receivable",
    "inventory", "accounts_payable", "fixed_asset",
]

# 需要标准化的字段（用于特征上下文）
_STANDARDIZE_FIELDS = [
    "total_operate_income", "parent_netprofit", "operate_cost",
    "total_operate_cost", "operate_profit", "total_profit",
    "income_tax", "deduct_parent_netprofit",
    "weightavg_roe", "gross_margin", "basic_eps", "bps",
    "total_assets", "total_liabilities", "total_equity",
    "current_ratio",
]


def _row_to_dict(row) -> dict:
    """将 sqlite3.Row 转为 dict。"""
    return dict(row) if row is not None else None


def _standardize_merged(merged: dict) -> tuple[dict, list[dict]]:
    """对语义合并后的记录做标准化。

    返回 (标准化值字典, 诊断列表)。
    去除 __sources__ 等元信息键。
    """
    std_values, diags = standardize_record(merged, _STANDARDIZE_FIELDS)
    # 保留标识字段（不标准化）
    for ident in ["secucode", "report_date", "security_code", "security_name", "industry_name"]:
        if ident in merged:
            std_values[ident] = merged[ident]
    return (std_values, diags)


def _compute_self_calculated_ratios(std_values: dict) -> dict:
    """预算自算比率字段，覆盖原 API 值。

    debt_asset_ratio = total_liabilities / total_assets（小数）
    """
    liabilities = std_values.get("total_liabilities")
    assets = std_values.get("total_assets")
    if liabilities is not None and assets is not None and assets != 0:
        std_values["debt_asset_ratio"] = liabilities / assets
    else:
        std_values["debt_asset_ratio"] = None
    return std_values


def _compute_single_quarter(
    report_date: str,
    current_cum: dict | None,
    prev_cum: dict | None,
    prev_prev_cum: dict | None,
) -> tuple[dict | None, str | None]:
    """将累计流量转为单季值。

    Q1(03-31): sq = cumQ1（本身就是单季）
    H1(06-30): sq = cumH1 - cumQ1
    Q3(09-30): sq = cum9M - cumH1
    FY(12-31): sq = cumFY - cum9M

    时点余额字段直接复制（不差分）。

    返回 (单季值字典, reason_code)。
    reason_code 为 None 表示成功，否则为失败原因。
    """
    if current_cum is None:
        return (None, "missing_input")

    from fin_feature.utils.date_utils import parse_date
    dt = parse_date(report_date)
    if dt is None:
        return (None, "invalid_value")

    month = dt.month
    sq: dict = {}

    # 时点余额字段：直接复制当期值
    for f in _STOCK_END_FIELDS:
        sq[f] = current_cum.get(f)

    # 累计流量字段：按季度差分
    for f in _FLOW_YTD_FIELDS:
        current_val = current_cum.get(f)
        if current_val is None:
            sq[f] = None
            continue

        if month == 3:
            # Q1: 本身就是单季
            sq[f] = current_val
        elif month == 6:
            # H1: cumH1 - cumQ1
            if prev_cum is None:
                sq[f] = None
            else:
                prev_val = prev_cum.get(f)
                if prev_val is None:
                    sq[f] = None
                else:
                    sq[f] = current_val - prev_val
        elif month == 9:
            # Q3: cum9M - cumH1
            if prev_cum is None:
                sq[f] = None
            else:
                prev_val = prev_cum.get(f)
                if prev_val is None:
                    sq[f] = None
                else:
                    sq[f] = current_val - prev_val
        elif month == 12:
            # FY: cumFY - cum9M
            if prev_cum is None:
                sq[f] = None
            else:
                prev_val = prev_cum.get(f)
                if prev_val is None:
                    sq[f] = None
                else:
                    sq[f] = current_val - prev_val
        else:
            sq[f] = None

    # 检查是否所有流量字段都缺失（缺上一期）
    flow_vals = [sq.get(f) for f in _FLOW_YTD_FIELDS]
    if month != 3 and all(v is None for v in flow_vals):
        return (None, "missing_comparison_period")

    return (sq, None)


def _build_context(
    repo: Repository, secucode: str, report_date: str
) -> FeatureContext | None:
    """构建特征计算上下文。

    当期数据必须存在（至少一张表有记录），否则返回 None。
    """
    # 1. 语义合并当期三表
    merged, merge_diags = merge_three_tables_semantic(repo, secucode, report_date)
    # 去除 __sources__ 后检查是否有数据
    has_data = any(
        merged.get(f) is not None
        for f in _STANDARDIZE_FIELDS + ["secucode"]
    )
    if not has_data:
        return None

    # 2. 标准化当期值
    current, std_diags = _standardize_merged(merged)
    current = _compute_self_calculated_ratios(current)

    all_diags = list(merge_diags) + list(std_diags)

    # 3. 查找上一报告期（环比基准）
    prev_date = previous_report_date(report_date)
    previous = None
    previous_sq = None
    if prev_date:
        prev_merged, prev_merge_diags = merge_three_tables_semantic(repo, secucode, prev_date)
        if any(prev_merged.get(f) is not None for f in _STANDARDIZE_FIELDS):
            previous, prev_std_diags = _standardize_merged(prev_merged)
            previous = _compute_self_calculated_ratios(previous)
            all_diags.extend(prev_merge_diags)
            all_diags.extend(prev_std_diags)

    # 4. 查找去年同期（同比基准）
    ya_date = year_ago_report_date(report_date)
    year_ago = None
    if ya_date:
        ya_merged, ya_merge_diags = merge_three_tables_semantic(repo, secucode, ya_date)
        if any(ya_merged.get(f) is not None for f in _STANDARDIZE_FIELDS):
            year_ago, ya_std_diags = _standardize_merged(ya_merged)
            year_ago = _compute_self_calculated_ratios(year_ago)
            all_diags.extend(ya_merge_diags)
            all_diags.extend(ya_std_diags)

    # 5. 计算当期单季值
    current_sq, sq_reason = _compute_single_quarter(
        report_date, current, previous, None
    )
    if sq_reason:
        all_diags.append({
            "field": "current_sq",
            "reason_code": sq_reason,
            "detail": f"当期单季值计算失败: {sq_reason}",
        })

    # 6. 计算上一报告期单季值（需要 prev_prev）
    if prev_date and previous is not None:
        prev_prev_date = previous_report_date(prev_date)
        prev_prev = None
        if prev_prev_date:
            pp_merged, _ = merge_three_tables_semantic(repo, secucode, prev_prev_date)
            if any(pp_merged.get(f) is not None for f in _STANDARDIZE_FIELDS):
                prev_prev, _ = _standardize_merged(pp_merged)
                prev_prev = _compute_self_calculated_ratios(prev_prev)

        previous_sq, prev_sq_reason = _compute_single_quarter(
            prev_date, previous, prev_prev, None
        )
        if prev_sq_reason:
            all_diags.append({
                "field": "previous_sq",
                "reason_code": prev_sq_reason,
                "detail": f"上一期单季值计算失败: {prev_sq_reason}",
            })

    return FeatureContext(
        secucode=secucode,
        report_date=report_date,
        current=current,
        previous=previous,
        year_ago=year_ago,
        current_sq=current_sq,
        previous_sq=previous_sq,
        diagnostics=all_diags if all_diags else None,
    )


def compute_all(repo: Repository, groups: list[str] | None = None) -> int:
    """计算所有特征并写入 features 表。

    groups: 指定计算的特征组列表，None 表示计算全部。
    返回写入的特征记录数。
    """

    # 获取所有已注册的特征类
    if groups:
        feature_classes = []
        for g in groups:
            feature_classes.extend(FeatureRegistry.get_by_group(g))
    else:
        feature_classes = FeatureRegistry.get_all()

    if not feature_classes:
        logger.warning("无已注册特征，请检查特征模块是否被 import")
        return 0

    logger.info("已注册特征 %d 个: %s", len(feature_classes), [f.name for f in feature_classes])

    # 实例化特征
    features = [cls() for cls in feature_classes]

    # 收集所有 (secucode, report_date) 组合（以财务指标表为主）
    all_pairs = repo.get_all_clean_secucode_dates("financial_indicator")
    # 补充利润表和资产负债表中的组合
    for table_suffix in ["income_statement", "balance_sheet"]:
        pairs = repo.get_all_clean_secucode_dates(table_suffix)
        existing = set(all_pairs)
        for p in pairs:
            if p not in existing:
                all_pairs.append(p)
                existing.add(p)

    logger.info("待计算特征的公司-报告期组合: %d", len(all_pairs))

    total = 0
    feature_records: list[dict] = []
    reason_counts: dict[str, int] = {}

    for secucode, report_date in all_pairs:
        ctx = _build_context(repo, secucode, report_date)
        if ctx is None:
            continue

        # 收集诊断原因计数
        if ctx.diagnostics:
            for d in ctx.diagnostics:
                reason = d.get("reason_code", "unknown")
                reason_counts[reason] = reason_counts.get(reason, 0) + 1

        for feature in features:
            try:
                value = feature.calculate(ctx)
            except Exception as exc:
                logger.warning(
                    "计算特征 %s 失败 (%s %s): %s",
                    feature.name, secucode, report_date, exc,
                )
                value = None
            feature_records.append(
                {
                    "secucode": secucode,
                    "report_date": report_date,
                    "feature_name": feature.name,
                    "feature_value": value,
                    "feature_group": feature.group,
                    "definition_version": DEFINITION_VERSION,
                }
            )
            total += 1

    # 批量写入
    written = repo.batch_upsert_features(feature_records)
    logger.info("特征计算完成，共写入 %d 条特征记录", written)
    if reason_counts:
        logger.info("诊断原因统计: %s", reason_counts)
    return written
