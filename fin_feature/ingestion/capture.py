"""2B-2：带原始证据的采集结果类型与类型化失败。

契约依据：docs/stage-2/2b-contract.md 第 3、5 节，docs/stage-2/2b-test-matrix.md
B16-B24。

- CapturedPage：一页最终成功响应与它的 attempt/snapshot 身份、源端声明页数、
  原体行数与真实接收时间（UTC）。
- SourcedRecord：来源行的发生身份（snapshot_id + 0-based row_index），不从值相等
  或列表 index() 倒推页归属。
- FetchResult：仅全部请求范围完整时 complete=True；失败不返回可误当成功的部分
  records，而是抛带引用的类型化异常。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CapturedPage:
    """一页已校验响应：文件/审计已提交，可作为业务输入。"""

    page_number: int
    attempt_id: str
    snapshot_id: str
    declared_pages: int
    source_rows: int
    count: int | None
    fetched_at: str          # 响应真实接收时间（UTC ISO-8601，Z）
    http_status: int
    rows: tuple[dict, ...] = ()
    legal_empty: bool = False
    reused_snapshot: bool = False


@dataclass(frozen=True)
class SourcedRecord:
    """来源行及其页内 0-based 下标；该身份可回原页重建。"""

    value: dict
    snapshot_id: str
    row_index: int
    page_number: int
    attempt_id: str
    fetched_at: str


@dataclass(frozen=True)
class FetchResult:
    """分页完整结果。complete=False 不应作为成功返回（失败走类型化异常）。"""

    records: tuple[SourcedRecord, ...]
    pages: tuple[CapturedPage, ...]
    complete: bool
    source_row_count: int
    declared_pages: int


class IngestionError(Exception):
    """带单元/页/尝试/快照引用的类型化采集失败。

    code 与 ingest_attempt.outcome / ingest_unit.failure_code 对齐；
    message 只描述结构与状态，不包含响应体内容。
    """

    code = "ingestion_error"

    def __init__(
        self,
        message: str,
        *,
        unit_id: str | None = None,
        page_number: int | None = None,
        http_attempt: int | None = None,
        attempt_id: str | None = None,
        snapshot_id: str | None = None,
        http_status: int | None = None,
        references: tuple[str, ...] = (),
    ):
        super().__init__(message)
        self.unit_id = unit_id
        self.page_number = page_number
        self.http_attempt = http_attempt
        self.attempt_id = attempt_id
        self.snapshot_id = snapshot_id
        self.http_status = http_status
        self.references = references

    def as_detail(self) -> str:
        """去敏的失败摘要（不含响应体）。"""
        refs = []
        if self.unit_id:
            refs.append(f"unit={self.unit_id}")
        if self.page_number is not None:
            refs.append(f"page={self.page_number}")
        if self.http_attempt is not None:
            refs.append(f"attempt={self.http_attempt}")
        if self.snapshot_id:
            refs.append(f"snapshot={self.snapshot_id}")
        suffix = f"（{', '.join(refs)}）" if refs else ""
        return f"{self.code}: {self}{suffix}"


class ActiveTransactionError(IngestionError):
    """协调器在已有外层事务中被调用：请求前拒绝。"""

    code = "active_transaction"


class TransportFailure(IngestionError):
    """网络无响应（没有可留存的 body）。"""

    code = "transport_error"


class HttpStatusFailure(IngestionError):
    """HTTP 状态非 2xx；响应体已按原字节留存。"""

    code = "http_error"


class BusinessFailure(IngestionError):
    """success=false / rc!=0 等业务失败；不重试。"""

    code = "business_error"


class DecodeFailure(IngestionError):
    """响应体不是合法 JSON；原字节已留存。"""

    code = "decode_error"


class SchemaFailure(IngestionError):
    """结果结构错误或分页/行结构不完整。"""

    code = "schema_error"


class StorageFailure(IngestionError):
    """RawStore/审计写入失败；不重试，也不继续业务写入。"""

    code = "storage_error"


class IncompleteScope(IngestionError):
    """预知页数超过安全上限：保留首响应后失败，不返回截断成功。"""

    code = "incomplete_scope"


@dataclass
class UnitResult:
    """单个财务单元的结算结果（计数只在业务事务成功提交后填入）。"""

    unit_id: str
    secucode: str
    report_name: str
    status: str
    failure_code: str | None = None
    detail: str | None = None
    source_rows: int = 0
    selected_rows: int = 0
    new_versions: int = 0
    new_observations: int = 0
    compat_writes: int = 0
    reused: bool = False
    references: dict = field(default_factory=dict)


@dataclass
class RunResult:
    """一次采集运行的汇总；状态来自已持久化的单元状态，不用内存计数补造。"""

    run_id: str
    status: str
    failed_units: int
    total: int
    success: int
    failed: int
    incomplete: int
    units: tuple[UnitResult, ...] = ()

    @property
    def exit_code(self) -> int:
        """0 仅当全部计划单元成功（含合法空）；失败/部分失败为 1。"""
        return 0 if self.status == "success" else 1
