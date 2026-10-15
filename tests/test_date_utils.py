"""日期工具测试：报告期推算、上一报告期、去年同期。

重点验证 previous_report_date 的 Q1 bug 修复（T21/T22 回归）。
"""

from fin_feature.utils.date_utils import (
    previous_report_date,
    year_ago_report_date,
    get_report_type,
    get_disclosure_status,
)


class TestPreviousReportDate:
    """上一报告期推算测试。"""

    def test_q1_previous_is_last_year_q4(self):
        """Q1(03-31) 的上一报告期应为上一年 Q4(12-31)，而非同年 Q4。

        这是 P1-2 的关键 bug 修复回归。
        """
        assert previous_report_date("2024-03-31") == "2023-12-31"

    def test_h1_previous_is_q1(self):
        """半年报(06-30) 的上一报告期是 Q1(03-31)。"""
        assert previous_report_date("2024-06-30") == "2024-03-31"

    def test_q3_previous_is_h1(self):
        """三季报(09-30) 的上一报告期是半年报(06-30)。"""
        assert previous_report_date("2024-09-30") == "2024-06-30"

    def test_annual_previous_is_q3(self):
        """年报(12-31) 的上一报告期是三季报(09-30)。"""
        assert previous_report_date("2024-12-31") == "2024-09-30"


class TestYearAgoReportDate:
    """去年同期推算测试。"""

    def test_q1_year_ago(self):
        assert year_ago_report_date("2024-03-31") == "2023-03-31"

    def test_h1_year_ago(self):
        assert year_ago_report_date("2024-06-30") == "2023-06-30"

    def test_q3_year_ago(self):
        assert year_ago_report_date("2024-09-30") == "2023-09-30"

    def test_annual_year_ago(self):
        assert year_ago_report_date("2024-12-31") == "2023-12-31"


class TestReportType:
    """报告类型推断测试。"""

    def test_annual(self):
        assert get_report_type("2024-12-31") == "annual"

    def test_q1(self):
        assert get_report_type("2024-03-31") == "q1"

    def test_semi_annual(self):
        assert get_report_type("2024-06-30") == "semi_annual"

    def test_q3(self):
        assert get_report_type("2024-09-30") == "q3"


class TestDisclosureStatus:
    """披露状态测试。"""

    def test_on_time(self):
        """年报次年 4 月公告 → on_time。"""
        assert get_disclosure_status("2024-12-31", "2025-04-29") == "on_time"

    def test_delayed(self):
        """年报次年 6 月公告 → delayed。"""
        assert get_disclosure_status("2024-12-31", "2025-06-15") == "delayed"

    def test_not_disclosed(self):
        """无公告日期 → not_disclosed。"""
        assert get_disclosure_status("2024-12-31", None) == "not_disclosed"
