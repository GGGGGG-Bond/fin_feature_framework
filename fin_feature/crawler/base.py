"""爬虫基类：封装 HTTP 会话、UA 伪装、filter 构建、带重试请求。

子类只需实现 fetch_page / fetch_all，具体数据源逻辑在子类中实现。
"""

import time
from abc import ABC, abstractmethod
from typing import Any

import requests

from fin_feature.crawler.rate_limiter import RateLimiter
from fin_feature.utils.logger import setup_logger

logger = setup_logger("fin_feature.crawler")


def build_filter_string(filters: dict | None) -> str:
    """将 dict 构建为东方财富 API 的 filter 字符串。

    支持 = / >= / <= 操作符。键以 __ 后缀指定操作符，如 REPORTDATE__gte。
    示例: {"SECUCODE": "002501.SZ", "REPORTDATE__gte": "2024-01-01"}
        -> '(SECUCODE="002501.SZ")(REPORTDATE>='2024-01-01')'
    """
    if not filters:
        return ""
    parts = []
    for key, value in filters.items():
        if key.endswith("__gte"):
            field = key[:-5]
            parts.append(f"({field}>='{value}')")
        elif key.endswith("__lte"):
            field = key[:-5]
            parts.append(f"({field}<='{value}')")
        else:
            parts.append(f'({key}="{value}")')
    return "".join(parts)


class CrawlerError(Exception):
    """爬虫异常基类。"""


class BaseCrawler(ABC):
    """爬虫抽象基类，封装通用 HTTP 请求逻辑与反爬策略。"""

    def __init__(
        self,
        base_url: str,
        page_size: int = 50,
        timeout: int = 15,
        min_interval: float = 1.5,
        max_retries: int = 3,
        user_agent: str = "Mozilla/5.0",
        referer: str = "",
    ):
        self.base_url = base_url
        self.page_size = page_size
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Referer": referer,
                "Accept": "application/json, text/plain, */*",
            }
        )
        self.rate_limiter = RateLimiter(min_interval)

    def _build_filter_string(self, filters: dict | None) -> str:
        """将 dict 构建为东方财富 API 的 filter 字符串（委托模块函数）。"""
        return build_filter_string(filters)

    def _request_with_retry(self, url: str, params: dict) -> dict:
        """带重试的 HTTP GET 请求，返回解析后的 JSON。

        - 频率控制：每次请求前等待
        - 重试：网络异常时指数退避（2/4/8 秒）
        - 业务校验：success=false 时抛出 CrawlerError
        """
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            self.rate_limiter.wait()
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                resp.raise_for_status()
                data = resp.json()
                if not data.get("success", False):
                    msg = data.get("message", "未知错误")
                    # 业务错误不重试（如字段名错误），直接抛出
                    raise CrawlerError(f"API 返回失败: {msg}")
                return data
            except (requests.RequestException, CrawlerError) as exc:
                last_exc = exc
                # CrawlerError（业务错误）不重试
                if isinstance(exc, CrawlerError):
                    raise
                wait_sec = 2 ** attempt
                logger.warning(
                    "请求失败（第%d/%d次）: %s，%d秒后重试",
                    attempt, self.max_retries, exc, wait_sec,
                )
                time.sleep(wait_sec)
        raise CrawlerError(f"请求重试 {self.max_retries} 次后仍失败: {last_exc}")

    @abstractmethod
    def fetch_page(
        self,
        report_name: str,
        page_number: int = 1,
        page_size: int | None = None,
        filters: dict | None = None,
        sort_columns: str = "REPORTDATE",
        sort_types: str = "-1",
    ) -> dict:
        """获取单页数据，返回原始 JSON 响应。"""

    @abstractmethod
    def fetch_all(
        self,
        report_name: str,
        filters: dict | None = None,
        sort_columns: str = "REPORTDATE",
        sort_types: str = "-1",
        max_pages: int | None = None,
    ) -> list[dict]:
        """自动分页获取全部数据，返回记录列表。"""
