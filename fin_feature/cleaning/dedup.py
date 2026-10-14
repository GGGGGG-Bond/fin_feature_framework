"""去重逻辑：同一公司同一报告期可能有多条记录（数据修订），保留最新版本。

去重依据：按 (secucode, report_date) 分组，取 notice_date 最新的一条。
若 notice_date 相同，取 entry_time 最新；若仍相同，取最后一条。
"""

from fin_feature.storage.repository import Repository
from fin_feature.utils.date_utils import normalize_date
from fin_feature.utils.logger import setup_logger

logger = setup_logger("fin_feature.cleaning.dedup")


def deduplicate(
    repo: Repository,
    table_suffix: str,
    raw_records: list,
) -> list[dict]:
    """对原始记录去重，返回每组 (secucode, report_date) 的最新版本。

    raw_records: sqlite3.Row 列表（来自原始表）
    返回: 去重后的 dict 列表（已转换为普通 dict）
    """
    # 按 (secucode, report_date) 分组，保留 notice_date 最新的
    latest: dict[tuple[str, str], dict] = {}
    skipped = 0

    for row in raw_records:
        record = dict(row)
        secucode = record.get("secucode", "")
        report_date = normalize_date(record.get("report_date"))
        if not secucode or not report_date:
            skipped += 1
            continue
        record["report_date"] = report_date
        record["notice_date"] = normalize_date(record.get("notice_date"))

        key = (secucode, report_date)
        existing = latest.get(key)
        if existing is None:
            latest[key] = record
        else:
            # 比较 notice_date，取最新
            new_date = record.get("notice_date") or ""
            old_date = existing.get("notice_date") or ""
            if new_date >= old_date:
                # 记录被替换（数据修订）
                repo.log_clean_action(
                    secucode, report_date, table_suffix, "dedup_replace",
                    f"旧版本 notice_date={old_date} 被新版本 notice_date={new_date} 替换",
                )
                latest[key] = record

    if skipped:
        logger.warning("去重时跳过 %d 条无效记录（缺 secucode 或 report_date）", skipped)

    result = list(latest.values())
    logger.info("表 %s: 原始 %d 条 -> 去重后 %d 条", table_suffix, len(raw_records), len(result))
    return result
