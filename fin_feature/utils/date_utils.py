"""日期处理工具：报告期类型推断、发布窗口计算、上一报告期/去年同期推算。"""

from datetime import datetime, timedelta

from fin_feature.constants import DISCLOSURE_WINDOWS, REPORT_MONTH_TO_TYPE


def parse_date(date_str: str | None) -> datetime | None:
    """解析日期字符串，支持 'YYYY-MM-DD' 和 'YYYY-MM-DD HH:MM:SS'。"""
    if not date_str:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except ValueError:
            continue
    return None


def normalize_date(date_str: str | None) -> str | None:
    """将日期字符串规范化为 'YYYY-MM-DD'。"""
    dt = parse_date(date_str)
    return dt.strftime("%Y-%m-%d") if dt else None


def get_report_type(report_date: str) -> str | None:
    """根据报告期日期推断报告类型（annual/q1/semi_annual/q3）。

    报告期日期形如 '2024-12-31'，按月份判断。
    """
    dt = parse_date(report_date)
    if dt is None:
        return None
    return REPORT_MONTH_TO_TYPE.get(dt.month)


def get_disclosure_window(report_date: str) -> tuple[int, int] | None:
    """根据报告期推算预期发布窗口（月份范围）。

    年报次年1-4月、一季报4月、半年报7-8月、三季报10月。
    """
    report_type = get_report_type(report_date)
    if report_type is None:
        return None
    return DISCLOSURE_WINDOWS.get(report_type)


def get_disclosure_status(report_date: str, notice_date: str | None) -> str:
    """判断披露状态：on_time / delayed / not_disclosed。

    - not_disclosed: 无公告日期
    - on_time: 公告月份在预期窗口内
    - delayed: 公告月份晚于预期窗口
    """
    if not notice_date:
        return "not_disclosed"
    window = get_disclosure_window(report_date)
    if window is None:
        return "unknown"
    notice_dt = parse_date(notice_date)
    if notice_dt is None:
        return "not_disclosed"
    early, late = window
    if notice_dt.month < early:
        # 早于窗口，可能是预告，标记为 on_time（提前披露）
        return "on_time"
    if notice_dt.month <= late:
        return "on_time"
    return "delayed"


def previous_report_date(report_date: str) -> str | None:
    """推算上一报告期日期（环比基准）。

    季度报告期序列：Q1(03-31) -> 半年报(06-30) -> Q3(09-30) -> 年报(12-31) -> Q1...
    """
    dt = parse_date(report_date)
    if dt is None:
        return None
    month = dt.month
    # 上一报告期月份
    prev_month_map = {3: 12, 6: 3, 9: 6, 12: 9}
    prev_month = prev_month_map.get(month)
    if prev_month is None:
        return None
    # Q1(3月)的上一报告期是上一年Q4(12月)，需要年份减一
    # 其他报告期的上一报告期是同年更早的季度
    if prev_month > month:
        prev_year = dt.year - 1
    else:
        prev_year = dt.year
    # 构造上一报告期日期（取该月最后一天）
    if prev_month == 12:
        prev_dt = datetime(prev_year, 12, 31)
    elif prev_month == 3:
        prev_dt = datetime(prev_year, 3, 31)
    elif prev_month == 6:
        prev_dt = datetime(prev_year, 6, 30)
    elif prev_month == 9:
        prev_dt = datetime(prev_year, 9, 30)
    else:
        return None
    return prev_dt.strftime("%Y-%m-%d")


def year_ago_report_date(report_date: str) -> str | None:
    """推算去年同期报告期日期（同比基准）。"""
    dt = parse_date(report_date)
    if dt is None:
        return None
    # 同月同日，年份减一
    try:
        prev_dt = dt.replace(year=dt.year - 1)
        return prev_dt.strftime("%Y-%m-%d")
    except ValueError:
        # 2月29日等边界情况
        prev_dt = dt.replace(year=dt.year - 1, day=dt.day - 1)
        return prev_dt.strftime("%Y-%m-%d")
