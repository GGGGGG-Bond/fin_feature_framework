"""特征计算引擎：遍历所有 (secucode, report_date)，构建上下文，执行所有已注册特征。

上下文构建：
- current: 合并三张清洗表当期数据
- previous: 上一报告期三表合并（环比基准）
- year_ago: 去年同期三表合并（同比基准）

引擎通过 import 三组特征模块触发注册，无需手动注册。
"""

from fin_feature.features import profitability, growth, solvency  # noqa: F401 触发注册
from fin_feature.features.base import FeatureContext
from fin_feature.features.registry import FeatureRegistry
from fin_feature.storage.repository import Repository
from fin_feature.utils.date_utils import (
    previous_report_date,
    year_ago_report_date,
)
from fin_feature.utils.logger import setup_logger

logger = setup_logger("fin_feature.features.engine")


def _row_to_dict(row) -> dict:
    """将 sqlite3.Row 转为 dict。"""
    return dict(row) if row is not None else None


def _merge_three_tables(
    repo: Repository, secucode: str, report_date: str
) -> dict:
    """合并某公司某报告期的三张清洗表数据为一个 dict。

    三表字段无冲突（已在前置映射中保证命名独立），直接合并。
    """
    merged: dict = {}
    for table_suffix in ["financial_indicator", "income_statement", "balance_sheet"]:
        row = repo.get_clean_record(table_suffix, secucode, report_date)
        if row is not None:
            merged.update(dict(row))
    return merged


def _build_context(
    repo: Repository, secucode: str, report_date: str
) -> FeatureContext | None:
    """构建特征计算上下文。

    当期数据必须存在（至少一张表有记录），否则返回 None。
    """
    current = _merge_three_tables(repo, secucode, report_date)
    if not current:
        return None

    # 查找上一报告期（环比）
    prev_date = previous_report_date(report_date)
    previous = None
    if prev_date:
        previous = _merge_three_tables(repo, secucode, prev_date)

    # 查找去年同期（同比）
    ya_date = year_ago_report_date(report_date)
    year_ago = None
    if ya_date:
        year_ago = _merge_three_tables(repo, secucode, ya_date)

    return FeatureContext(
        secucode=secucode,
        report_date=report_date,
        current=current,
        previous=previous,
        year_ago=year_ago,
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
    for secucode, report_date in all_pairs:
        ctx = _build_context(repo, secucode, report_date)
        if ctx is None:
            continue
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
                }
            )
            total += 1

    # 批量写入
    written = repo.batch_upsert_features(feature_records)
    logger.info("特征计算完成，共写入 %d 条特征记录", written)
    return written
