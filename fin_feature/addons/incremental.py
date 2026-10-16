"""增量处理机制：支持增量接入新数据，仅处理新增/变更部分。

通过 crawl_watermark 表记录每公司每数据源的最后爬取水位（notice_date）。
增量爬取时，采用有限报告期回扫获取目标期内全部记录，幂等入库，避免全量重跑。
水位用于诊断和修订检测，不用于报告期过滤。

stage1-v1:
- N 期选择保留目标期内全部记录（含同报告期修订），不按行截断
- 采集成功单元 = (报表类型 + 公司 + 请求范围)；单元内任一失败不提交 raw、不推进水位
- 非法 N（0/负数/布尔/字符串）请求前拒绝
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


def select_recent_periods(records: list[dict], n: int | None) -> list[dict]:
    """选择最近 N 个不同报告期的全部记录（含同报告期修订）。

    n=None: 不限制，返回全部。
    n 为正整数: 按 report_date 降序取前 N 个不同报告期，保留这些期的全部记录。
    来源按 report_date 排序不保证同一期首条是最新公告，因此保留全部由清洗去重处理。
    """
    if n is None:
        return records

    # 收集不同报告期（保持降序）
    seen_dates: list[str] = []
    for r in records:
        rd = r.get("REPORT_DATE") or r.get("REPORTDATE") or ""
        if rd and rd not in seen_dates:
            seen_dates.append(rd)

    # 取前 N 个报告期
    target_dates = set(seen_dates[:n])

    # 保留目标期内全部记录
    filtered = [
        r for r in records
        if (r.get("REPORT_DATE") or r.get("REPORTDATE") or "") in target_dates
    ]
    return filtered


def validate_recent_periods(n) -> int | None:
    """校验 recent_periods 参数。

    None: 不限制（合法）。
    正整数: 合法。
    0/负数/布尔/字符串: 非法，抛 ValueError。
    """
    if n is None:
        return None
    if isinstance(n, bool):
        raise ValueError(f"recent_periods 不能为布尔值: {n}")
    if not isinstance(n, int):
        raise ValueError(f"recent_periods 必须为整数或 None: {n}")
    if n <= 0:
        raise ValueError(f"recent_periods 必须为正整数或 None: {n}")
    return n


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

    stage1-v1: 采集成功单元 = (报表 + 公司)；单元内失败不提交 raw、不推进水位。
    发生任一采集失败时，返回统计中含 "__failed_units__" 计数，CLI 据此非零退出。
    """
    # 请求前校验 N
    recent_periods = validate_recent_periods(recent_periods)

    if report_names is None:
        report_names = list(REPORT_TO_SUFFIX.keys())

    stats: dict[str, int] = {}
    failed_units = 0

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

            # 采集成功单元：(报表 + 公司)
            # 单元内任一失败 → 不提交 raw、不推进水位
            try:
                records = crawler.fetch_all(
                    report_name,
                    filters=filters,
                    sort_columns=date_field,
                    sort_types="-1",
                )
            except Exception as exc:
                logger.error("增量爬取 %s %s 失败（单元失败，不提交）：%s", secucode, report_name, exc)
                failed_units += 1
                continue

            if not records:
                continue

            # N 期选择：保留目标期内全部记录（含修订）
            if recent_periods is not None:
                records = select_recent_periods(records, recent_periods)

            # 字段映射
            mapped = [map_fields(r, report_name) for r in records]

            # R1 修复：成功单元原子提交 raw + 水位
            # 所有写入方法不自行提交（见 repository.py），由 unit_of_work 统一提交/回滚。
            try:
                with repo.unit_of_work():
                    written = repo.batch_upsert_raw(table_suffix, mapped)
                    new_count += written

                    # 更新水位（取最新 notice_date）
                    latest_notice = max(
                        (r.get("NOTICE_DATE") or r.get("notice_date") or "") for r in records
                    )
                    if latest_notice:
                        repo.update_watermark(report_name, secucode, latest_notice[:10])
            except Exception as exc:
                # 单元内写入失败：unit_of_work 自动回滚，不残留部分数据
                logger.error("增量写入 %s %s 失败（单元回滚）：%s", secucode, report_name, exc)
                failed_units += 1
                continue

        stats[report_name] = new_count
        logger.info("增量爬取 %s: 新增 %d 条", report_name, new_count)

    # 失败单元汇总：CLI 据此非零退出
    if failed_units:
        stats["__failed_units__"] = failed_units
        logger.error("增量爬取完成，但 %d 个单元失败", failed_units)

    return stats
