"""来源专属响应信封解析与校验。

F1-F3 整改：写入路径（Repository）与只读对账（audit-raw）共用同一份解析结果，
不能让 Repository 只看已登记 attempt 集合、audit 另用 metadata 计数。

契约依据：docs/stage-2/2b-contract.md。本模块只做纯解析与结构校验，不读写文件、
不访问数据库；文件读取由调用方在调用前完成并把 body 字节传入。
"""

from __future__ import annotations

import json
from dataclasses import dataclass


def _is_int_not_bool(value) -> bool:
    """bool 是 int 的子类，须排除——pages=True/count=True 不能当整数接受。"""
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class ParsedEnvelope:
    """一次原体信封解析的结构化结果。

    - pages：源端声明的总页数（财务必需；股票发现固定 1，不分页）
    - count：源端声明的总记录数（可选，缺失为 None）
    - data_rows：本页原体 data/diff 列表的实际长度（F3：取原体，不取 metadata）
    - all_rows_objects：data/diff 每项是否均为对象（F3：非对象元素拒绝）
    - error：解析/结构失败原因（非 None 时该页不能作为有效证据）
    - error_code：失败类型（decode_error/business_error/schema_error），供采集
      路径映射为 attempt outcome；结构错误时 error 与 error_code 同时非 None
    - is_legal_empty：合法空响应（仅财务：第一页、pages 为 0 或 1、data=[]、count 为 0 或缺失）
    - rows：解析出的数据行（财务为 result.data，股票发现为 data.diff）；
      解析失败时为空元组。写入路径直接消费，避免与对账各解析一遍产生分歧。
    """

    pages: int | None = None
    count: int | None = None
    data_rows: int | None = None
    all_rows_objects: bool = False
    error: str | None = None
    error_code: str | None = None
    is_legal_empty: bool = False
    rows: tuple[dict, ...] = ()


def parse_financial_envelope(body: bytes) -> ParsedEnvelope:
    """解析财务响应信封 {success, result:{pages, count, data}}。

    E3：success 必须严格 true；result.data 必须存在且为列表；pages/count 排除 bool。
    E2a：合法空页 pages=0 或 1 且 data=[]。
    F2：合法空是组合条件——仅第一页、pages 为 0 或 1、data=[]、count 为 0 或缺失。
    F3：data_rows 取 len(data)；每项必须为对象。
    """
    try:
        payload = json.loads(body)
    except (ValueError, json.JSONDecodeError) as exc:
        return ParsedEnvelope(error=f"响应非合法 JSON（{exc}）", error_code="decode_error")
    if not isinstance(payload, dict):
        return ParsedEnvelope(
            error=f"响应顶层非对象：{type(payload).__name__}", error_code="schema_error"
        )

    success = payload.get("success")
    if success is not True:
        return ParsedEnvelope(
            error=f"业务失败：success={success!r}（应为 true）",
            error_code="business_error",
        )
    inner = payload.get("result")
    if not isinstance(inner, dict):
        return ParsedEnvelope(error="响应缺少 result 对象", error_code="schema_error")
    data = inner.get("data")
    if not isinstance(data, list):
        return ParsedEnvelope(
            error=f"信封 result.data 缺失或非列表：{type(data).__name__}",
            error_code="schema_error",
        )

    # F3：每项必须为对象——仅"最外层是 list"不满足契约
    all_objects = all(isinstance(item, dict) for item in data)

    pages = inner.get("pages")
    if not _is_int_not_bool(pages) or pages < 0:
        return ParsedEnvelope(
            error=f"信封 pages 非非负整数：{pages!r}", error_code="schema_error"
        )

    count = inner.get("count")
    if count is not None:
        if not _is_int_not_bool(count) or count < 0:
            return ParsedEnvelope(
                error=f"信封 count 非非负整数：{count!r}", error_code="schema_error"
            )

    # F2：合法空是组合条件——pages 为 0 或 1、data=[]、count 为 0 或缺失
    is_legal_empty = (
        pages in (0, 1)
        and len(data) == 0
        and (count is None or count == 0)
    )

    return ParsedEnvelope(
        pages=pages,
        count=count,
        data_rows=len(data),
        all_rows_objects=all_objects,
        is_legal_empty=is_legal_empty,
        rows=tuple(data),
    )


def parse_stock_list_envelope(body: bytes) -> ParsedEnvelope:
    """解析股票发现信封 {rc, data:{total, diff}}。

    E2b：股票发现不分页，不套财务全分页规则。
    F3：data_rows 取 len(diff)；每项必须为对象。
    """
    try:
        payload = json.loads(body)
    except (ValueError, json.JSONDecodeError) as exc:
        return ParsedEnvelope(error=f"响应非合法 JSON（{exc}）", error_code="decode_error")
    if not isinstance(payload, dict):
        return ParsedEnvelope(
            error=f"响应顶层非对象：{type(payload).__name__}", error_code="schema_error"
        )

    for key in ("ret", "rc"):
        if key in payload and (not _is_int_not_bool(payload[key]) or payload[key] != 0):
            return ParsedEnvelope(error="stock discovery business status is not zero", error_code="business_error")
    data = payload.get("data")
    if not isinstance(data, dict):
        return ParsedEnvelope(
            error=f"股票发现响应缺少 data 对象：{type(data).__name__}",
            error_code="schema_error",
        )
    diff = data.get("diff")
    if not isinstance(diff, list):
        return ParsedEnvelope(
            error=f"股票发现 data.diff 缺失或非列表：{type(diff).__name__}",
            error_code="schema_error",
        )

    all_objects = all(isinstance(item, dict) for item in diff)

    total = data.get("total")
    if not _is_int_not_bool(total) or total < 0:
        return ParsedEnvelope(error="stock discovery total must be a non-negative integer", error_code="schema_error")

    return ParsedEnvelope(
        pages=1,  # 股票发现不分页
        count=total,
        data_rows=len(diff),
        all_rows_objects=all_objects,
        rows=tuple(diff),
    )


def parse_envelope(body: bytes, unit_kind: str = "financial") -> ParsedEnvelope:
    """按 unit_kind 分派信封解析。"""
    if unit_kind == "stock_list":
        return parse_stock_list_envelope(body)
    return parse_financial_envelope(body)
