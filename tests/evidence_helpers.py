"""2B-1 测试共用：真实临时文件证据环境。

契约要求（docs/stage-2/2b-test-matrix.md「交付证据与运行限制」）：
- fixture 必须以临时真实文件满足新边界，不能靠假路径或空查询条件跳过
- 篡改测试使用真实临时文件；事务与持久化测试重连
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from fin_feature.constants import REPORT_FINANCIAL_INDICATOR
from fin_feature.ingestion.evidence import (
    MAPPING_VERSION,
    UnitEvidence,
    compute_unit_key,
    normalize_request_scope,
)
from fin_feature.storage.raw_store import PublishContext, RawStore, compute_raw_hash
from fin_feature.storage.repository import Repository

SOURCE = "eastmoney"


@dataclass
class Captured:
    """一次响应体发布的完整引用。"""

    snapshot_id: str
    file_path: str
    raw_hash: str
    raw_bytes: int
    rows: list[dict]
    attempt_id: str
    page_number: int
    http_attempt: int
    body: bytes


@dataclass
class EvidenceEnv:
    """临时库 + 临时证据目录的采集证据环境。"""

    conn: sqlite3.Connection
    raw_root: str
    source_name: str = SOURCE
    rows_by_snapshot: dict[str, list[dict]] = field(default_factory=dict)
    run_id: str = ""
    repo: Repository = field(init=False)
    store: RawStore = field(init=False)

    def __post_init__(self) -> None:
        self.store = RawStore(self.raw_root)
        self.repo = Repository(self.conn, raw_store=self.store)

    # -- 批次与单元 ---------------------------------------------------
    def new_run(self, source_name: str | None = None) -> str:
        self.run_id = self.repo.create_ingest_run(source_name or self.source_name)
        return self.run_id

    def new_unit(
        self,
        *,
        secucode: str = "000001.SZ",
        report_name: str = REPORT_FINANCIAL_INDICATOR,
        unit_kind: str = "financial",
        run_id: str | None = None,
        request_scope: dict | None = None,
    ) -> dict:
        scope = request_scope or {
            "endpoint": "/api/data/v1/get",
            "report_name": report_name,
            "page_size": 50,
            "recent_periods": 4,
            "mapping_version": MAPPING_VERSION,
        }
        scope_json = normalize_request_scope(scope)
        if unit_kind == "stock_list":
            report_name = "stock_list"
            secucode = ""
            if request_scope is None:
                scope_json = normalize_request_scope({"endpoint": "/api/qt/clist/get", "stock_count": 1, "page_size": 100, "mapping_version": MAPPING_VERSION})
        unit_key = compute_unit_key(
            source_name=self.source_name,
            unit_kind=unit_kind,
            report_name=report_name,
            secucode=secucode,
            request_scope=scope_json,
        )
        unit_id = self.repo.create_ingest_unit(
            run_id=run_id or self.run_id,
            unit_key=unit_key,
            source_name=self.source_name,
            unit_kind=unit_kind,
            report_name=report_name,
            secucode=secucode,
            request_scope=scope_json,
        )
        return {
            "unit_id": unit_id,
            "unit_key": unit_key,
            "secucode": secucode,
            "report_name": report_name,
            "unit_kind": unit_kind,
            "request_scope": scope_json,
            "run_id": run_id or self.run_id,
            "source_name": self.source_name,
        }

    # -- HTTP 尝试与证据 ----------------------------------------------
    def capture(
        self,
        unit: dict,
        *,
        page: int = 1,
        http_attempt: int = 1,
        rows: list[dict] | None = None,
        body: bytes | None = None,
        http_status: int = 200,
        validation: str = "valid",
        fetched_at: str = "2026-10-16T08:00:00Z",
        outcome: str | None = "ok",
    ) -> Captured:
        """登记一次尝试、发布响应体证据并（可选）完成页校验。"""
        rows = rows if rows is not None else []
        if body is None:
            body = json.dumps(
                {"success": True, "result": {"pages": 1, "count": len(rows), "data": rows}},
                ensure_ascii=False,
            ).encode("utf-8")

        attempt_id = self.repo.register_attempt(
            unit["unit_id"], page, http_attempt, started_at=fetched_at
        )
        context = PublishContext(
            run_id=unit["run_id"],
            unit_key=unit["unit_key"],
            unit_id=unit["unit_id"],
            page_number=page,
            http_attempt=http_attempt,
            source_name=self.source_name,
            fetched_at=fetched_at,
            http_status=http_status,
            content_type="application/json",
        )
        stored = self.store.publish(context, body)

        secucode = None if unit["unit_kind"] == "stock_list" else unit["secucode"]
        snapshot_id = self.repo.save_raw_snapshot(
            unit["run_id"], self.source_name, unit["report_name"], secucode, page,
            "{}", stored.raw_hash, stored.file_path,
            len(rows) if validation == "valid" else None,
            attempt_id=attempt_id,
            http_status=http_status,
            content_type="application/json",
            raw_bytes=stored.raw_bytes,
            validation_status="pending",
            fetched_at=fetched_at,
        )
        if outcome is not None:
            self.repo.finish_attempt(attempt_id, outcome)
        if validation == "valid":
            self.repo.set_snapshot_validation(snapshot_id, "valid", len(rows))
        elif validation == "invalid":
            self.repo.set_snapshot_validation(snapshot_id, "invalid")

        self.rows_by_snapshot[snapshot_id] = rows
        return Captured(
            snapshot_id=snapshot_id, file_path=stored.file_path, raw_hash=stored.raw_hash,
            raw_bytes=stored.raw_bytes, rows=rows, attempt_id=attempt_id,
            page_number=page, http_attempt=http_attempt, body=body,
        )

    # -- 证据证明 -----------------------------------------------------
    def evidence(self, unit_id: str, captured: list[Captured], *,
                 complete: bool = True, verifier=None) -> UnitEvidence:
        """构造单元完整性证明；默认核验器按 row_index 比对原始行。"""
        if verifier is None:
            rows_by_snapshot = self.rows_by_snapshot

            def verifier(snapshot_id, row_index, standardized, raw_fields):  # noqa: ARG001
                rows = rows_by_snapshot.get(snapshot_id)
                if rows is None or row_index >= len(rows):
                    return False
                return raw_fields == rows[row_index]

        return UnitEvidence(
            unit_id=unit_id,
            complete=complete,
            page_snapshot_ids=tuple(c.snapshot_id for c in captured),
            row_verifier=verifier,
        )

    # -- 版本写入 -----------------------------------------------------
    def create_version(self, unit: dict, captured: Captured, *, row_index: int = 0,
                       standardized: dict | None = None, evidence_sufficient: int = 1,
                       evidence: UnitEvidence | None = None, **kwargs):
        """按契约写一条带完整证据的版本/观察。

        evidence 可显式覆盖默认证明（用于注入自定义 row_verifier 的用例）。
        """
        row = captured.rows[row_index] if row_index < len(captured.rows) else {}
        standardized = standardized if standardized is not None else {
            "total_operate_income": row.get("TOTAL_OPERATE_INCOME", 100)
        }
        unit_evidence = evidence if evidence is not None else self.evidence(
            unit["unit_id"], [captured]
        )
        return self.repo.create_statement_version(
            secucode=unit["secucode"],
            report_date=kwargs.pop("report_date", "2024-03-31"),
            table_type="financial_indicator",
            source_name=self.source_name,
            snapshot_id=captured.snapshot_id,
            run_id=unit["run_id"],
            standardized_fields=json.dumps(standardized, sort_keys=True),
            raw_fields=json.dumps(row, sort_keys=True),
            row_index=row_index,
            unit_evidence=unit_evidence,
            evidence_sufficient=evidence_sufficient,
            **kwargs,
        )


def make_env(conn: sqlite3.Connection, tmp_root, **kwargs) -> EvidenceEnv:
    """建立证据环境（临时库须已跑过 run_migrations）。"""
    import os

    root = os.path.join(str(tmp_root), "raw")
    return EvidenceEnv(conn=conn, raw_root=root, **kwargs)


def hash_of(body: bytes) -> str:
    return compute_raw_hash(body)
