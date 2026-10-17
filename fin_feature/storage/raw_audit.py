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

    # C3：收集每页信封（pages/count），核对声明范围、连续页号与合计行数
    envelopes: list[dict] = []
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
        # C3/D3/E2/E3：解析原页信封，按 unit_kind 分派规则。
        # D3：解析失败不能跳过——非 JSON/HTML/缺 result/非法类型都须报告诊断。
        # E2：财务与股票发现信封形状不同，须按 unit_kind 分派；财务合法空页 pages=0/1。
        # E3：success/data/pages/count 严格校验，bool 不当 int，多余页也报。
        envelope = _parse_page_envelope(raw_store, snapshot, unit["unit_kind"])
        if envelope["error"] is not None:
            add(
                "envelope_parse_failed", SEVERITY_ERROR,
                f"成功单元的最终页 {page_number} 信封解析失败：{envelope['error']}",
                run_id=unit["run_id"], unit_id=unit["unit_id"],
                snapshot_id=snapshot["snapshot_id"],
            )
        elif envelope["pages"] is not None:
            envelopes.append(envelope)
        # 实际行数取快照 record_count（各页本页行数之和应等于声明总记录数）
        if snapshot["record_count"] is not None:
            total_rows += int(snapshot["record_count"])

    # C3：财务单元核对声明页数与连续页号（股票发现 unit_kind=stock_list 不套用全分页规则）
    if unit["unit_kind"] == "financial" and envelopes:
        _check_page_completeness(conn, unit, envelopes, final_by_page, total_rows, add)


def _is_int_not_bool(value) -> bool:
    """E3c：bool 是 int 的子类，须排除——pages=True/count=True 不能当整数接受。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_page_envelope(raw_store: RawStore, snapshot, unit_kind: str = "financial") -> dict:
    """从原体响应解析分页信封，按 unit_kind 分派规则。

    D3：返回强类型结果，不用 None 表示"跳过"。
    - pages：声明的总页数（财务必需，缺失/非 int 则 error）
    - count：声明的总记录数（可选，缺失不影响页数完整性检查）
    - data_rows：本页 data 列表长度（E3b：用于核对实际行数）
    - error：解析失败原因（非 None 时该页须报告诊断，不能 clean）

    E2：财务信封为 {success, result:{pages,count,data}}；股票发现为 {rc, data:{total,diff}}。
    E2a：财务合法空页 pages=0 或 pages=1 且 data=[]。
    E3：success 必须严格 true；data 必须存在且为列表；pages/count 排除 bool。
    文件错误（缺文件/哈希不符）由文件诊断记录，不在此重复；JSON/结构错误须独立诊断。
    """
    import json
    result = {"pages": None, "count": None, "data_rows": None, "error": None}
    try:
        body = raw_store.verify({
            "file_path": snapshot["file_path"],
            "raw_hash": snapshot["raw_content_hash"],
            "raw_bytes": snapshot["raw_bytes"],
        }).body
        payload = json.loads(body)
    except (RawStoreVerifyError, RawStoreError):
        # 文件级错误由 _check_snapshots 报告，信封检查不重复
        result["error"] = "文件不可读（见文件诊断）"
        return result
    except (ValueError, json.JSONDecodeError) as exc:
        result["error"] = f"响应非合法 JSON（{exc}）"
        return result
    if not isinstance(payload, dict):
        result["error"] = f"响应顶层非对象：{type(payload).__name__}"
        return result

    if unit_kind == "stock_list":
        # E2b：股票发现信封为 {rc, data:{total,diff}}，不要求财务 result
        return _parse_stock_list_envelope(payload, result)

    # 财务信封：{success, result:{pages,count,data}}
    success = payload.get("success")
    if success is not True:
        result["error"] = f"业务失败：success={success!r}（应为 true）"
        return result
    inner = payload.get("result")
    if not isinstance(inner, dict):
        result["error"] = "响应缺少 result 对象"
        return result
    data = inner.get("data")
    if not isinstance(data, list):
        result["error"] = f"信封 result.data 缺失或非列表：{type(data).__name__}"
        return result
    result["data_rows"] = len(data)
    pages = inner.get("pages")
    # E2a：合法空页 pages=0 或 pages=1；E3c：排除 bool
    if not _is_int_not_bool(pages) or pages < 0:
        result["error"] = f"信封 pages 非非负整数：{pages!r}"
        return result
    result["pages"] = pages
    count = inner.get("count")
    if count is not None:
        if not _is_int_not_bool(count) or count < 0:
            result["error"] = f"信封 count 非非负整数：{count!r}"
            return result
        result["count"] = count
    return result


def _parse_stock_list_envelope(payload: dict, result: dict) -> dict:
    """E2b：股票发现信封 {rc, data:{total,diff}}，不套用财务全分页规则。"""
    rc = payload.get("rc")
    if rc is not None and rc != 0:
        result["error"] = f"股票发现业务失败：rc={rc!r}"
        return result
    data = payload.get("data")
    if not isinstance(data, dict):
        result["error"] = f"股票发现响应缺少 data 对象：{type(data).__name__}"
        return result
    diff = data.get("diff")
    if not isinstance(diff, list):
        result["error"] = f"股票发现 data.diff 缺失或非列表：{type(diff).__name__}"
        return result
    result["data_rows"] = len(diff)
    # 股票发现不分页，pages 设为 1 以跳过财务页数完整性检查
    result["pages"] = 1
    total = data.get("total")
    if total is not None:
        if not _is_int_not_bool(total) or total < 0:
            result["error"] = f"股票发现 data.total 非非负整数：{total!r}"
            return result
        result["count"] = total
    return result


def _check_page_completeness(conn, unit, envelopes, final_by_page, total_rows, add) -> None:
    """C3/E3：核对声明页数、连续页号与合计行数。缺页/多余页不得 clean。

    D3：count 可选——缺失时只跳过总行数一致性检查，不影响页数完整性。
    E3d：一旦某页出现有效 count，所有已出现 count 必须一致，且完整范围的实际总行数
    必须匹配该已知 count（不要求每页都重复提供）。
    E3e：页号集合必须等于声明范围，既查缺页也查多余页。
    """
    # 各页声明的总页数应一致
    declared_pages_set = {e["pages"] for e in envelopes}
    if len(declared_pages_set) != 1:
        add(
            "inconsistent_declared_pages", SEVERITY_ERROR,
            f"成功单元各页声明的总页数不一致：{sorted(declared_pages_set)}",
            run_id=unit["run_id"], unit_id=unit["unit_id"],
        )
        return
    declared_pages = declared_pages_set.pop()
    actual_pages = set(final_by_page.keys())

    # E2a：合法空页 pages=0——单页空响应，不要求连续页号
    if declared_pages == 0:
        if actual_pages != {1}:
            add(
                "missing_declared_page", SEVERITY_ERROR,
                f"成功单元声明 pages=0（合法空），但实际页号 {sorted(actual_pages)}（应为 [1]）",
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

    # E3d：count 可选——一旦某页出现有效 count，所有已出现 count 必须一致，
    # 且完整范围的实际总行数必须匹配该已知 count
    counts = [e["count"] for e in envelopes if e["count"] is not None]
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
