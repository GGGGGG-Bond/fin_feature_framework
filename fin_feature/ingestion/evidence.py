"""采集单元身份与证据充分性证明。

契约依据：docs/stage-2/2b-contract.md 第 2、6 节，docs/stage-2/2b-schema.md。

- unit_key：来源 + 单元类型 + 报表 + 公司 + 规范化请求范围的 canonical JSON
  SHA-256。它是"逻辑请求范围"的稳定摘要，与页号、HTTP 重试号无关。
- UnitEvidence：本次业务事务内对"单元页证据完整"的证明，交 Repository 核验，
  避免"必须先提交 unit.success 才能写版本"的循环依赖。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Callable

from fin_feature.storage.hashing import canonical_json

# 固定的来源字段映射版本：进入请求范围摘要，映射定义变更需另行版本设计
MAPPING_VERSION = "eastmoney-stage1-v1"

UNIT_KIND_FINANCIAL = "financial"
UNIT_KIND_STOCK_LIST = "stock_list"

# 股票发现单元的固定标识（secucode 为空串，报表名为 stock_list）
STOCK_LIST_REPORT_NAME = "stock_list"


class CaptureConflict(Exception):
    """同一来源身份（snapshot + row_index / attempt）出现不同内容或元信息。

    不可变证据冲突必须显式失败：既不静默改旧观察，也不部分恢复当前投影。
    """


def normalize_request_scope(request_scope: dict | str) -> str:
    """把请求范围规范化为 canonical JSON（键序稳定，便于摘要与比对）。"""
    if isinstance(request_scope, str):
        try:
            request_scope = json.loads(request_scope)
        except json.JSONDecodeError as exc:
            raise ValueError(f"request_scope 不是有效 JSON：{exc}") from exc
    if not isinstance(request_scope, dict):
        raise ValueError("request_scope 必须是 JSON 对象或对象字符串")
    return canonical_json(request_scope)


def resolve_max_pages_per_unit(config) -> int:
    """解析 crawl.max_pages_per_unit（默认 1000，正整数，两个 CLI 共用）。

    超过上限是失败条件，不是"截断成功"：调用方必须按失败处理。
    """
    value = config.get("crawl", "max_pages_per_unit", default=1000) \
        if config is not None else 1000
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"crawl.max_pages_per_unit 必须为正整数，当前为 {value!r}")
    return value


def compute_unit_key(
    *,
    source_name: str,
    unit_kind: str,
    report_name: str,
    secucode: str,
    request_scope: dict | str,
) -> str:
    """计算 unit_key：逻辑请求范围的 canonical JSON SHA-256。

    同一语义（键序不同）得到同一 key；页大小、筛选、来源、报表、公司、
    最近期数等范围参数改变会得到不同 key，因此不会混入同一单元。
    """
    if unit_kind not in (UNIT_KIND_FINANCIAL, UNIT_KIND_STOCK_LIST):
        raise ValueError(f"未知 unit_kind：{unit_kind!r}")
    payload = {
        "source_name": source_name,
        "unit_kind": unit_kind,
        "report_name": report_name,
        "secucode": secucode,
        "request_scope": json.loads(normalize_request_scope(request_scope)),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class UnitEvidence:
    """单元完整性证明：本次业务事务内由协调器给出，供 Repository 核验。

    complete：全部请求范围已完成（分页完整、无失败页、无截断）。
    page_snapshot_ids：全部最终成功页的 snapshot 身份，用于核验"每个页都有
        已校验的 v4 证据且属于本单元"。
    row_verifier：(snapshot_id, row_index, standardized_fields, raw_fields) -> bool，
        核验该行的来源下标能重建传入的映射/标准值。
    """

    unit_id: str
    complete: bool
    page_snapshot_ids: tuple[str, ...] = ()
    row_verifier: Callable[[str, int, dict, dict], bool] | None = None
    detail: str | None = None
    extra: dict = field(default_factory=dict)
