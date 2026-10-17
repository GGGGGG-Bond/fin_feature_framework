"""audit-raw：采集证据的只读对账。

契约依据：docs/stage-2/2b-contract.md 第 4 节、docs/stage-2/2b-schema.md。

只读承诺：本模块不写入数据库、不创建/删除/修改任何证据文件，
也不自动补抓、重算、改水位或把 running 判成功。它只报告：
- 引用文件的缺失 / 解压失败 / 大小或哈希不一致 / 越根路径
- 有文件但无 snapshot 引用的孤儿，以及未完成的临时文件
- legacy-unverified 引用（只能作审计，不能当 v4 已核验结论）
- 未完成的 run / unit / attempt
- 已成功单元却缺少可核验页证据
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from fin_feature.storage.raw_store import (
    RAW_FORMAT_HTTP_BODY,
    RawStore,
    RawStoreError,
    RawStoreVerifyError,
)
from fin_feature.storage.envelope import ParsedEnvelope, parse_envelope

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"

TERMINAL_UNIT_STATUSES = ("success", "success_empty", "failed")


@dataclass(frozen=True)
class AuditFinding:
    """一条对账发现。"""

    code: str
    severity: str
    detail: str
    run_id: str | None = None
    unit_id: str | None = None
    attempt_id: str | None = None
    snapshot_id: str | None = None
    file_path: str | None = None

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "detail": self.detail,
            "run_id": self.run_id,
            "unit_id": self.unit_id,
            "attempt_id": self.attempt_id,
            "snapshot_id": self.snapshot_id,
            "file_path": self.file_path,
        }


@dataclass
class AuditReport:
    """只读对账报告。"""

    findings: list[AuditFinding] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    scope: str = "all"

    @property
    def errors(self) -> list[AuditFinding]:
        return [f for f in self.findings if f.severity == SEVERITY_ERROR]

    @property
    def warnings(self) -> list[AuditFinding]:
        return [f for f in self.findings if f.severity == SEVERITY_WARNING]

    @property
    def clean(self) -> bool:
        """无 error 级发现即为干净（warning 只提示 legacy 引用等历史事实）。"""
        return not self.errors

    def exit_code(self) -> int:
        return 0 if self.clean else 1

    def as_dict(self) -> dict:
        return {
            "scope": self.scope,
            "clean": self.clean,
            "counts": dict(self.counts),
            "findings": [f.as_dict() for f in self.findings],
        }

    def format_text(self) -> str:
        lines = [f"audit-raw scope={self.scope} clean={self.clean}"]
        for code in sorted(self.counts):
            lines.append(f"  {code}: {self.counts[code]}")
        for finding in self.findings:
            refs = " ".join(
                f"{k}={v}" for k, v in (
                    ("run", finding.run_id), ("unit", finding.unit_id),
                    ("attempt", finding.attempt_id), ("snapshot", finding.snapshot_id),
                    ("file", finding.file_path),
                ) if v
            )
            lines.append(f"[{finding.severity}] {finding.code}: {finding.detail} {refs}".rstrip())
        return "\n".join(lines)


def audit_raw(
    conn: sqlite3.Connection,
    raw_store: RawStore,
    run_id: str | None = None,
) -> AuditReport:
    """对账采集证据（只读）。run_id 为 None 时检查全部批次。"""
    report = AuditReport(scope=run_id or "all")

    def add(code: str, severity: str, detail: str, **refs) -> None:
        report.findings.append(AuditFinding(code=code, severity=severity, detail=detail, **refs))
        report.counts[code] = report.counts.get(code, 0) + 1

    _check_runs(conn, run_id, add)
    _check_units(conn, raw_store, run_id, add)
    _check_attempts(conn, run_id, add)
    referenced = _check_snapshots(conn, raw_store, run_id, add)
    # R3：孤儿判断用全库引用集合（避免把其他批次的合法文件误报为孤儿），
    # 但文件扫描范围限定到所选 run 的目录前缀，不串入其他批次的问题。
    all_referenced = _all_referenced_files(conn)
    _check_orphan_files(raw_store, all_referenced, add, run_prefix=run_id)
    return report


def _all_referenced_files(conn: sqlite3.Connection) -> set[str]:
    """全库所有快照引用的相对路径集合（孤儿判断的基准）。"""
    rows = conn.execute("SELECT file_path FROM raw_snapshot").fetchall()
    return {r["file_path"] for r in rows}


def _run_filter(run_id: str | None, column: str = "run_id") -> tuple[str, list]:
    if run_id is None:
        return "", []
    return f" WHERE {column} = ?", [run_id]


def _check_runs(conn, run_id, add) -> None:
    where, params = _run_filter(run_id)
    rows = conn.execute(f"SELECT * FROM ingest_run{where}", params).fetchall()
    for row in rows:
        if row["status"] == "running":
            add(
                "unfinished_run", SEVERITY_ERROR,
                f"run 仍为 running（started_at={row['started_at']}），未写成终态",
                run_id=row["run_id"],
            )


def _check_units(conn, raw_store, run_id, add) -> None:
    where, params = _run_filter(run_id)
    rows = conn.execute(f"SELECT * FROM ingest_unit{where}", params).fetchall()
    for row in rows:
        if row["status"] not in TERMINAL_UNIT_STATUSES:
            add(
                "unfinished_unit", SEVERITY_ERROR,
                f"unit 未完成（status={row['status']}）",
                run_id=row["run_id"], unit_id=row["unit_id"],
            )
        if row["status"] in ("success", "success_empty"):
            _check_unit_evidence(conn, raw_store, row, add)


def _check_unit_evidence(conn, raw_store, unit, add) -> None:
    """成功单元必须有可核验的分页证据；空成功也必须留有效空响应快照。

    R8：不能只看"至少一页 valid"——成功单元的全部最终页都必须已核验。
    pending/invalid 的最终页、缺页或 attempt/snapshot 状态矛盾都要报告。
    历史失败重试页（非最终页）可保留，不要求其也 valid。

    C2：终态失败的响应不能凭 valid 标志冒充业务输入。最终尝试必须 outcome=ok
    且 HTTP 状态成功（2xx）；http_error 等终态失败即使文件有效、快照标 valid，
    对账也必须报告。

    C3：成功财务单元需依据已核验原页信封核对声明范围、连续页号、稳定 pages/count
    及合计行数；缺页不得 clean。股票发现的前 N 范围按其契约单独处理。
    """
    attempts = conn.execute(
        "SELECT * FROM ingest_attempt WHERE unit_id = ? ORDER BY page_number, http_attempt",
        (unit["unit_id"],),
    ).fetchall()
    if not attempts:
        add(
            "unit_success_without_evidence", SEVERITY_ERROR,
            "单元标为成功但没有任何 HTTP 尝试记录",
            run_id=unit["run_id"], unit_id=unit["unit_id"],
        )
        return

    # 每页取最终尝试（同页最大 http_attempt）；最终尝试才是单元结论
    final_by_page: dict[int, sqlite3.Row] = {}
    for attempt in attempts:
        page = attempt["page_number"]
        if page not in final_by_page or attempt["http_attempt"] > final_by_page[page]["http_attempt"]:
            final_by_page[page] = attempt

    # C3/F1-F3：收集每页信封，核对声明范围、连续页号与合计行数。
    # F3：实际行数取原体 len(data)，不取 metadata record_count——metadata 不是独立证据。
    envelopes: list[tuple[int, ParsedEnvelope]] = []
    total_rows = 0
    for page_number, attempt in sorted(final_by_page.items()):
        snapshot = conn.execute(
            "SELECT * FROM raw_snapshot WHERE attempt_id = ?", (attempt["attempt_id"],)
        ).fetchone()
        if snapshot is None:
            add(
                "unit_success_without_evidence", SEVERITY_ERROR,
                f"成功单元的最终页 {page_number} 没有快照证据",
                run_id=unit["run_id"], unit_id=unit["unit_id"],
                attempt_id=attempt["attempt_id"],
            )
            continue
        if snapshot["raw_format"] != "http-body-v1":
            add(
                "unit_success_without_evidence", SEVERITY_ERROR,
                f"成功单元的最终页 {page_number} 是 legacy-unverified，非 v4 已核验证据",
                run_id=unit["run_id"], unit_id=unit["unit_id"],
                snapshot_id=snapshot["snapshot_id"],
            )
            continue
        if snapshot["validation_status"] != "valid":
            add(
                "unit_success_without_evidence", SEVERITY_ERROR,
                f"成功单元的最终页 {page_number} 校验状态为 "
                f"{snapshot['validation_status']}（应为 valid）",
                run_id=unit["run_id"], unit_id=unit["unit_id"],
                snapshot_id=snapshot["snapshot_id"],
            )
            continue
        # C2：终态失败的响应不能凭 valid 标志冒充业务输入
        if attempt["outcome"] != "ok":
            add(
                "terminal_failure_as_evidence", SEVERITY_ERROR,
                f"成功单元的最终页 {page_number} 最终尝试 outcome={attempt['outcome']}，"
                f"非 ok：终态失败响应不能作为成功单元证据",
                run_id=unit["run_id"], unit_id=unit["unit_id"],
                attempt_id=attempt["attempt_id"], snapshot_id=snapshot["snapshot_id"],
            )
            continue
        http_status = snapshot["http_status"]
        if http_status is None or not (200 <= int(http_status) < 300):
            add(
                "terminal_failure_as_evidence", SEVERITY_ERROR,
                f"成功单元的最终页 {page_number} HTTP 状态 {http_status} 非成功（2xx），"
                f"不能作为成功单元证据",
                run_id=unit["run_id"], unit_id=unit["unit_id"],
                snapshot_id=snapshot["snapshot_id"],
            )
            continue
        # C3/D3/E2/E3/F1-F3：解析原页信封，按 unit_kind 分派规则（共享解析器）。
        # D3：解析失败不能跳过——非 JSON/HTML/缺 result/非法类型都须报告诊断。
        # E2：财务与股票发现信封形状不同，须按 unit_kind 分派；财务合法空页 pages=0/1。
        # E3：success/data/pages/count 严格校验，bool 不当 int，多余页也报。
        # F3：data_rows 取原体 len(data)，不取 metadata；非对象元素拒绝。
        try:
            body = raw_store.verify({
                "file_path": snapshot["file_path"],
                "raw_hash": snapshot["raw_content_hash"],
                "raw_bytes": snapshot["raw_bytes"],
            }).body
        except (RawStoreVerifyError, RawStoreError):
            add(
                "envelope_parse_failed", SEVERITY_ERROR,
                f"成功单元的最终页 {page_number} 信封解析失败：文件不可读（见文件诊断）",
                run_id=unit["run_id"], unit_id=unit["unit_id"],
                snapshot_id=snapshot["snapshot_id"],
            )
            continue
        envelope = parse_envelope(body, unit["unit_kind"])
        if envelope.error is not None:
            add(
                "envelope_parse_failed", SEVERITY_ERROR,
                f"成功单元的最终页 {page_number} 信封解析失败：{envelope.error}",
                run_id=unit["run_id"], unit_id=unit["unit_id"],
                snapshot_id=snapshot["snapshot_id"],
            )
            continue
        # F3：非对象元素拒绝——仅"最外层是 list"不满足契约
        if not envelope.all_rows_objects:
            add(
                "envelope_parse_failed", SEVERITY_ERROR,
                f"成功单元的最终页 {page_number} 原体 data/diff 含非对象元素",
                run_id=unit["run_id"], unit_id=unit["unit_id"],
                snapshot_id=snapshot["snapshot_id"],
            )
            continue
        # F3：原体行数与 metadata record_count 对照——两个相同错误值不能互相作证
        if (
            envelope.data_rows is not None
            and snapshot["record_count"] is not None
            and envelope.data_rows != int(snapshot["record_count"])
        ):
            add(
                "row_count_mismatch", SEVERITY_ERROR,
                f"成功单元的最终页 {page_number} 原体行数 {envelope.data_rows} "
                f"与快照 record_count {snapshot['record_count']} 不一致",
                run_id=unit["run_id"], unit_id=unit["unit_id"],
                snapshot_id=snapshot["snapshot_id"],
            )
            continue
        envelopes.append((page_number, envelope))
        # F3：实际行数取原体 len(data)，不取 metadata
        if envelope.data_rows is not None:
            total_rows += envelope.data_rows

    # C3/F1-F3：财务单元核对声明页数、连续页号、合法空与合计行数
    # （股票发现 unit_kind=stock_list 不套用财务全分页规则）
    if unit["unit_kind"] == "financial" and envelopes:
        _check_page_completeness(conn, unit, envelopes, final_by_page, total_rows, add)
    elif unit["unit_kind"] == "stock_list" and envelopes:
        _check_stock_completeness(unit, envelopes, final_by_page, total_rows, add)


def _check_stock_completeness(unit, envelopes, final_by_page, total_rows, add):
    import json
    from fin_feature.ingestion.stock_discovery import map_stock_row
    from fin_feature.ingestion.capture import SchemaFailure

    def problem(code, detail):
        add(code, SEVERITY_ERROR, detail, run_id=unit["run_id"], unit_id=unit["unit_id"])
    try:
        scope = json.loads(unit["request_scope"])
        requested = scope.get("stock_count")
        if isinstance(requested, bool) or not isinstance(requested, int) or requested < 1:
            raise ValueError("stock_count must be positive")
    except (ValueError, TypeError, AttributeError):
        problem("envelope_parse_failed", "stock discovery is missing a valid requested range")
        return
    numbers = sorted(final_by_page)
    if numbers != list(range(1, len(numbers) + 1)):
        problem("missing_declared_page", "stock discovery page numbers are not contiguous from one")
    totals = {envelope.count for _, envelope in envelopes}
    if len(totals) != 1:
        problem("inconsistent_declared_count", "stock discovery total changed between pages")
        return
    total = totals.pop()
    companies = set()
    try:
        for _, envelope in envelopes:
            for row in envelope.rows:
                companies.add(map_stock_row(row)["secucode"])
    except SchemaFailure:
        problem("envelope_parse_failed", "stock discovery includes an invalid company identity")
        return
    if not companies or total_rows > total:
        problem("row_count_mismatch", "successful stock discovery is empty or exceeds its declared total")
    if len(companies) < requested and total_rows < total:
        problem("missing_declared_page", "stock discovery neither satisfied N nor exhausted the declared source")
    if unit["source_rows"] != total_rows or unit["selected_rows"] != min(requested, len(companies)):
        problem("row_count_mismatch", "stock discovery unit counts disagree with the original pages")


def _check_page_completeness(conn, unit, envelopes, final_by_page, total_rows, add) -> None:
    """C3/E3/F1-F3：核对声明页数、连续页号、合法空与合计行数。缺页/多余页不得 clean。

    envelopes: list[(page_number, ParsedEnvelope)]——已通过共享解析器校验。
    D3：count 可选——缺失时只跳过总行数一致性检查，不影响页数完整性。
    E3d：一旦某页出现有效 count，所有已出现 count 必须一致，且完整范围的实际总行数
    必须匹配该已知 count（不要求每页都重复提供）。
    E3e：页号集合必须等于声明范围，既查缺页也查多余页。
    F2：合法空是组合条件——仅第一页、pages 为 0 或 1、data=[]、count 为 0 或缺失；
    pages=0 非空、多页首空、非首空一律诊断失败。
    """
    # 各页声明的总页数应一致
    declared_pages_set = {env.pages for _, env in envelopes}
    if len(declared_pages_set) != 1:
        add(
            "inconsistent_declared_pages", SEVERITY_ERROR,
            f"成功单元各页声明的总页数不一致：{sorted(declared_pages_set)}",
            run_id=unit["run_id"], unit_id=unit["unit_id"],
        )
        return
    declared_pages = declared_pages_set.pop()
    actual_pages = set(final_by_page.keys())

    # F2：合法空是组合条件——仅第一页、pages 为 0 或 1、data=[]、count 为 0 或缺失。
    # 解析器已计算 is_legal_empty；合法空只允许单页（页 1）。
    # pages=0 非空、pages=1 非空、多页首空、非首空一律诊断失败。
    is_legal_empty_unit = (
        declared_pages in (0, 1)
        and actual_pages == {1}
        and envelopes[0][1].is_legal_empty
    )
    if declared_pages == 0:
        # pages=0 必须是合法空（单页、data=[]、count 为 0 或缺失）
        if not is_legal_empty_unit:
            env = envelopes[0][1] if envelopes else None
            add(
                "envelope_parse_failed", SEVERITY_ERROR,
                f"成功单元声明 pages=0 但不满足合法空条件"
                f"（data_rows={env.data_rows if env else None}，"
                f"count={env.count if env else None}）："
                "pages=0 要求 data=[] 且 count 为 0 或缺失",
                run_id=unit["run_id"], unit_id=unit["unit_id"],
            )
        return

    # 页号集合必须等于声明范围 [1, declared_pages]，既查缺页也查多余页
    expected_pages = set(range(1, declared_pages + 1))
    missing = sorted(expected_pages - actual_pages)
    extra = sorted(actual_pages - expected_pages)
    if missing:
        add(
            "missing_declared_page", SEVERITY_ERROR,
            f"成功单元声明 {declared_pages} 页，但缺少页 {missing}",
            run_id=unit["run_id"], unit_id=unit["unit_id"],
        )
    if extra:
        add(
            "extra_page_outside_declared_range", SEVERITY_ERROR,
            f"成功单元声明 {declared_pages} 页，但有多余页 {extra}",
            run_id=unit["run_id"], unit_id=unit["unit_id"],
        )

    # F2：多页场景下任一页为空（data=[]）但声明 pages>1 → 不完整
    # （单页 pages=1 且 data=[] 已由 is_legal_empty_unit 处理）
    if declared_pages > 1:
        for page_number, env in envelopes:
            if env.data_rows == 0:
                add(
                    "envelope_parse_failed", SEVERITY_ERROR,
                    f"成功单元声明 {declared_pages} 页，但页 {page_number} data 为空："
                    "多页场景下空页不满足完整性",
                    run_id=unit["run_id"], unit_id=unit["unit_id"],
                )

    # E3d：count 可选——一旦某页出现有效 count，所有已出现 count 必须一致，
    # 且完整范围的实际总行数必须匹配该已知 count
    counts = [env.count for _, env in envelopes if env.count is not None]
    if counts:
        declared_count_set = set(counts)
        if len(declared_count_set) != 1:
            add(
                "inconsistent_declared_count", SEVERITY_ERROR,
                f"成功单元各页声明的总记录数不一致：{sorted(declared_count_set)}",
                run_id=unit["run_id"], unit_id=unit["unit_id"],
            )
            return
        declared_count = declared_count_set.pop()
        if declared_count != total_rows:
            add(
                "row_count_mismatch", SEVERITY_ERROR,
                f"成功单元声明总记录数 count={declared_count}，但实际行数 {total_rows}",
                run_id=unit["run_id"], unit_id=unit["unit_id"],
            )


def _check_attempts(conn, run_id, add) -> None:
    """逐条检查 HTTP 尝试；未结算的 attempt 在任何单元状态下都算未完成。

    C4：将"最终页是否成功完整"和"每次收到的响应是否留证据"分开检查。
    - transport_error（网络无响应）：允许没有 snapshot，不伪造证据。
    - http_error/business_error/decode_error/schema_error（收到失败响应）：
      即使是非最终的历史重试页，也必须保留原体快照；缺快照报证据缺口。
      契约允许历史失败重试页 invalid，不要求它 valid，但收到响应就必须留证据。
    - 最终尝试（同页最大 http_attempt）：任何已结算 outcome（除 transport_error）
      都必须有 snapshot。
    """
    where, params = _run_filter(run_id)
    rows = conn.execute(
        f"SELECT a.*, u.run_id AS u_run FROM ingest_attempt a "
        f"JOIN ingest_unit u ON u.unit_id = a.unit_id"
        + (f" WHERE u.run_id = ?" if run_id is not None else ""),
        params,
    ).fetchall()
    # 每页的最终尝试（同页最大 http_attempt）
    final_attempt_by_page: dict[tuple, sqlite3.Row] = {}
    for row in rows:
        key = (row["unit_id"], row["page_number"])
        if key not in final_attempt_by_page or \
                row["http_attempt"] > final_attempt_by_page[key]["http_attempt"]:
            final_attempt_by_page[key] = row

    # 收到失败响应但未留原体的终态（transport_error 除外——它本就无响应）
    RECEIVED_FAILURE_OUTCOMES = ("http_error", "business_error", "decode_error",
                                 "schema_error", "storage_error")

    for row in rows:
        if row["outcome"] == "running":
            add(
                "unfinished_attempt", SEVERITY_ERROR,
                f"attempt 未结算（page={row['page_number']}, "
                f"http_attempt={row['http_attempt']}）",
                run_id=row["u_run"], unit_id=row["unit_id"], attempt_id=row["attempt_id"],
            )
            continue
        if row["outcome"] == "transport_error":
            # 网络无响应：只有 attempt，允许没有 snapshot（不伪造证据）
            continue
        snapshot = conn.execute(
            "SELECT snapshot_id FROM raw_snapshot WHERE attempt_id = ?", (row["attempt_id"],)
        ).fetchone()
        if snapshot is None:
            key = (row["unit_id"], row["page_number"])
            is_final = final_attempt_by_page[key]["attempt_id"] == row["attempt_id"]
            if is_final:
                # 最终尝试缺快照：任何已结算 outcome（除 transport_error）都必须有证据
                add(
                    "attempt_without_snapshot", SEVERITY_ERROR,
                    f"最终 attempt（outcome={row['outcome']}）没有对应快照",
                    run_id=row["u_run"], unit_id=row["unit_id"], attempt_id=row["attempt_id"],
                )
            elif row["outcome"] in RECEIVED_FAILURE_OUTCOMES:
                # C4：历史失败重试页收到响应但未留原体——证据丢失，不得因后续成功消除
                add(
                    "failed_response_without_evidence", SEVERITY_ERROR,
                    f"历史失败 attempt（outcome={row['outcome']}）收到响应但未保留原体快照",
                    run_id=row["u_run"], unit_id=row["unit_id"], attempt_id=row["attempt_id"],
                )


def _check_snapshots(conn, raw_store: RawStore, run_id, add) -> set[str]:
    """校验引用文件与引用一致性，返回被引用的相对路径集合。"""
    where, params = _run_filter(run_id)
    snapshots = conn.execute(f"SELECT * FROM raw_snapshot{where}", params).fetchall()
    referenced: set[str] = set()

    for row in snapshots:
        referenced.add(row["file_path"])
        if row["raw_format"] == "legacy-unverified":
            add(
                "legacy_unverified", SEVERITY_WARNING,
                "旧快照只有 legacy-unverified 引用；不能作为 v4 已核验结论",
                run_id=row["run_id"], snapshot_id=row["snapshot_id"],
                file_path=row["file_path"],
            )
            continue

        unit_id = None
        if row["attempt_id"]:
            attempt = conn.execute(
                "SELECT * FROM ingest_attempt WHERE attempt_id = ?", (row["attempt_id"],)
            ).fetchone()
            if attempt is None:
                add(
                    "snapshot_attempt_missing", SEVERITY_ERROR,
                    f"快照引用的 attempt {row['attempt_id']} 不存在",
                    run_id=row["run_id"], snapshot_id=row["snapshot_id"],
                )
            else:
                unit_id = attempt["unit_id"]
                unit = conn.execute(
                    "SELECT * FROM ingest_unit WHERE unit_id = ?", (unit_id,)
                ).fetchone()
                if unit is None:
                    add(
                        "snapshot_unit_missing", SEVERITY_ERROR,
                        f"快照的 attempt 归属单元 {unit_id} 不存在",
                        run_id=row["run_id"], snapshot_id=row["snapshot_id"],
                    )
                elif (
                    unit["run_id"] != row["run_id"]
                    or unit["source_name"] != row["source_name"]
                    or unit["report_name"] != row["report_name"]
                    or unit["secucode"] != (row["secucode"] or "")
                ):
                    add(
                        "snapshot_unit_mismatch", SEVERITY_ERROR,
                        "快照与所属单元的 run/来源/报表/公司不一致",
                        run_id=row["run_id"], unit_id=unit_id,
                        snapshot_id=row["snapshot_id"],
                    )
        else:
            add(
                "http_body_without_attempt", SEVERITY_ERROR,
                "http-body-v1 快照没有 attempt_id（缺 HTTP 尝试身份）",
                run_id=row["run_id"], snapshot_id=row["snapshot_id"],
            )

        try:
            raw_store.verify({
                "file_path": row["file_path"],
                "raw_hash": row["raw_content_hash"],
                "raw_bytes": row["raw_bytes"],
            })
        except RawStoreVerifyError as exc:
            code = "missing_file" if "缺失" in str(exc) else "file_verification_failed"
            add(
                code, SEVERITY_ERROR, str(exc),
                run_id=row["run_id"], unit_id=unit_id,
                snapshot_id=row["snapshot_id"], file_path=row["file_path"],
            )
        except RawStoreError as exc:
            add(
                "evidence_path_invalid", SEVERITY_ERROR, str(exc),
                run_id=row["run_id"], snapshot_id=row["snapshot_id"],
                file_path=row["file_path"],
            )

    return referenced


def _check_orphan_files(raw_store: RawStore, referenced: set[str], add, *,
                        run_prefix: str | None = None) -> None:
    """有文件但无 snapshot 引用的孤儿与临时文件（不自动删除）。

    R3：孤儿判断用全库引用集合，避免把其他批次的合法文件误报为孤儿；
    文件扫描范围限定到所选 run 的目录前缀（run_id 是路径第二段），不串入其他批次。
    """
    for rel_path in raw_store.iter_relative_files():
        if run_prefix is not None:
            parts = rel_path.split("/")
            # 路径布局 source/{run_id}/...；run_id 不在第二段则不属于本范围
            if len(parts) < 2 or parts[1] != run_prefix:
                continue
        if RawStore.is_temp_path(rel_path):
            add("temp_file", SEVERITY_ERROR, "未完成的临时发布文件", file_path=rel_path)
        elif rel_path not in referenced:
            add("orphan_file", SEVERITY_ERROR, "证据文件没有对应的 snapshot 引用", file_path=rel_path)


def default_raw_root_for_db(db_path) -> Path:
    """默认证据根：已解析数据库文件所在目录/raw。"""
    return Path(db_path).expanduser().resolve().parent / "raw"


__all__ = [
    "AuditFinding",
    "AuditReport",
    "audit_raw",
    "default_raw_root_for_db",
    "RAW_FORMAT_HTTP_BODY",
]
