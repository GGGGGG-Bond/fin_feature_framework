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
    _check_units(conn, run_id, add)
    _check_attempts(conn, run_id, add)
    referenced = _check_snapshots(conn, raw_store, run_id, add)
    _check_orphan_files(raw_store, referenced, add)
    return report


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


def _check_units(conn, run_id, add) -> None:
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
            _check_unit_evidence(conn, row, add)


def _check_unit_evidence(conn, unit, add) -> None:
    """成功单元必须有可核验的分页证据；空成功也必须留有效空响应快照。"""
    attempts = conn.execute(
        "SELECT * FROM ingest_attempt WHERE unit_id = ?", (unit["unit_id"],)
    ).fetchall()
    if not attempts:
        add(
            "unit_success_without_evidence", SEVERITY_ERROR,
            "单元标为成功但没有任何 HTTP 尝试记录",
            run_id=unit["run_id"], unit_id=unit["unit_id"],
        )
        return
    valid_pages = 0
    for attempt in attempts:
        snapshot = conn.execute(
            "SELECT * FROM raw_snapshot WHERE attempt_id = ?", (attempt["attempt_id"],)
        ).fetchone()
        if snapshot is not None and snapshot["validation_status"] == "valid":
            valid_pages += 1
    if valid_pages == 0:
        add(
            "unit_success_without_evidence", SEVERITY_ERROR,
            "单元标为成功但没有已校验的 v4 页证据",
            run_id=unit["run_id"], unit_id=unit["unit_id"],
        )


def _check_attempts(conn, run_id, add) -> None:
    """逐条检查 HTTP 尝试；未结算的 attempt 在任何单元状态下都算未完成。"""
    where, params = _run_filter(run_id)
    rows = conn.execute(
        f"SELECT a.*, u.run_id AS u_run FROM ingest_attempt a "
        f"JOIN ingest_unit u ON u.unit_id = a.unit_id"
        + (f" WHERE u.run_id = ?" if run_id is not None else ""),
        params,
    ).fetchall()
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
            # 网络无响应：只有 attempt，允许没有 snapshot（第 3 类保留对象）
            continue
        snapshot = conn.execute(
            "SELECT snapshot_id FROM raw_snapshot WHERE attempt_id = ?", (row["attempt_id"],)
        ).fetchone()
        if snapshot is None:
            add(
                "attempt_without_snapshot", SEVERITY_ERROR,
                f"已结算 attempt（outcome={row['outcome']}）没有对应快照",
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


def _check_orphan_files(raw_store: RawStore, referenced: set[str], add) -> None:
    """有文件但无 snapshot 引用的孤儿与临时文件（不自动删除）。"""
    for rel_path in raw_store.iter_relative_files():
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
