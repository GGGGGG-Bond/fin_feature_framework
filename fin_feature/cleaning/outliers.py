"""异常值检测：对财务指标设置合理阈值，检测并标记异常值。

检测规则：
1. 阈值检测：字段标准化后超出 (min, max) 范围视为异常，记日志但不清空原值。
2. 负值合理性：应为正的金额字段（营收、资产等）若为负，视为异常。

stage1-v1: 阈值使用标准单位（小数），检测前先标准化原值。
clean 表保留原计量，超标只记 clean_log 诊断，不置 NULL。
"""

from fin_feature.cleaning.rules import (
    AMOUNT_MUST_POSITIVE,
    get_outlier_threshold,
)
from fin_feature.features.standardize import standardize
from fin_feature.storage.repository import Repository
from fin_feature.utils.logger import setup_logger

logger = setup_logger("fin_feature.cleaning.outliers")


def detect_and_fix(
    repo: Repository,
    table_suffix: str,
    record: dict,
) -> dict:
    """对单条记录执行异常值检测。

    stage1-v1: 标准化后判断阈值；超标只记 clean_log，不清空原值。
    返回原记录（不修改数值字段）。
    """
    secucode = record.get("secucode", "")
    report_date = record.get("report_date", "")
    flagged_fields: list[str] = []

    for field, value in list(record.items()):
        if value is None:
            continue
        if not isinstance(value, (int, float)):
            continue
        # 布尔值不算数值
        if isinstance(value, bool):
            continue

        # 1. 阈值检测（标准化后判断）
        threshold = get_outlier_threshold(field)
        if threshold is not None:
            # 标准化原值后再判断
            std_value, reason = standardize(field, value)
            if std_value is not None:
                lo, hi = threshold
                if std_value < lo or std_value > hi:
                    # 超标：记日志，不清空原值
                    flagged_fields.append(
                        f"{field}={value}(标准值{std_value})超出阈值[{lo},{hi}]"
                    )
                    repo.log_clean_action(
                        secucode, report_date, table_suffix, "outlier_threshold",
                        f"{field}={value} 标准化后={std_value} 超出阈值[{lo},{hi}]，原值保留",
                    )
                    continue

        # 2. 负值合理性检测（应为正的字段出现负值）
        if field in AMOUNT_MUST_POSITIVE and value < 0:
            flagged_fields.append(f"{field}={value}应为正但为负")
            repo.log_clean_action(
                secucode, report_date, table_suffix, "outlier_negative",
                f"{field}={value} 应为正值但为负，原值保留",
            )

    if flagged_fields:
        logger.debug(
            "%s %s 异常值标记: %s",
            secucode, report_date, "; ".join(flagged_fields),
        )
    return record
