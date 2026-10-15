"""增量处理测试：水位记录、仅获取新数据、水位更新、N 期选择。

对应契约 T06-T09。
"""

import pytest

from fin_feature.addons.incremental import (
    crawl_incremental,
    select_recent_periods,
    validate_recent_periods,
)
from fin_feature.storage.repository import Repository


def test_watermark_persistence(repo):
    """水位应持久化并可读取。"""
    assert repo.get_watermark("RPT_LICO_FN_CPD", "002501.SZ") is None
    repo.update_watermark("RPT_LICO_FN_CPD", "002501.SZ", "2025-04-29")
    assert repo.get_watermark("RPT_LICO_FN_CPD", "002501.SZ") == "2025-04-29"


def test_watermark_update(repo):
    """水位应可更新为更新的值。"""
    repo.update_watermark("RPT_LICO_FN_CPD", "002501.SZ", "2025-04-29")
    repo.update_watermark("RPT_LICO_FN_CPD", "002501.SZ", "2025-10-30")
    assert repo.get_watermark("RPT_LICO_FN_CPD", "002501.SZ") == "2025-10-30"


def test_watermark_per_source(repo):
    """不同数据源的水位应独立。"""
    repo.update_watermark("RPT_LICO_FN_CPD", "002501.SZ", "2025-04-29")
    repo.update_watermark("RPT_DMSK_FN_INCOME", "002501.SZ", "2025-04-30")
    assert repo.get_watermark("RPT_LICO_FN_CPD", "002501.SZ") == "2025-04-29"
    assert repo.get_watermark("RPT_DMSK_FN_INCOME", "002501.SZ") == "2025-04-30"


def test_watermark_per_company(repo):
    """不同公司的水位应独立。"""
    repo.update_watermark("RPT_LICO_FN_CPD", "002501.SZ", "2025-04-29")
    repo.update_watermark("RPT_LICO_FN_CPD", "600000.SH", "2025-04-28")
    assert repo.get_watermark("RPT_LICO_FN_CPD", "002501.SZ") == "2025-04-29"
    assert repo.get_watermark("RPT_LICO_FN_CPD", "600000.SH") == "2025-04-28"


# ---------------------------------------------------------------------------
# N 期选择测试（T06-T09）
# ---------------------------------------------------------------------------

class TestSelectRecentPeriods:
    """T06-T09: N 期选择保留目标期内全部记录（含修订）。"""

    def test_none_returns_all(self):
        """N=None 不限制，返回全部。"""
        records = [
            {"REPORTDATE": "2024-12-31", "NOTICE_DATE": "2025-04-29"},
            {"REPORTDATE": "2024-09-30", "NOTICE_DATE": "2024-10-30"},
        ]
        result = select_recent_periods(records, None)
        assert len(result) == 2

    def test_preserves_revisions_in_same_period(self):
        """T06: 同一报告期多公告（修订）全部保留。"""
        records = [
            {"REPORTDATE": "2024-12-31", "NOTICE_DATE": "2025-03-01", "TOTAL_OPERATE_INCOME": 100},
            {"REPORTDATE": "2024-12-31", "NOTICE_DATE": "2025-04-01", "TOTAL_OPERATE_INCOME": 120},  # 修订
            {"REPORTDATE": "2024-09-30", "NOTICE_DATE": "2024-10-30", "TOTAL_OPERATE_INCOME": 80},
        ]
        # N=2：保留 2024-12-31（2条）和 2024-09-30（1条）
        result = select_recent_periods(records, 2)
        assert len(result) == 3  # 全部保留（2期×含修订）
        # 修订版本 120 也在结果中
        values = [r["TOTAL_OPERATE_INCOME"] for r in result]
        assert 120 in values

    def test_fewer_than_n_no_padding(self):
        """T07: 来源少于 N 期不补造。"""
        records = [
            {"REPORTDATE": "2024-12-31", "NOTICE_DATE": "2025-04-29"},
        ]
        result = select_recent_periods(records, 4)
        assert len(result) == 1  # 只有 1 期，不补造

    def test_n_limits_periods(self):
        """N 限制报告期数量。"""
        records = [
            {"REPORTDATE": "2024-12-31"},
            {"REPORTDATE": "2024-09-30"},
            {"REPORTDATE": "2024-06-30"},
            {"REPORTDATE": "2024-03-31"},
        ]
        result = select_recent_periods(records, 2)
        # 只保留最近 2 期
        dates = {r["REPORTDATE"] for r in result}
        assert dates == {"2024-12-31", "2024-09-30"}


class TestValidateRecentPeriods:
    """T08: 非法 N 请求前拒绝。"""

    def test_none_is_valid(self):
        assert validate_recent_periods(None) is None

    def test_positive_int_is_valid(self):
        assert validate_recent_periods(4) == 4

    def test_zero_rejected(self):
        with pytest.raises(ValueError):
            validate_recent_periods(0)

    def test_negative_rejected(self):
        with pytest.raises(ValueError):
            validate_recent_periods(-1)

    def test_boolean_rejected(self):
        with pytest.raises(ValueError):
            validate_recent_periods(True)

    def test_string_rejected(self):
        with pytest.raises(ValueError):
            validate_recent_periods("4")


# ---------------------------------------------------------------------------
# 失败传播测试（P1 #2：全部采集失败仍以退出码 0 结束）
# ---------------------------------------------------------------------------

class TestCrawlIncrementalFailurePropagation:
    """P1 #2: 发生任一采集失败时返回 __failed_units__ 统计。"""

    def test_all_units_fail_returns_failed_count(self, repo):
        """全部采集单元失败 → 返回 __failed_units__ 计数。"""
        from unittest.mock import MagicMock
        from fin_feature.crawler.base import CrawlerError

        crawler = MagicMock()
        crawler.fetch_all.side_effect = CrawlerError("连接失败")

        stats = crawl_incremental(
            crawler, repo, ["002501.SZ", "600519.SH"],
            report_names=["RPT_LICO_FN_CPD"],
        )

        # 失败单元被统计
        assert stats.get("__failed_units__") == 2
        # 无成功写入
        assert stats.get("RPT_LICO_FN_CPD", 0) == 0

    def test_partial_failure_returns_failed_count(self, repo):
        """部分单元失败 → 失败单元被统计，成功单元正常写入。"""
        from unittest.mock import MagicMock
        from fin_feature.crawler.base import CrawlerError

        crawler = MagicMock()
        # 第一家公司成功，第二家失败
        crawler.fetch_all.side_effect = [
            [{"SECUCODE": "002501.SZ", "REPORTDATE": "2024-12-31",
              "NOTICE_DATE": "2025-04-29", "TOTAL_OPERATE_INCOME": 1000.0}],
            CrawlerError("连接失败"),
        ]

        stats = crawl_incremental(
            crawler, repo, ["002501.SZ", "600519.SH"],
            report_names=["RPT_LICO_FN_CPD"],
        )

        # 1 个失败单元
        assert stats.get("__failed_units__") == 1
        # 成功单元有写入
        assert stats.get("RPT_LICO_FN_CPD", 0) > 0

    def test_no_failure_no_failed_key(self, repo):
        """全部成功 → 不含 __failed_units__ 键。"""
        from unittest.mock import MagicMock

        crawler = MagicMock()
        crawler.fetch_all.return_value = [
            {"SECUCODE": "002501.SZ", "REPORTDATE": "2024-12-31",
             "NOTICE_DATE": "2025-04-29", "TOTAL_OPERATE_INCOME": 1000.0},
        ]

        stats = crawl_incremental(
            crawler, repo, ["002501.SZ"],
            report_names=["RPT_LICO_FN_CPD"],
        )

        assert "__failed_units__" not in stats

