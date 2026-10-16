"""去重逻辑：同一公司同一报告期可能有多条记录（数据修订），保留最新版本。

去重依据：按 (secucode, report_date) 分组，取 notice_date 最新的一条。
若 notice_date 相同，取 entry_time 最新；若仍相同，按确定内容摘要选取
并标歧义（T31）。不依赖输入顺序，不声称恢复真实修订先后。
"""

import hashlib
import json

from fin_feature.storage.repository import Repository
from fin_feature.utils.date_utils import normalize_date
from fin_feature.utils.logger import setup_logger

logger = setup_logger("fin_feature.cleaning.dedup")


def _content_digest(record: dict) -> str:
    """计算记录的内容摘要（用于同日版本排序的确定性回退）。

    T31: 当 notice_date 和 entry_time 都相同时，按内容摘要排序确保
    结果确定性可复现，不依赖输入顺序。
    """
    # 取数值字段参与摘要（排除时间戳和 crawled_at 等元信息）
    digest_fields = {
        k: v for k, v in record.items()
        if k not in ("crawled_at", "notice_date", "entry_time")
        and v is not None
    }
    serialized = json.dumps(digest_fields, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def sort_key(record: dict) -> tuple:
    """选值排序键：notice_date → entry_time → content_digest。

    T31: 有效公告日优先；已提供有效 entry_time 时使用该证据；
    仍相同时按确定内容摘要选取。不依赖输入顺序。

    这是阶段一已验收的选值规则，阶段 2 迁移的当前投影复用同一函数，
    避免两套排序漂移（B1）。使用前先按 `deduplicate` 的口径规范化
    report_date 与 notice_date。
    """
    notice_date = record.get("notice_date") or ""
    entry_time = record.get("entry_time") or ""
    return (notice_date, entry_time, _content_digest(record))


def deduplicate(
    repo: Repository,
    table_suffix: str,
    raw_records: list,
) -> list[dict]:
    """对原始记录去重，返回每组 (secucode, report_date) 的最新版本。

    raw_records: sqlite3.Row 列表（来自原始表）
    返回: 去重后的 dict 列表（已转换为普通 dict）

    T31: 同日版本选择不依赖输入顺序。有效公告日优先，已提供有效
    entry_time 时使用该证据；仍相同时按确定内容摘要选取并标歧义。
    没有元信息时不声称恢复真实修订先后。
    """
    # 按 (secucode, report_date) 分组，收集同组全部记录
    groups: dict[tuple[str, str], list[dict]] = {}
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
        groups.setdefault(key, []).append(record)

    if skipped:
        logger.warning("去重时跳过 %d 条无效记录（缺 secucode 或 report_date）", skipped)

    # 每组按排序键选取最新版本
    latest: dict[tuple[str, str], dict] = {}
    for key, records in groups.items():
        secucode, report_date = key
        if len(records) == 1:
            latest[key] = records[0]
            continue

        # T31: 按确定性排序键选取最新版本，不依赖输入顺序
        records.sort(key=sort_key)
        chosen = records[-1]
        prev = records[-2]

        # 检查是否同日同 entry_time（歧义标记）
        chosen_date = chosen.get("notice_date") or ""
        prev_date = prev.get("notice_date") or ""
        chosen_entry = chosen.get("entry_time") or ""
        prev_entry = prev.get("entry_time") or ""

        if chosen_date == prev_date:
            if chosen_entry and chosen_entry == prev_entry:
                # 同日同 entry_time：按内容摘要选取，标歧义
                repo.log_clean_action(
                    secucode, report_date, table_suffix, "dedup_ambiguous",
                    f"同日同 entry_time={chosen_entry} 多版本，按内容摘要选取，"
                    f"不声称恢复真实修订先后",
                )
            elif not chosen_entry and not prev_entry:
                # 同日但无 entry_time：按内容摘要选取，标歧义
                repo.log_clean_action(
                    secucode, report_date, table_suffix, "dedup_ambiguous",
                    f"同日 notice_date={chosen_date} 无 entry_time，按内容摘要选取，"
                    f"不声称恢复真实修订先后",
                )
            else:
                # 同日但 entry_time 不同：用 entry_time 选取
                repo.log_clean_action(
                    secucode, report_date, table_suffix, "dedup_replace",
                    f"同日 notice_date={chosen_date}，按 entry_time 选取最新",
                )
        else:
            # 不同公告日：正常修订替换
            repo.log_clean_action(
                secucode, report_date, table_suffix, "dedup_replace",
                f"旧版本 notice_date={prev_date} 被新版本 notice_date={chosen_date} 替换",
            )
        latest[key] = chosen

    result = list(latest.values())
    logger.info("表 %s: 原始 %d 条 -> 去重后 %d 条", table_suffix, len(raw_records), len(result))
    return result
