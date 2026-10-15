"""爬虫模块测试：filter 构建、字段映射、分页逻辑、分页失败传播。

对应契约 T03-T05。
"""

from unittest.mock import patch, MagicMock

import pytest

from fin_feature.crawler.base import BaseCrawler, CrawlerError
from fin_feature.crawler.eastmoney import EastMoneyCrawler
from fin_feature.crawler.rate_limiter import RateLimiter
from fin_feature.storage.field_mapping import map_fields


class _DummyCrawler(BaseCrawler):
    """用于测试的 BaseCrawler 实现。"""

    def fetch_page(self, report_name, page_number=1, page_size=None,
                   filters=None, sort_columns="REPORTDATE", sort_types="-1"):
        return {}

    def fetch_all(self, report_name, filters=None, sort_columns="REPORTDATE",
                  sort_types="-1", max_pages=None):
        return []


def test_rate_limiter_enforces_interval():
    """频率控制器应强制最小间隔。"""
    import time
    limiter = RateLimiter(min_interval=0.1)
    limiter.reset()
    start = time.monotonic()
    limiter.wait()
    limiter.wait()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.1


def test_build_filter_string_equality():
    """filter 字符串应正确构建等值过滤。"""
    crawler = _DummyCrawler("http://example.com")
    result = crawler._build_filter_string({"SECUCODE": "002501.SZ"})
    assert result == '(SECUCODE="002501.SZ")'


def test_build_filter_string_gte_lte():
    """filter 字符串应正确构建范围过滤。"""
    crawler = _DummyCrawler("http://example.com")
    result = crawler._build_filter_string({
        "SECUCODE": "002501.SZ",
        "REPORTDATE__gte": "2024-01-01",
        "REPORTDATE__lte": "2024-12-31",
    })
    assert '(SECUCODE="002501.SZ")' in result
    assert "(REPORTDATE>='2024-01-01')" in result
    assert "(REPORTDATE<='2024-12-31')" in result


def test_build_filter_string_empty():
    """无过滤器时应返回空字符串。"""
    crawler = _DummyCrawler("http://example.com")
    assert crawler._build_filter_string(None) == ""
    assert crawler._build_filter_string({}) == ""


def test_map_fields_financial_indicator():
    """字段映射应正确转换大写为 snake_case。"""
    raw = {
        "SECUCODE": "002501.SZ",
        "SECURITY_CODE": "002501",
        "REPORTDATE": "2024-12-31 00:00:00",
        "TOTAL_OPERATE_INCOME": 1000000.0,
        "PARENT_NETPROFIT": 200000.0,
        "WEIGHTAVG_ROE": 15.5,
        "XSMLL": 30.0,
        "UNKNOWN_FIELD": "ignored",
    }
    mapped = map_fields(raw, "RPT_LICO_FN_CPD")
    assert mapped["secucode"] == "002501.SZ"
    assert mapped["report_date"] == "2024-12-31 00:00:00"
    assert mapped["total_operate_income"] == 1000000.0
    assert mapped["parent_netprofit"] == 200000.0
    assert mapped["weightavg_roe"] == 15.5
    assert mapped["gross_margin"] == 30.0
    assert "UNKNOWN_FIELD" not in mapped


def test_map_fields_unknown_report():
    """未知 reportName 应抛出 ValueError。"""
    import pytest
    with pytest.raises(ValueError):
        map_fields({}, "UNKNOWN_REPORT")


def test_eastmoney_crawler_init():
    """EastMoneyCrawler 应正确初始化。"""
    crawler = EastMoneyCrawler("http://example.com", page_size=20)
    assert crawler.page_size == 20
    assert crawler.base_url == "http://example.com"
    assert "User-Agent" in crawler.session.headers


# ---------------------------------------------------------------------------
# 分页失败传播测试（T03-T05）
# ---------------------------------------------------------------------------

class TestPaginationFailure:
    """T03-T05: 分页失败不返回部分数据，传播异常。"""

    def test_second_page_failure_raises(self):
        """T03: 第二页 success=false → 抛异常，不返回第一页部分数据。"""
        crawler = EastMoneyCrawler("http://example.com", min_interval=0)

        # mock fetch_page：第一页成功，第二页失败
        page1 = {
            "success": True,
            "result": {"pages": 2, "data": [{"SECUCODE": "002501.SZ"}]},
        }
        page2 = {"success": False, "message": "服务器错误"}

        with patch.object(crawler, "fetch_page", side_effect=[page1, page2]):
            with pytest.raises(CrawlerError):
                crawler.fetch_all("RPT_LICO_FN_CPD")

    def test_second_page_result_none_raises(self):
        """T03: 第二页 result=None（结构异常）→ 抛异常。"""
        crawler = EastMoneyCrawler("http://example.com", min_interval=0)

        page1 = {
            "success": True,
            "result": {"pages": 2, "data": [{"SECUCODE": "002501.SZ"}]},
        }
        page2 = {"success": True, "result": None}  # 结构异常

        with patch.object(crawler, "fetch_page", side_effect=[page1, page2]):
            with pytest.raises(CrawlerError):
                crawler.fetch_all("RPT_LICO_FN_CPD")

    def test_first_page_success_false_raises(self):
        """T05: 第一页 success=false → 抛异常。"""
        crawler = EastMoneyCrawler("http://example.com", min_interval=0)

        page1 = {"success": False, "message": "参数错误"}

        with patch.object(crawler, "fetch_page", return_value=page1):
            with pytest.raises(CrawlerError):
                crawler.fetch_all("RPT_LICO_FN_CPD")

    def test_empty_success_returns_empty(self):
        """T05: 合法成功空数据（success=true, data=[]）→ 返回空列表。"""
        crawler = EastMoneyCrawler("http://example.com", min_interval=0)

        page1 = {
            "success": True,
            "result": {"pages": 1, "data": []},
        }

        with patch.object(crawler, "fetch_page", return_value=page1):
            result = crawler.fetch_all("RPT_LICO_FN_CPD")
        assert result == []

    def test_single_page_success(self):
        """单页成功 → 返回数据。"""
        crawler = EastMoneyCrawler("http://example.com", min_interval=0)

        page1 = {
            "success": True,
            "result": {
                "pages": 1,
                "data": [{"SECUCODE": "002501.SZ"}, {"SECUCODE": "600519.SH"}],
            },
        }

        with patch.object(crawler, "fetch_page", return_value=page1):
            result = crawler.fetch_all("RPT_LICO_FN_CPD")
        assert len(result) == 2


# ---------------------------------------------------------------------------
# 结构校验测试（P1 #3：缺少 data 的成功分页仍被当作合法空页）
# ---------------------------------------------------------------------------

class TestResultStructureValidation:
    """P1 #3: success=true 但 result 缺少 data/pages 或类型错误应抛异常。"""

    def test_result_missing_data_raises(self):
        """success=true 但 result 缺少 data 字段 → 抛异常。"""
        crawler = EastMoneyCrawler("http://example.com", min_interval=0)
        page1 = {"success": True, "result": {"pages": 1}}  # 缺 data

        with patch.object(crawler, "fetch_page", return_value=page1):
            with pytest.raises(CrawlerError):
                crawler.fetch_all("RPT_LICO_FN_CPD")

    def test_result_data_not_list_raises(self):
        """data 非列表 → 抛异常。"""
        crawler = EastMoneyCrawler("http://example.com", min_interval=0)
        page1 = {"success": True, "result": {"pages": 1, "data": "not_a_list"}}

        with patch.object(crawler, "fetch_page", return_value=page1):
            with pytest.raises(CrawlerError):
                crawler.fetch_all("RPT_LICO_FN_CPD")

    def test_result_missing_pages_raises(self):
        """result 缺少 pages 字段 → 抛异常。"""
        crawler = EastMoneyCrawler("http://example.com", min_interval=0)
        page1 = {"success": True, "result": {"data": []}}  # 缺 pages

        with patch.object(crawler, "fetch_page", return_value=page1):
            with pytest.raises(CrawlerError):
                crawler.fetch_all("RPT_LICO_FN_CPD")

    def test_result_pages_not_int_raises(self):
        """pages 非整数 → 抛异常。"""
        crawler = EastMoneyCrawler("http://example.com", min_interval=0)
        page1 = {"success": True, "result": {"pages": "2", "data": []}}

        with patch.object(crawler, "fetch_page", return_value=page1):
            with pytest.raises(CrawlerError):
                crawler.fetch_all("RPT_LICO_FN_CPD")

    def test_result_not_dict_raises(self):
        """result 非对象 → 抛异常。"""
        crawler = EastMoneyCrawler("http://example.com", min_interval=0)
        page1 = {"success": True, "result": "unexpected_string"}

        with patch.object(crawler, "fetch_page", return_value=page1):
            with pytest.raises(CrawlerError):
                crawler.fetch_all("RPT_LICO_FN_CPD")

    def test_empty_data_list_is_valid(self):
        """合法空数据：data=[] 且 pages=1 → 返回空列表（不抛异常）。"""
        crawler = EastMoneyCrawler("http://example.com", min_interval=0)
        page1 = {"success": True, "result": {"pages": 1, "data": []}}

        with patch.object(crawler, "fetch_page", return_value=page1):
            result = crawler.fetch_all("RPT_LICO_FN_CPD")
        assert result == []
