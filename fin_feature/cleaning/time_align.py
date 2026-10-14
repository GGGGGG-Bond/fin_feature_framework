"""时间对齐：财务数据存在发布滞后，按实际发布日期对齐。

年报通常在次年3-4月发布，季报也有固定披露窗口。
本模块根据报告期推算预期发布窗口，标记披露状态：
- on_time: 在预期窗口内披露
- delayed: 晚于预期窗口披露
- not_disclosed: 未披露（无公告日期）
"""

from fin_feature.storage.repository import Repository
from fin_feature.utils.date_utils import get_disclosure_status, get_report_type
from fin_feature.utils.logger import setup_logger

logger = setup_logger("fin_feature.cleaning.time_align")


def align_time(
    repo: Repository,
    table_suffix: str,
    record: dict,
) -> dict:
    """对单条记录执行时间对齐，添加 disclosure_status 和 report_type 字段。

    返回添加了 disclosure_status 的记录。
    """
    secucode = record.get("secucode", "")
    report_date = record.get("report_date", "")
    notice_date = record.get("notice_date")

    # 推算报告类型（annual/q1/semi_annual/q3）
    report_type = get_report_type(report_date)
    record["report_type"] = report_type

    # 计算披露状态
    status = get_disclosure_status(report_date, notice_date)
    record["disclosure_status"] = status

    if status == "delayed":
        repo.log_clean_action(
            secucode, report_date, table_suffix, "time_align_delayed",
            f"报告期 {report_date} 的公告日期 {notice_date} 晚于预期窗口",
        )
    elif status == "not_disclosed":
        repo.log_clean_action(
            secucode, report_date, table_suffix, "time_align_not_disclosed",
            f"报告期 {report_date} 无公告日期",
        )

    return record
