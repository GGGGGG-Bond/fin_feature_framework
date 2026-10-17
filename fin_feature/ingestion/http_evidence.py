"""2B-2：带 HTTP 尝试观察与原体证据的单页请求执行器。

契约依据：docs/stage-2/2b-contract.md 第 3 节。

顺序（不能被 raise_for_status/JSON 解析/success 校验提前打断）：
1. on_attempt_start：登记 attempt（unit/page/http_attempt、UTC 开始时间），短事务提交。
2. 发请求；收到响应后立刻读取 ``response.content`` 并发布为 gzip 证据，
   记录状态码、接收时间（UTC）与编码元信息；snapshot 以 pending 登记，短事务提交。
3. 页校验（HTTP 状态、JSON 解码、信封结构）完成后才把 attempt 结算为 ok；
   失败按 http_error/business_error/decode_error/schema_error/storage_error 结算。

网络/HTTP 失败按 max_retries（最大尝试总次数，含第一次）退避重试；
business/decode/schema/storage 错误不重试。失败不返回部分记录，抛带引用的
类型化异常；已发布的文件与审计行不回滚、不自动删除。
"""

from __future__ import annotations

import json
import sqlite3
import time

import requests

from fin_feature.crawler.rate_limiter import RateLimiter
from fin_feature.ingestion.capture import (
    BusinessFailure,
    CapturedPage,
    DecodeFailure,
    HttpStatusFailure,
    IngestionError,
    SchemaFailure,
    StorageFailure,
    TransportFailure,
)
from fin_feature.storage.envelope import parse_envelope
from fin_feature.storage.raw_store import PublishContext, RawStoreError
from fin_feature.storage.repository import utc_now_iso
from fin_feature.utils.logger import setup_logger

logger = setup_logger("fin_feature.ingestion.http_evidence")

# snapshot.request_params 只保存业务白名单，不保存 Cookie/Authorization/整段 body
_REQUEST_PARAM_WHITELIST = frozenset({
    "reportName", "columns", "pageSize", "pageNumber", "sortColumns", "sortTypes",
    "filter", "pn", "pz", "po", "np", "fltt", "invt", "fs", "fields",
})


def _snapshot_request_params(params: dict) -> str:
    safe = {k: str(v) for k, v in params.items() if k in _REQUEST_PARAM_WHITELIST}
    return json.dumps(safe, sort_keys=True, ensure_ascii=False)


def _content_type(response) -> str | None:
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    value = headers.get("Content-Type")
    return str(value) if value is not None else None


class EvidenceRequestExecutor:
    """执行单页 GET，并把每次真实尝试与响应体纳入审计与 RawStore。

    session、时钟、睡眠与限速器均可注入，测试不依赖真实网络与真实等待。
    """

    def __init__(
        self,
        *,
        repository,
        raw_store,
        session,
        url: str,
        timeout: int = 15,
        max_retries: int = 3,
        rate_limiter: RateLimiter | None = None,
        sleep=time.sleep,
        clock=utc_now_iso,
    ):
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 1:
            raise ValueError(f"max_retries 必须为 >=1 的整数（最大尝试总次数），当前 {max_retries!r}")
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
            raise ValueError(f"timeout 必须为正整数秒，当前 {timeout!r}")
        self.repository = repository
        self.raw_store = raw_store
        self.session = session
        self.url = url
        self.timeout = timeout
        self.max_retries = max_retries
        self.rate_limiter = rate_limiter or RateLimiter(0)
        self._sleep = sleep
        self._clock = clock

    # ------------------------------------------------------------------
    # 单页请求
    # ------------------------------------------------------------------
    def fetch_page(self, unit, *, page_number: int, params: dict) -> CapturedPage:
        """请求一页：逐次登记真实 attempt，成功返回已校验页与行下标。

        unit 需含 unit_id/unit_key/run_id/source_name/report_name/secucode/unit_kind。
        """
        if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1:
            raise ValueError(f"page_number 必须为 >=1 的整数，当前 {page_number!r}")
        unit_id = unit["unit_id"]
        last_failure: IngestionError | None = None

        for http_attempt in range(1, self.max_retries + 1):
            attempt_id = self._start_attempt(unit_id, page_number, http_attempt)
            self.rate_limiter.wait()
            try:
                response = self.session.get(self.url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                # 无响应：不留 body/快照，仅保留 transport_error attempt
                self._finish(attempt_id, None, "transport_error",
                             error_code=type(exc).__name__, detail=str(exc)[:300])
                last_failure = TransportFailure(
                    f"第 {page_number} 页第 {http_attempt} 次请求无响应：{type(exc).__name__}",
                    unit_id=unit_id, page_number=page_number, http_attempt=http_attempt,
                    attempt_id=attempt_id,
                )
                if http_attempt >= self.max_retries:
                    raise last_failure from exc
                self._backoff(http_attempt)
                continue

            received_at = self._clock()
            body = bytes(getattr(response, "content", b"") or b"")
            http_status = int(response.status_code)
            snapshot_id, _reused = self._capture_response(
                unit=unit, page_number=page_number, http_attempt=http_attempt,
                attempt_id=attempt_id, body=body, http_status=http_status,
                received_at=received_at, params=params, response=response,
            )

            # 1) HTTP 状态失败：body 已留存，标记 invalid，可按上限重试
            if not (200 <= http_status < 300):
                self._finish(attempt_id, snapshot_id, "http_error", valid=False,
                             error_code=f"HTTP {http_status}",
                             detail=f"HTTP 状态 {http_status}")
                last_failure = HttpStatusFailure(
                    f"第 {page_number} 页 HTTP 状态 {http_status}",
                    unit_id=unit_id, page_number=page_number, http_attempt=http_attempt,
                    attempt_id=attempt_id, snapshot_id=snapshot_id,
                    http_status=http_status,
                )
                if http_attempt >= self.max_retries:
                    raise last_failure
                self._backoff(http_attempt)
                continue

            # 2) 解码/业务/结构失败：先留原体再报错，不重试
            envelope = parse_envelope(body, unit["unit_kind"])
            if envelope.error is not None:
                raise self._envelope_failure(
                    unit=unit, page_number=page_number, http_attempt=http_attempt,
                    attempt_id=attempt_id, snapshot_id=snapshot_id,
                    error_code=envelope.error_code or "schema_error",
                    error=envelope.error,
                )
            if not envelope.all_rows_objects:
                raise self._envelope_failure(
                    unit=unit, page_number=page_number, http_attempt=http_attempt,
                    attempt_id=attempt_id, snapshot_id=snapshot_id,
                    error_code="schema_error",
                    error="原体 data/diff 含非对象元素",
                )
            if unit["unit_kind"] == "financial" and envelope.pages == 0 \
                    and not envelope.is_legal_empty:
                raise self._envelope_failure(
                    unit=unit, page_number=page_number, http_attempt=http_attempt,
                    attempt_id=attempt_id, snapshot_id=snapshot_id,
                    error_code="schema_error",
                    error="pages=0 但响应非空，不满足合法空条件",
                )

            # 3) 页校验通过后才结算 ok；valid 与 attempt 同事务提交
            try:
                self._finish(attempt_id, snapshot_id, "ok", valid=True,
                             record_count=envelope.data_rows)
            except (ValueError, sqlite3.Error) as exc:
                raise StorageFailure(
                    f"页校验状态写入失败：{type(exc).__name__}",
                    unit_id=unit_id, page_number=page_number,
                    http_attempt=http_attempt, attempt_id=attempt_id,
                    snapshot_id=snapshot_id,
                ) from exc

            return CapturedPage(
                page_number=page_number,
                attempt_id=attempt_id,
                snapshot_id=snapshot_id,
                declared_pages=envelope.pages if envelope.pages is not None else 1,
                source_rows=envelope.data_rows or 0,
                count=envelope.count,
                fetched_at=received_at,
                http_status=http_status,
                rows=envelope.rows,
                legal_empty=envelope.is_legal_empty,
                reused_snapshot=_reused,
            )

        # 循环必然在最后一次失败时 raise；此处只作防御
        assert last_failure is not None
        raise last_failure

    # ------------------------------------------------------------------
    # attempt 与证据
    # ------------------------------------------------------------------
    def _start_attempt(self, unit_id: str, page_number: int, http_attempt: int) -> str:
        started_at = self._clock()
        with self.repository.unit_of_work():
            return self.repository.register_attempt(
                unit_id, page_number, http_attempt, started_at=started_at
            )

    def _capture_response(
        self, *, unit, page_number: int, http_attempt: int, attempt_id: str,
        body: bytes, http_status: int, received_at: str, params: dict, response,
    ) -> tuple[str, bool]:
        """发布响应体并登记 pending snapshot；失败映射为 storage_error。"""
        context = PublishContext(
            run_id=unit["run_id"],
            unit_key=unit["unit_key"],
            unit_id=unit["unit_id"],
            page_number=page_number,
            http_attempt=http_attempt,
            source_name=unit["source_name"],
            fetched_at=received_at,
            http_status=http_status,
            content_type=_content_type(response),
            text_encoding=getattr(response, "encoding", None),
        )
        try:
            stored = self.raw_store.publish(context, body)
        except RawStoreError as exc:
            self._finish(attempt_id, None, "storage_error",
                         error_code=type(exc).__name__, detail=str(exc)[:300])
            raise StorageFailure(
                f"响应体证据发布失败：{type(exc).__name__}",
                unit_id=unit["unit_id"], page_number=page_number,
                http_attempt=http_attempt, attempt_id=attempt_id,
            ) from exc

        secucode = None if unit["unit_kind"] == "stock_list" else unit["secucode"]
        try:
            with self.repository.unit_of_work():
                snapshot_id = self.repository.save_raw_snapshot(
                    unit["run_id"], unit["source_name"], unit["report_name"],
                    secucode, page_number, _snapshot_request_params(params),
                    stored.raw_hash, stored.file_path, None,
                    attempt_id=attempt_id,
                    http_status=http_status,
                    content_type=_content_type(response),
                    text_encoding=getattr(response, "encoding", None),
                    raw_bytes=stored.raw_bytes,
                    validation_status="pending",
                    fetched_at=received_at,
                )
        except (ValueError, sqlite3.Error) as exc:
            self._finish(attempt_id, None, "storage_error",
                         error_code=type(exc).__name__, detail=str(exc)[:300])
            raise StorageFailure(
                f"响应体审计登记失败：{type(exc).__name__}",
                unit_id=unit["unit_id"], page_number=page_number,
                http_attempt=http_attempt, attempt_id=attempt_id,
            ) from exc
        return snapshot_id, stored.reused

    def _envelope_failure(
        self, *, unit, page_number, http_attempt, attempt_id, snapshot_id,
        error_code: str, error: str,
    ) -> IngestionError:
        """把信封解析/结构失败结算到 attempt，并返回对应类型化异常。"""
        self._finish(attempt_id, snapshot_id, error_code, valid=False,
                     error_code=error_code, detail=error[:300])
        failure_cls = {
            "business_error": BusinessFailure,
            "decode_error": DecodeFailure,
        }.get(error_code, SchemaFailure)
        return failure_cls(
            f"第 {page_number} 页响应不可作为业务输入：{error}",
            unit_id=unit["unit_id"], page_number=page_number,
            http_attempt=http_attempt, attempt_id=attempt_id,
            snapshot_id=snapshot_id,
        )

    def _finish(
        self, attempt_id: str, snapshot_id: str | None, outcome: str, *,
        valid: bool | None = None, record_count: int | None = None,
        error_code: str | None = None, detail: str | None = None,
    ) -> None:
        """在一个短事务内提交快照校验状态与 attempt 终态。"""
        with self.repository.unit_of_work():
            if snapshot_id is not None:
                if valid is True:
                    self.repository.set_snapshot_validation(
                        snapshot_id, "valid", record_count
                    )
                elif valid is False:
                    self.repository.set_snapshot_validation(snapshot_id, "invalid")
            self.repository.finish_attempt(
                attempt_id, outcome, error_code=error_code, detail=detail
            )

    def _backoff(self, attempt: int) -> None:
        wait_sec = 2 ** attempt
        logger.warning("请求失败（第 %d/%d 次），%d 秒后重试", attempt, self.max_retries, wait_sec)
        self._sleep(wait_sec)
