"""东方财富 datacenter-web API 爬虫实现。

接口模式：
  GET https://datacenter-web.eastmoney.com/api/data/v1/get
  参数: reportName, columns, pageSize, pageNumber, sortColumns, sortTypes, filter
  返回: {success, result:{pages, data, count}}

filter 参数必须 URL 编码（requests 的 params 自动处理）。
"""

from fin_feature.crawler.base import BaseCrawler
from fin_feature.utils.logger import setup_logger

logger = setup_logger("fin_feature.crawler.eastmoney")


class EastMoneyCrawler(BaseCrawler):
    """东方财富财务数据爬虫。"""

    def fetch_page(
        self,
        report_name: str,
        page_number: int = 1,
        page_size: int | None = None,
        filters: dict | None = None,
        sort_columns: str = "REPORTDATE",
        sort_types: str = "-1",
    ) -> dict:
        """获取单页数据。返回原始 JSON 响应 dict。"""
        params = {
            "reportName": report_name,
            "columns": "ALL",
            "pageSize": str(page_size or self.page_size),
            "pageNumber": str(page_number),
            "sortColumns": sort_columns,
            "sortTypes": sort_types,
        }
        filter_str = self._build_filter_string(filters)
        if filter_str:
            params["filter"] = filter_str

        data = self._request_with_retry(self.base_url, params)
        return data

    def fetch_all(
        self,
        report_name: str,
        filters: dict | None = None,
        sort_columns: str = "REPORTDATE",
        sort_types: str = "-1",
        max_pages: int | None = None,
    ) -> list[dict]:
        """自动分页获取全部数据。

        先请求第一页获取总页数，再循环获取后续页。
        max_pages 限制最大页数（防止意外拉取过多数据）。
        """
        first = self.fetch_page(
            report_name, page_number=1, filters=filters,
            sort_columns=sort_columns, sort_types=sort_types,
        )
        result = first.get("result")
        if not result:
            logger.warning("reportName=%s 第一页无数据: %s", report_name, first.get("message"))
            return []

        all_data: list[dict] = list(result.get("data", []))
        total_pages = result.get("pages", 1)
        if max_pages is not None:
            total_pages = min(total_pages, max_pages)

        logger.info(
            "reportName=%s 共 %d 页，已获取第1页（%d 条）",
            report_name, total_pages, len(all_data),
        )

        for page in range(2, total_pages + 1):
            page_data = self.fetch_page(
                report_name, page_number=page, filters=filters,
                sort_columns=sort_columns, sort_types=sort_types,
            )
            page_result = page_data.get("result")
            if page_result and page_result.get("data"):
                all_data.extend(page_result["data"])
            logger.info("已获取第 %d/%d 页", page, total_pages)

        logger.info("reportName=%s 共获取 %d 条记录", report_name, len(all_data))
        return all_data

    def fetch_by_company(
        self,
        secucode: str,
        report_name: str,
        start_date: str | None = None,
        end_date: str | None = None,
        max_pages: int | None = None,
    ) -> list[dict]:
        """按公司爬取某报表的全部历史数据，可选报告期日期范围。

        secucode: 如 "002501.SZ"
        start_date/end_date: 报告期日期范围，如 "2024-01-01"
        """
        filters: dict = {"SECUCODE": secucode}
        if start_date:
            filters["REPORTDATE__gte"] = start_date
        if end_date:
            filters["REPORTDATE__lte"] = end_date
        # 主要财务指标接口用 REPORTDATE，利润表/资产负债表用 REPORT_DATE
        # 这里统一用 REPORTDATE，对利润表/资产负债表需用 REPORT_DATE
        # 实际上两个接口字段名不同，这里通过 report_name 判断
        if report_name in ("RPT_DMSK_FN_INCOME", "RPT_DMSK_FN_BALANCE"):
            filters = {"SECUCODE": secucode}
            if start_date:
                filters["REPORT_DATE__gte"] = start_date
            if end_date:
                filters["REPORT_DATE__lte"] = end_date

        return self.fetch_all(
            report_name, filters=filters,
            sort_columns="REPORTDATE" if report_name == "RPT_LICO_FN_CPD" else "REPORT_DATE",
            sort_types="-1",
            max_pages=max_pages,
        )
