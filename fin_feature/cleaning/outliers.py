"""异常值检测：对财务指标设置合理阈值，检测并标记异常值。

检测规则：
1. 阈值检测：字段值超出 (min, max) 范围视为异常，置 NULL 并记日志。
2. 负值合理性：应为正的金额字段（营收、资产等）若为负，视为异常。
"""

from fin_feature.cleaning.rules import (
    AMOUNT_MUST_POSITIVE,
    get_outlier_threshold,
)
from fin_feature.storage.repository import Repository
from fin_feature.utils.logger import setup_logger

logger = setup_logger("fin_feature.cleaning.outliers")


def detect_and_fix(
    repo: Repository,
    table_suffix: str,
    record: dict,
) -> dict:
    """对单条记录执行异常值检测与修正。

    超出阈值的字段置 None，并记录清洗日志。
    返回修正后的记录。
    """
    secucode = record.get("secucode", "")
    report_date = record.get("report_date", "")
    fixed_fields: list[str] = []

    for field, value in list(record.items()):
        if value is None:
            continue
        if not isinstance(value, (int, float)):
            continue

        # 1. 阈值检测
        threshold = get_outlier_threshold(field)
        if threshold is not None:
            lo, hi = threshold
            if value < lo or value > hi:
                record[field] = None
                fixed_fields.append(f"{field}={value}超出阈值[{lo},{hi}]")
                repo.log_clean_action(
                    secucode, report_date, table_suffix, "outlier_threshold",
                    f"{field}={value} 超出阈值[{lo},{hi}]，置NULL",
                )
                continue

        # 2. 负值合理性检测（应为正的字段出现负值）
        if field in AMOUNT_MUST_POSITIVE and value < 0:
            record[field] = None
            fixed_fields.append(f"{field}={value}应为正但为负")
            repo.log_clean_action(
                secucode, report_date, table_suffix, "outlier_negative",
                f"{field}={value} 应为正值但为负，置NULL",
            )

    if fixed_fields:
        logger.debug(
            "%s %s 异常值修正: %s",
            secucode, report_date, "; ".join(fixed_fields),
        )
    return record
