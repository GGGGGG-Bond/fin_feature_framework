"""爬虫模块测试：filter 构建、字段映射、分页逻辑。"""

from fin_feature.crawler.base import BaseCrawler
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
