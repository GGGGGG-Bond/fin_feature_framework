"""2B-2 测试共用：可脚本化 HTTP 会话、接收时钟与财务行构造。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fin_feature.constants import REPORT_FINANCIAL_INDICATOR

SOURCE = "eastmoney"
URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"


class FakeResponse:
    """requests.Response 的最小替身：content 为真实响应字节。"""

    def __init__(self, *, status_code: int = 200, body: bytes = b"",
                 headers: dict | None = None, encoding: str | None = None):
        self.status_code = status_code
        self.content = body
        self.headers = headers if headers is not None else {"Content-Type": "application/json"}
        self.encoding = encoding


class FakeSession:
    """按 pageNumber 路由的脚本化会话；未配置的页返回 200 空页。

    routes: {page_number: [FakeResponse | Exception, ...]}，每页按调用次序消费；
    列表耗尽后重复最后一项（便于测试无限失败/无限成功）。
    """

    def __init__(self, routes: dict | None = None):
        self.routes = {page: list(items) for page, items in (routes or {}).items()}
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None):
        params = dict(params or {})
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        page = int(params.get("pageNumber", 1))
        items = self.routes.get(page)
        if not items:
            return FakeResponse(body=body_for([]))
        item = items.pop(0) if len(items) > 1 else items[0]
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def requested_pages(self) -> list[int]:
        return [int(call["params"].get("pageNumber", 1)) for call in self.calls]


class StepClock:
    """每次调用返回递增的 UTC 秒级时间戳（模拟真实接收时间）。"""

    def __init__(self, start: str = "2026-10-17T08:00:00Z", step_seconds: int = 1):
        self._current = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        self._step = timedelta(seconds=step_seconds)

    def __call__(self) -> str:
        value = self._current.strftime("%Y-%m-%dT%H:%M:%SZ")
        self._current += self._step
        return value


class SleepRecorder:
    """记录退避等待，不真实睡眠。"""

    def __init__(self):
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def body_for(rows, *, pages: int = 1, count=None, success: bool = True,
             result: dict | None = None, message: str | None = None) -> bytes:
    """构造真实响应体字节（默认财务信封）。"""
    if result is None:
        payload = {"success": success, "result": {"pages": pages, "data": list(rows)}}
        if count is not None:
            payload["result"]["count"] = count
        if message is not None:
            payload["message"] = message
    else:
        payload = {"success": success, "result": result}
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def fi_row(*, secucode="000001.SZ", report_date="2024-03-31",
           notice_date="2024-04-20", entry_time=None, income=100.0,
           roe=16.75, current_ratio=1.5, **extra) -> dict:
    """主要财务指标原始行（接口大写字段）。"""
    row = {
        "SECUCODE": secucode,
        "SECURITY_CODE": secucode.split(".")[0],
        "SECURITY_NAME_ABBR": "测试公司",
        "REPORTDATE": report_date,
        "NOTICE_DATE": notice_date,
        "EITIME": entry_time,
        "TOTAL_OPERATE_INCOME": income,
        "WEIGHTAVG_ROE": roe,
        "CURRENT_RATIO": current_ratio,
    }
    row.update(extra)
    return row


def version_rows(conn, secucode="000001.SZ", table_type="financial_indicator"):
    return conn.execute(
        "SELECT * FROM statement_version WHERE secucode = ? AND table_type = ? "
        "ORDER BY observation_seq",
        (secucode, table_type),
    ).fetchall()


def observation_rows(conn):
    return conn.execute(
        "SELECT * FROM statement_observation ORDER BY observed_at, observation_id"
    ).fetchall()


def raw_rows(conn, table="raw_financial_indicator", secucode="000001.SZ"):
    return conn.execute(
        f"SELECT * FROM {table} WHERE secucode = ? ORDER BY report_date, notice_date",
        (secucode,),
    ).fetchall()


def version_of(conn, version_id):
    return conn.execute(
        "SELECT * FROM statement_version WHERE version_id = ?", (version_id,)
    ).fetchone()


def current_versions(conn, secucode="000001.SZ", table_type="financial_indicator"):
    return conn.execute(
        "SELECT * FROM statement_version WHERE secucode = ? AND table_type = ? "
        "AND is_superseded = 0",
        (secucode, table_type),
    ).fetchall()


def count_rows(conn, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


FIN = REPORT_FINANCIAL_INDICATOR
