"""缺失值处理：处理财务数据中的空值、未披露等情况。

策略：
- drop: 核心字段缺失，丢弃整条记录（无法计算特征）
- zero: 填 0（适用于明确为 0 的字段，如未分配）
- keep: 保留 NULL（非核心字段，特征计算时跳过）
"""

from fin_feature.cleaning.rules import get_missing_strategy
from fin_feature.storage.repository import Repository
from fin_feature.utils.logger import setup_logger

logger = setup_logger("fin_feature.cleaning.missing")


def handle_missing(
    repo: Repository,
    table_suffix: str,
    record: dict,
) -> dict | None:
    """对单条记录执行缺失值处理。

    返回处理后的记录；若核心字段缺失需丢弃，返回 None。
    """
    secucode = record.get("secucode", "")
    report_date = record.get("report_date", "")
    dropped_reasons: list[str] = []

    for field, value in list(record.items()):
        if value is not None:
            continue
        strategy = get_missing_strategy(field)

        if strategy == "drop":
            dropped_reasons.append(f"{field}缺失(策略drop)")
            repo.log_clean_action(
                secucode, report_date, table_suffix, "missing_drop",
                f"核心字段 {field} 缺失，记录被丢弃",
            )
        elif strategy == "zero":
            record[field] = 0
            repo.log_clean_action(
                secucode, report_date, table_suffix, "missing_zero",
                f"字段 {field} 缺失，填0",
            )
        # keep: 不处理，保留 None

    if dropped_reasons:
        logger.debug("%s %s 缺失值丢弃: %s", secucode, report_date, "; ".join(dropped_reasons))
        return None

    return record
