"""增量处理机制：支持增量接入新数据，仅处理新增/变更部分。

通过 crawl_watermark 表记录每公司每数据源的最后爬取水位（notice_date）。
增量爬取时，采用有限报告期回扫获取目标期内全部记录，幂等入库，避免全量重跑。
水位用于诊断和修订检测，不用于报告期过滤。
"""

from fin_feature.constants import REPORT_BALANCE_SHEET, REPORT_FINANCIAL_INDICATOR, REPORT_INCOME_STATEMENT
from fin_feature.crawler.eastmoney import EastMoneyCrawler
from fin_feature.storage.field_mapping import map_fields
from fin_feature.storage.repository import Repository
from fin_feature.utils.logger import setup_logger

logger = setup_logger("fin_feature.addons.incremental")

# reportName -> 表后缀
REPORT_TO_SUFFIX = {
    REPORT_FINANCIAL_INDICATOR: "financial_indicator",
    REPORT_INCOME_STATEMENT: "income_statement",
    REPORT_BALANCE_SHEET: "balance_sheet",
}


def crawl_incremental(
    crawler: EastMoneyCrawler,
    repo: Repository,
    secucodes: list[str],
    report_names: list[str] | None = None,
    recent_periods: int | None = None,
) -> dict[str, int]:
    """增量爬取：采用有限报告期回扫获取目标期内全部记录。

    recent_periods: 每公司每表最近 N 个报告期；None 表示不限制（全部历史）。
    水位用于诊断和修订检测，不用于报告期过滤。
    返回 {report_name: 新增记录数}。
    """
    if report_names is None:
        report_names = list(REPORT_TO_SUFFIX.keys())

    stats: dict[str, int] = {}

    for report_name in report_names:
        table_suffix = REPORT_TO_SUFFIX[report_name]
        new_count = 0

        for secucode in secucodes:
            # 读取水位用于诊断，但不用于过滤报告期
            watermark = repo.get_watermark(report_name, secucode)

            # 构建过滤器：按公司查询，不限制报告期（由 recent_periods 控制范围）
            filters: dict = {"SECUCODE": secucode}
            # 利润表/资产负债表用 REPORT_DATE，财务指标用 REPORTDATE
            date_field = "REPORTDATE" if report_name == REPORT_FINANCIAL_INDICATOR else "REPORT_DATE"

            try:
                records = crawler.fetch_all(
                    report_name,
                    filters=filters,
                    sort_columns=date_field,
                    sort_types="-1",
                )
            except Exception as exc:
                logger.error("增量爬取 %s %s 失败：%s", secucode, report_name, exc)
                continue

            if not records:
                continue

            # 若配置了 recent_periods，限制为最近 N 个不同报告期
            if recent_periods is not None and recent_periods > 0:
                seen_dates: set[str] = set()
                filtered: list[dict] = []
                for r in records:
                    rd = r.get("REPORT_DATE") or r.get("REPORTDATE") or ""
                    if rd and rd not in seen_dates:
                        seen_dates.add(rd)
                        filtered.append(r)
                    if len(seen_dates) >= recent_periods:
                        break
                records = filtered

            # 字段映射
            mapped = [map_fields(r, report_name) for r in records]
            written = repo.batch_upsert_raw(table_suffix, mapped)
            new_count += written

            # 更新水位（取最新 notice_date）
            latest_notice = max(
                (r.get("NOTICE_DATE") or r.get("notice_date") or "") for r in records
            )
            if latest_notice:
                repo.update_watermark(report_name, secucode, latest_notice[:10])

        stats[report_name] = new_count
        logger.info("增量爬取 %s: 新增 %d 条", report_name, new_count)

    return stats
