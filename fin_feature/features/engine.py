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


# 金融行业关键词（用于从 industry_name 推断 industry_type）
_FINANCIAL_KEYWORDS = ("银行", "保险", "证券", "金融", "信托", "期货")


def _resolve_industry_type(merged: dict) -> str:
    """从合并数据中解析行业类型。

    T16: 将 industry_name 映射为 industrial/financial/unknown。
    不靠证券名称或数值猜测，仅依据来源提供的行业名称。
    金融企业相关指标返回 None + not_applicable。
    """
    if merged.get("industry_type") in ("financial", "industrial"):
        return merged["industry_type"]
    industry_name = merged.get("industry_name")
    if not industry_name or not isinstance(industry_name, str):
        return "unknown"
    if any(kw in industry_name for kw in _FINANCIAL_KEYWORDS):
        return "financial"
    return "industrial"


def _standardize_merged(merged: dict) -> tuple[dict, list[dict]]:
    """对语义合并后的记录做标准化。

    返回 (标准化值字典, 诊断列表)。
    保留 __sources__ 字段级来源元信息，供跨周期可比性检查使用。
    """
    if merged.get("__value_format__") == "standard-v1":
        std_values = {f: merged.get(f) for f in _STANDARDIZE_FIELDS}
        diags = []
    else:
        std_values, diags = standardize_record(merged, _STANDARDIZE_FIELDS)
    # 保留标识字段（不标准化）
    for ident in ["secucode", "report_date", "security_code", "security_name", "industry_name"]:
        if ident in merged:
            std_values[ident] = merged[ident]
    # 保留字段级来源元信息（P1 #1：语义证据贯穿上下文）
    if "__sources__" in merged:
        std_values["__sources__"] = merged["__sources__"]
    return (std_values, diags)


def _check_period_comparability(
    current: dict | None,
    comparison: dict | None,
    comparison_label: str,
) -> list[dict]:
    """检查两个报告期之间字段级可比性，阻断不可比字段的跨期计算。

    stage1-v1: 来源表名相同不构成财务可比性证据。阶段 1 currency/scope/计量/
    合并范围/修订基准均标 unknown，不臆造。对两期都有有效值的字段：
    - 来源表不同 → 可比性未知 → 将比较期该字段置 None（阻断计算）+ incomparable_basis
    - 来源表相同但无显式可比声明 → 可比性仍未知 → 同样阻断
    受控 fixture 可通过 FieldSource.comparable=True 显式声明同口径，放行计算。
    返回诊断列表。就地修改 comparison 字典以阻断不可比字段。
    """
    if current is None or comparison is None:
        return []

    diags: list[dict] = []
    cur_sources = current.get("__sources__", {})
    cmp_sources = comparison.get("__sources__", {})

    for field in _STANDARDIZE_FIELDS:
        cur_val = current.get(field)
        cmp_val = comparison.get(field)
        if cur_val is None or cmp_val is None:
            continue

        cur_src = cur_sources.get(field)
        cmp_src = cmp_sources.get(field)
        if cur_src is None or cmp_src is None:
            # 缺来源证据 → 可比性未知 → 阻断
            comparison[field] = None
            diags.append({
                "field": field,
                "report_date": comparison.get("report_date"),
                "reason_code": "incomparable_basis",
                "detail": f"{comparison_label}: {field} 缺少来源证据，可比性未知，拒算",
            })
            continue

        # 受控 fixture 显式声明可比 → 放行
        cur_basis = getattr(cur_src, 'comparison_basis', None)
        cmp_basis = getattr(cmp_src, 'comparison_basis', None)
        if cur_basis and cur_basis == cmp_basis and cur_src.table_suffix == cmp_src.table_suffix:
            continue
        if getattr(cur_src, "comparable", False) and getattr(cmp_src, "comparable", False):
            continue

        # 来源表不同 → 可比性未知 → 阻断
        # 来源表相同但无显式可比声明 → 阶段 1 仍标 unknown → 阻断
        if cur_src.table_suffix != cmp_src.table_suffix:
            comparison[field] = None
            diags.append({
                "field": field,
                "report_date": comparison.get("report_date"),
                "reason_code": "incomparable_basis",
                "detail": (
                    f"{comparison_label}: {field} 来源表不一致 "
                    f"(当期={cur_src.table_suffix}, {comparison_label}={cmp_src.table_suffix})，"
                    f"可比性未知，拒算"
                ),
            })
        else:
            # 同表但未显式声明可比 → 阶段 1 unknown → 阻断
            comparison[field] = None
            diags.append({
                "field": field,
                "report_date": comparison.get("report_date"),
                "reason_code": "incomparable_basis",
                "detail": (
                    f"{comparison_label}: {field} 来源表相同({cur_src.table_suffix}) "
                    f"但口径未验证，可比性未知，拒算"
                ),
            })

    return diags


def _compute_self_calculated_ratios(std_values: dict) -> dict:
    """预算自算比率字段，覆盖原 API 值。

    debt_asset_ratio = total_liabilities / total_assets（小数）
    stage1-v1: 资产必须 > 0，负债必须 >= 0；否则输出 None（非法负资产/负负债）。
    """
    liabilities = std_values.get("total_liabilities")
    assets = std_values.get("total_assets")
    if liabilities is None or assets is None:
        std_values["debt_asset_ratio"] = None
    elif assets <= 0:
        # 非法负资产或零资产
        std_values["debt_asset_ratio"] = None
    elif liabilities < 0:
        # 非法负负债
        std_values["debt_asset_ratio"] = None
    else:
        std_values["debt_asset_ratio"] = liabilities / assets
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
            # P1 #1: 跨周期可比性检查（环比基准），阻断不可比字段
            all_diags.extend(_check_period_comparability(current, previous, "上一报告期"))
            # 路径 A 修复：原始字段被阻断后，重新计算派生比率使其同步失效
            previous = _compute_self_calculated_ratios(previous)

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
            # P1 #1: 跨周期可比性检查（同比基准），阻断不可比字段
            all_diags.extend(_check_period_comparability(current, year_ago, "去年同期"))
            # 路径 A 修复：原始字段被阻断后，重新计算派生比率使其同步失效
            year_ago = _compute_self_calculated_ratios(year_ago)

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
                # 路径 B 修复：previous 与 prev_prev 的累计差分需要可比性门禁
                all_diags.extend(_check_period_comparability(
                    previous, prev_prev, "上一期累计基准"
                ))
                # 原始字段被阻断后，重新计算派生比率
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
        # P2 #3: 统一初始化为非 None list，特征计算时可追加原因
        diagnostics=all_diags,
        # T16: 行业元信息贯穿到特征上下文
        industry_type=_resolve_industry_type(merged),
    )


def compute_all(repo: Repository, groups: list[str] | None = None, *, input_mode: str = "legacy", config: dict | None = None) -> int:
    """计算所有特征并写入 features 表。

    groups: 指定计算的特征组列表，None 表示计算全部。
    返回写入的特征记录数。
    """

    if input_mode == "versions":
        from fin_feature.features.versioned import compute_versioned
        return compute_versioned(repo, groups, config)
    if input_mode != "legacy":
        raise ValueError("unknown feature input mode")

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

        # P2 #3: 确保诊断容器非 None，特征计算时可追加原因
        if ctx.diagnostics is None:
            ctx.diagnostics = []

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

        # P2 #3: 特征计算后汇总诊断（含计算时追加的原因）
        for d in ctx.diagnostics:
            reason = d.get("reason_code", "unknown")
            reason_counts[reason] = reason_counts.get(reason, 0) + 1

    # 批量写入
    with repo.unit_of_work():
        written = repo.batch_upsert_features(feature_records)
    logger.info("特征计算完成，共写入 %d 条特征记录", written)
    if reason_counts:
        logger.info("诊断原因统计: %s", reason_counts)
    return written
