"""增量处理机制：支持增量接入新数据，仅处理新增/变更部分。

通过 crawl_watermark 表记录每公司每数据源的最后爬取水位（notice_date）。
增量爬取时，仅获取水位之后的新数据，避免全量重跑。
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
) -> dict[str, int]:
    """增量爬取：仅获取每公司水位之后的新数据。

    返回 {report_name: 新增记录数}。
    """
    if report_names is None:
        report_names = list(REPORT_TO_SUFFIX.keys())

    stats: dict[str, int] = {}

    for report_name in report_names:
        table_suffix = REPORT_TO_SUFFIX[report_name]
        new_count = 0

        for secucode in secucodes:
            # 读取水位
            watermark = repo.get_watermark(report_name, secucode)

            # 构建过滤器：只取水位之后的数据
            filters: dict = {"SECUCODE": secucode}
            # 利润表/资产负债表用 REPORT_DATE，财务指标用 REPORTDATE
            date_field = "REPORTDATE" if report_name == REPORT_FINANCIAL_INDICATOR else "REPORT_DATE"
            if watermark:
                filters[f"{date_field}__gte"] = watermark

            try:
                records = crawler.fetch_all(
                    report_name,
                    filters=filters,
                    sort_columns=date_field,
                    sort_types="-1",
                )
            except Exception as exc:
                logger.error("增量爬取 %s %s 失败: %s", secucode, report_name, exc)
                continue

            if not records:
                continue

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
