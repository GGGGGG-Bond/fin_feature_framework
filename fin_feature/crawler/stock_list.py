"""股票列表爬虫：从东方财富 push2 接口获取 A 股代码列表。

接口：https://push2.eastmoney.com/api/qt/clist/get
参数：pn(页码), pz(每页数), po(排序方向), fs(市场筛选), fields(字段)
f13 字段区分市场：0=深交所(SZ), 1=上交所(SH)
"""

import requests

from fin_feature.crawler.rate_limiter import RateLimiter
from fin_feature.utils.logger import setup_logger

logger = setup_logger("fin_feature.crawler.stock_list")

# push2 接口字段：f12=代码, f14=名称, f13=市场代码
PUSH2_FIELDS = "f12,f13,f14"
# m:0 t:6 深圳A股, m:1 t:2 上海A股
MARKET_FILTER = "m:0 t:6,m:1 t:2"


class StockListCrawler:
    """A 股股票列表爬虫。"""

    def __init__(
        self,
        stock_list_url: str,
        timeout: int = 15,
        min_interval: float = 1.5,
        user_agent: str = "Mozilla/5.0",
    ):
        self.url = stock_list_url
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})
        self.rate_limiter = RateLimiter(min_interval)

    def fetch_stock_list(self, count: int = 50) -> list[dict]:
        """获取指定数量的 A 股列表。

        返回 [{secucode, security_code, security_name, market}]。
        secucode 形如 "002501.SZ"，与财务数据接口的 SECUCODE 一致。
        """
        records: list[dict] = []
        page = 1
        page_size = min(count, 100)

        while len(records) < count:
            self.rate_limiter.wait()
            params = {
                "pn": str(page),
                "pz": str(page_size),
                "po": "1",
                "np": "1",
                "fltt": "2",
                "invt": "2",
                "fs": MARKET_FILTER,
                "fields": PUSH2_FIELDS,
            }
            try:
                resp = self.session.get(self.url, params=params, timeout=self.timeout)
                resp.raise_for_status()
                data = resp.json()
            except (requests.RequestException, ValueError) as exc:
                logger.error("获取股票列表第%d页失败: %s", page, exc)
                break

            items = data.get("data", {}).get("diff", [])
            if not items:
                logger.warning("第%d页无数据，停止", page)
                break

            for item in items:
                code = item.get("f12", "")
                name = item.get("f14", "")
                market_code = item.get("f13", 0)
                # 市场代码 0=SZ, 1=SH
                market = "SZ" if market_code == 0 else "SH"
                secucode = f"{code}.{market}"
                records.append(
                    {
                        "secucode": secucode,
                        "security_code": code,
                        "security_name": name,
                        "market": market,
                    }
                )
                if len(records) >= count:
                    break

            logger.info("已获取 %d 只股票（第%d页）", len(records), page)
            page += 1

        logger.info("共获取 %d 只股票", len(records))
        return records[:count]
