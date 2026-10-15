"""东方财富 datacenter-web API 爬虫实现。

接口模式：
  GET https://datacenter-web.eastmoney.com/api/data/v1/get
  参数: reportName, columns, pageSize, pageNumber, sortColumns, sortTypes, filter
  返回: {success, result:{pages, data, count}}

filter 参数必须 URL 编码（requests 的 params 自动处理）。
"""

from fin_feature.crawler.base import BaseCrawler, CrawlerError
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

    @staticmethod
    def _validate_result(result, report_name: str, page_number: int) -> None:
        """校验分页 result 结构。

        stage1-v1: success=true 但 result 缺少 data/pages 或类型错误视为结构异常。
        合法空数据：result 为 dict，data 为空 list，pages 为非负 int。
        缺字段或类型错误应抛出异常，不当作合法空页。
        """
        if not isinstance(result, dict):
            raise CrawlerError(
                f"reportName={report_name} 第 {page_number} 页 result 非对象: "
                f"{type(result).__name__}"
            )
        if "data" not in result:
            raise CrawlerError(
                f"reportName={report_name} 第 {page_number} 页 result 缺少 data 字段"
            )
        if not isinstance(result["data"], list):
            raise CrawlerError(
                f"reportName={report_name} 第 {page_number} 页 data 非列表: "
                f"{type(result['data']).__name__}"
            )
        if "pages" not in result:
            raise CrawlerError(
                f"reportName={report_name} 第 {page_number} 页 result 缺少 pages 字段"
            )
        pages = result["pages"]
        if isinstance(pages, bool) or not isinstance(pages, int) or pages < 0:
            raise CrawlerError(
                f"reportName={report_name} 第 {page_number} 页 pages 非法: {pages}"
            )

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

        stage1-v1: 每页校验 success 和 result 结构；
        失败页抛异常，不返回部分数据（失败单元不提交）。
        合法成功空数据（success=true, result 存在但 data 为空）返回空列表。
        """
        first = self.fetch_page(
            report_name, page_number=1, filters=filters,
            sort_columns=sort_columns, sort_types=sort_types,
        )
        # 校验第一页结构
        if not first.get("success", False):
            raise CrawlerError(
                f"reportName={report_name} 第一页请求失败: {first.get('message', '未知错误')}"
            )
        result = first.get("result")
        if result is None:
            # success=true 但 result=None 视为结构异常
            raise CrawlerError(
                f"reportName={report_name} 第一页 result 结构异常（success=true 但无 result）"
            )

        # stage1-v1: 校验 result 对象结构（data 列表、pages 整数）
        self._validate_result(result, report_name, 1)

        all_data: list[dict] = list(result["data"])
        total_pages = result["pages"]
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
            # 校验每页结构
            if not page_data.get("success", False):
                raise CrawlerError(
                    f"reportName={report_name} 第 {page} 页请求失败: "
                    f"{page_data.get('message', '未知错误')}"
                )
            page_result = page_data.get("result")
            if page_result is None:
                raise CrawlerError(
                    f"reportName={report_name} 第 {page} 页 result 结构异常"
                )
            # stage1-v1: 校验 result 对象结构
            self._validate_result(page_result, report_name, page)
            if page_result["data"]:
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
