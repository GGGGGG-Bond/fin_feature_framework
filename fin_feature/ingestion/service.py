"""2B-2：统一财务采集单元协调器。

契约依据：docs/stage-2/2b-contract.md 第 2、4、6 节，验收矩阵 B19-B33。

职责：
- 拥有 run 生命周期：创建 run → 计划 unit（短事务）→ 逐单元采集 → 从已持久化
  单元状态汇总 run 终态（短事务）。
- 网络请求期间不持有 SQLite 写锁；文件/attempt/snapshot 证据各自短事务提交，
  业务 UOW 只提交兼容 raw、版本、观察、当前投影、水位与 unit 状态。
- 版本写入使用 ``defer_projection``，全部行写完后在同一业务事务内按阶段一
  ``dedup.sort_key`` 合并历史候选重建唯一当前投影；较旧公告后到不能降级覆盖。
- 任一步失败：业务 UOW 全回滚，随后另一短事务把 unit 标 failed；证据保留。

本模块只实现财务单元；股票发现单元的证据化接线在 2B-3。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from urllib.parse import urlsplit

from fin_feature.addons.incremental import validate_recent_periods
from fin_feature.cleaning.dedup import sort_key
from fin_feature.constants import (
    REPORT_BALANCE_SHEET,
    REPORT_FINANCIAL_INDICATOR,
    REPORT_INCOME_STATEMENT,
)
from fin_feature.crawler.eastmoney import build_data_params
from fin_feature.features.standardize import FIELD_UNITS, standardize_record
from fin_feature.ingestion.capture import (
    ActiveTransactionError,
    IngestionError,
    RunResult,
    SchemaFailure,
    SourcedRecord,
    UnitResult,
)
from fin_feature.ingestion.evidence import (
    MAPPING_VERSION,
    UNIT_KIND_FINANCIAL,
    UnitEvidence,
    compute_unit_key,
)
from fin_feature.ingestion.pagination import fetch_financial_pages
from fin_feature.storage.field_mapping import map_fields
from fin_feature.storage.hashing import canonical_json, normalize_numeric
from fin_feature.storage.repository import utc_now_iso
from fin_feature.utils.date_utils import normalize_date
from fin_feature.utils.logger import setup_logger

logger = setup_logger("fin_feature.ingestion.service")

REPORT_TO_TABLE_TYPE = {
    REPORT_FINANCIAL_INDICATOR: "financial_indicator",
    REPORT_INCOME_STATEMENT: "income_statement",
    REPORT_BALANCE_SHEET: "balance_sheet",
}

# 财务指标用 REPORTDATE，利润表/资产负债表用 REPORT_DATE（阶段一既有口径）
REPORT_DATE_FIELD = {
    REPORT_FINANCIAL_INDICATOR: "REPORTDATE",
    REPORT_INCOME_STATEMENT: "REPORT_DATE",
    REPORT_BALANCE_SHEET: "REPORT_DATE",
}

SOURCE_NAME = "eastmoney"
CRAWLER_VERSION = "eastmoney-stage2b-v1"


@dataclass(frozen=True)
class FinancialUnitSpec:
    """一个财务采集单元的计划输入。"""

    secucode: str
    report_name: str
    start_date: str | None = None
    end_date: str | None = None


@dataclass(frozen=True)
class _PreparedRow:
    """一条已映射/标准化、尚未持久化的来源行。"""

    record: SourcedRecord
    raw_fields: dict       # JSON 安全（非有限值转字符串），用于版本证据
    compat_fields: dict    # 原计量（含 inf/nan 原值），用于兼容 raw 表
    standardized: dict
    diagnostics_json: str
    report_date: str
    notice_date: str | None
    entry_time: str | None


def _json_safe_value(value):
    """非有限 float 转为可逆字符串标记，保证诊断/raw 可 JSON 序列化。"""
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
    return value


def _json_safe(mapping: dict) -> dict:
    return {k: _json_safe_value(v) for k, v in mapping.items()}


def _standardize_row(raw_record: dict) -> tuple[dict, list[dict]]:
    """固定 FIELD_UNITS 标准化（不消费运行时 fixture 覆盖）。

    返回（过滤 None 并统一数值表示的标准化字段，可序列化的有效诊断）。
    身份/日期等非数值字段不产生"非法数值"诊断噪声；未知单位与非法数值保留
    诊断，原始值同时保留在 raw_fields。
    """
    standardized, diagnostics = standardize_record(
        raw_record, list(raw_record.keys()), unit_map=FIELD_UNITS
    )
    meaningful = [
        diagnostic for diagnostic in diagnostics
        if diagnostic.get("field") in FIELD_UNITS
        or isinstance(diagnostic.get("original_value"), (int, float, bool))
    ]
    safe_diagnostics = [
        {k: _json_safe_value(v) for k, v in diagnostic.items()}
        for diagnostic in meaningful
    ]
    filtered = {k: v for k, v in standardized.items() if v is not None}
    return normalize_numeric(filtered), safe_diagnostics


def _selection_record(raw_fields: dict, notice_date, entry_time) -> dict:
    """构造阶段一选值口径的记录：先规范化报告期与公告日，再交给 sort_key。

    阶段一 deduplicate 在分组前把 report_date/notice_date 规范化为 YYYY-MM-DD；
    报告期参与内容摘要，若保留原始格式（如 ``2024/03/31`` 与 ``2024-03-31``），
    仅复用同一 sort_key 仍会选出不同记录（2B-2 复审 R3）。
    """
    record = dict(raw_fields)
    record["report_date"] = normalize_date(record.get("report_date")) or ""
    record["notice_date"] = normalize_date(notice_date) or ""
    record["entry_time"] = entry_time or ""
    return record


def _selection_key(row: _PreparedRow) -> tuple:
    """阶段一选值键：规范化报告期/公告日 → entry_time → 确定内容摘要。"""
    return sort_key(_selection_record(row.raw_fields, row.notice_date, row.entry_time))


class FinancialIngestionCoordinator:
    """财务采集协调器：分页证据、版本/观察、兼容 raw、水位与单元事务。"""

    def __init__(
        self,
        repository,
        raw_store,
        executor,
        *,
        source_name: str = SOURCE_NAME,
        page_size: int = 50,
        columns: str = "ALL",
        max_pages_per_unit: int = 1000,
        recent_periods: int | None = None,
        mapping_version: str = MAPPING_VERSION,
        crawler_version: str = CRAWLER_VERSION,
        clock=utc_now_iso,
    ):
        if isinstance(page_size, bool) or not isinstance(page_size, int) or page_size < 1:
            raise ValueError(f"page_size 必须为正整数，当前 {page_size!r}")
        if isinstance(max_pages_per_unit, bool) or not isinstance(max_pages_per_unit, int) \
                or max_pages_per_unit < 1:
            raise ValueError(
                f"max_pages_per_unit 必须为正整数，当前 {max_pages_per_unit!r}"
            )
        self.repository = repository
        self.raw_store = raw_store
        self.executor = executor
        self.source_name = source_name
        self.page_size = page_size
        self.columns = columns
        self.max_pages_per_unit = max_pages_per_unit
        self.recent_periods = validate_recent_periods(recent_periods)
        self.mapping_version = mapping_version
        self.crawler_version = crawler_version
        self._clock = clock

    # ------------------------------------------------------------------
    # 运行生命周期
    # ------------------------------------------------------------------
    def run(self, specs: list[FinancialUnitSpec]) -> RunResult:
        """执行一批财务单元；返回以已持久化状态为准的运行汇总。"""
        if self.repository.conn.in_transaction:
            raise ActiveTransactionError(
                "协调器不能在已有外层事务中调用：请在请求前提交或回滚外层事务，"
                "避免把网络与审计提交嵌入调用方事务"
            )
        validated = [self._validate_spec(spec) for spec in specs]

        run_started = self._clock()
        plan: dict[str, tuple[str, FinancialUnitSpec]] = {}
        with self.repository.unit_of_work():
            run_id = self.repository.create_ingest_run(
                self.source_name,
                request_scope=canonical_json({
                    "report_names": sorted({spec.report_name for spec in validated}),
                    "secucodes": sorted({spec.secucode for spec in validated}),
                    "recent_periods": self.recent_periods,
                    "mapping_version": self.mapping_version,
                }),
                crawler_version=self.crawler_version,
                started_at=run_started,
            )
            for spec in validated:
                scope = canonical_json(self._request_scope(spec))
                unit_key = compute_unit_key(
                    source_name=self.source_name,
                    unit_kind=UNIT_KIND_FINANCIAL,
                    report_name=spec.report_name,
                    secucode=spec.secucode,
                    request_scope=scope,
                )
                if unit_key in plan:
                    # 同 run 重复计划直接复用，不重复 GET / 不重复计数
                    continue
                unit_id = self.repository.create_ingest_unit(
                    run_id, unit_key, self.source_name, UNIT_KIND_FINANCIAL,
                    spec.report_name, spec.secucode, scope,
                )
                plan[unit_key] = (unit_id, spec)

        results: list[UnitResult] = []
        for unit_id, spec in plan.values():
            if self.repository.transaction_broken:
                break
            unit_row = self.repository.get_unit(unit_id)
            if unit_row["status"] in ("success", "success_empty"):
                results.append(UnitResult(
                    unit_id=unit_id, secucode=spec.secucode,
                    report_name=spec.report_name, status=unit_row["status"],
                    source_rows=unit_row["source_rows"],
                    selected_rows=unit_row["selected_rows"],
                    new_versions=unit_row["new_versions"],
                    new_observations=unit_row["new_observations"],
                    compat_writes=unit_row["compat_writes"],
                    reused=True,
                ))
                continue
            results.append(self._ingest_unit(unit_row, spec))

        if self.repository.transaction_broken:
            # 提交失败且回滚失败：连接停在不可恢复的事务中，不能继续读取
            # 未提交的 unit 状态宣称成功，也不能在该连接上再写 run 终态。
            logger.error(
                "运行 %s 的连接事务无法恢复，终止后续单元与 run 结算；"
                "run 保持 running 供对账识别", run_id,
            )
            return self._failed_run_result(run_id, plan, results)

        try:
            with self.repository.unit_of_work():
                summary = self.repository.finalize_run(run_id)
        except Exception as exc:  # noqa: BLE001 - 运行边界：不能据此宣称成功
            # 例如提交失败后外部读锁仍阻塞终态写入：返回 failed，run 保持
            # running 供对账，不能把未写入的终态报告为成功。
            logger.exception("运行 %s 终态写入失败：%s", run_id, exc)
            return self._failed_run_result(run_id, plan, results)

        return RunResult(
            run_id=run_id,
            status=summary["status"],
            failed_units=summary["failed_units"],
            total=summary["total"],
            success=summary["success"],
            failed=summary["failed"],
            incomplete=summary["incomplete"],
            units=tuple(results),
        )

    @staticmethod
    def _failed_run_result(
        run_id: str, plan: dict, results: list[UnitResult]
    ) -> RunResult:
        """无法写入 run 终态时的失败汇总：只采用已返回的单元结果。"""
        succeeded = sum(
            1 for result in results
            if result.status in ("success", "success_empty")
        )
        return RunResult(
            run_id=run_id,
            status="failed",
            failed_units=len(plan) - succeeded,
            total=len(plan),
            success=succeeded,
            failed=len(results) - succeeded,
            incomplete=len(plan) - len(results),
            units=tuple(results),
        )

    def _validate_spec(self, spec: FinancialUnitSpec) -> FinancialUnitSpec:
        if not isinstance(spec.secucode, str) or not spec.secucode:
            raise ValueError(f"财务单元必须给出非空 secucode：{spec.secucode!r}")
        if spec.report_name not in REPORT_TO_TABLE_TYPE:
            raise ValueError(
                f"未知报表 report_name={spec.report_name!r}（只支持三张财务报表）"
            )
        for label, value in (("start_date", spec.start_date), ("end_date", spec.end_date)):
            if value is not None and normalize_date(value) is None:
                raise ValueError(f"{label} 非法日期：{value!r}")
        return spec

    def _filters(self, spec: FinancialUnitSpec) -> dict:
        date_field = REPORT_DATE_FIELD[spec.report_name]
        filters = {"SECUCODE": spec.secucode}
        if spec.start_date:
            filters[f"{date_field}__gte"] = spec.start_date
        if spec.end_date:
            filters[f"{date_field}__lte"] = spec.end_date
        return filters

    def _sort_columns(self, report_name: str) -> str:
        return REPORT_DATE_FIELD[report_name]

    def _request_scope(self, spec: FinancialUnitSpec) -> dict:
        """逻辑请求范围：不含页号/HTTP 重试号；含固定映射版本。"""
        return {
            "endpoint": urlsplit(self.executor.url).path or self.executor.url,
            "report_name": spec.report_name,
            "columns": self.columns,
            "filters": self._filters(spec),
            "start_date": spec.start_date,
            "end_date": spec.end_date,
            "sort_columns": self._sort_columns(spec.report_name),
            "sort_types": "-1",
            "page_size": self.page_size,
            "recent_periods": self.recent_periods,
            "mapping_version": self.mapping_version,
        }

    def _params_builder(self, spec: FinancialUnitSpec):
        filters = self._filters(spec)
        sort_columns = self._sort_columns(spec.report_name)

        def build(page_number: int) -> dict:
            return build_data_params(
                report_name=spec.report_name,
                page_number=page_number,
                page_size=self.page_size,
                filters=filters,
                columns=self.columns,
                sort_columns=sort_columns,
                sort_types="-1",
            )

        return build

    # ------------------------------------------------------------------
    # 单元执行
    # ------------------------------------------------------------------
    def _ingest_unit(self, unit_row, spec: FinancialUnitSpec) -> UnitResult:
        unit_id = unit_row["unit_id"]
        base = {
            "unit_id": unit_id,
            "secucode": unit_row["secucode"],
            "report_name": unit_row["report_name"],
        }
        try:
            with self.repository.unit_of_work():
                self.repository.mark_unit_running(unit_id, started_at=self._clock())

            result = fetch_financial_pages(
                self.executor,
                unit=unit_row,
                params_builder=self._params_builder(spec),
                max_pages_per_unit=self.max_pages_per_unit,
            )

            if result.source_row_count == 0:
                # 合法空：保留有效空响应 snapshot，不写版本/兼容 raw，不动水位
                with self.repository.unit_of_work():
                    self.repository.finish_unit(unit_id, "success_empty")
                logger.info("单元 %s 合法空（success_empty）", unit_id)
                return UnitResult(status="success_empty", **base)

            prepared = self._prepare_rows(unit_row, result)
            evidence = self._build_evidence(unit_row, result)

            with self.repository.unit_of_work():
                counts = self._write_business(unit_row, prepared, evidence)
                self.repository.finish_unit(unit_id, "success", **counts)
            # 事务成功退出后才把计数用于返回；提交失败时不会走到这里
            logger.info("单元 %s 成功：%s", unit_id, counts)
            return UnitResult(status="success", **base, **counts)

        except IngestionError as exc:
            detail = exc.as_detail()
            self._mark_failed(unit_id, exc.code, detail)
            logger.error("单元 %s 失败（%s）：%s", unit_id, exc.code, exc)
            return UnitResult(status="failed", failure_code=exc.code,
                              detail=detail, **base)
        except Exception as exc:  # noqa: BLE001 - 单元边界：失败单元整体结算
            detail = f"internal_error: {type(exc).__name__}: {exc}"
            self._mark_failed(unit_id, "internal_error", detail)
            logger.exception("单元 %s 内部错误，已回滚业务写入", unit_id)
            return UnitResult(status="failed", failure_code="internal_error",
                              detail=detail, **base)

    def _mark_failed(self, unit_id: str, code: str, detail: str) -> None:
        """业务 UOW 已回滚后，用独立短事务标记失败；证据不回滚。"""
        if self.repository.transaction_broken:
            # 连接事务不可恢复：此时读到的状态可能尚未提交，不做结算写入
            logger.error("单元 %s 无法标记失败：连接事务已不可恢复", unit_id)
            return
        try:
            row = self.repository.get_unit(unit_id)
            if row is None:
                return
            if row["status"] != "running":
                # planned（尚未开始）或已完成：不补造运行中失败；由 run 汇总识别未完成
                logger.error(
                    "单元 %s 当前 status=%s，未写入 failed 结算", unit_id, row["status"]
                )
                return
            with self.repository.unit_of_work():
                self.repository.finish_unit(
                    unit_id, "failed", failure_code=code, detail=detail[:1000]
                )
        except Exception as exc:  # noqa: BLE001
            # 状态写入失败时保留未完成状态，供 audit-raw 识别
            logger.error("标记单元 %s 失败时写入失败：%s", unit_id, exc)

    # ------------------------------------------------------------------
    # 纯内存准备：映射、公司/报告期校验、标准化、诊断
    # ------------------------------------------------------------------
    def _prepare_rows(self, unit_row, result) -> list[_PreparedRow]:
        report_name = unit_row["report_name"]
        expected_secucode = unit_row["secucode"]
        prepared: list[_PreparedRow] = []

        for sourced in result.records:
            try:
                mapped = map_fields(sourced.value, report_name)
            except (ValueError, TypeError) as exc:
                raise SchemaFailure(
                    f"字段映射失败：{exc}", unit_id=unit_row["unit_id"],
                    page_number=sourced.page_number, attempt_id=sourced.attempt_id,
                    snapshot_id=sourced.snapshot_id,
                ) from exc

            # 新财务行必须与请求公司匹配：不符即使一行也整单元失败，不静默跳过
            if mapped.get("secucode") != expected_secucode:
                raise SchemaFailure(
                    f"来源行公司 {mapped.get('secucode')!r} 与请求公司 "
                    f"{expected_secucode} 不一致",
                    unit_id=unit_row["unit_id"], page_number=sourced.page_number,
                    attempt_id=sourced.attempt_id, snapshot_id=sourced.snapshot_id,
                )

            report_date = normalize_date(mapped.get("report_date"))
            if not report_date:
                raise SchemaFailure(
                    "来源行缺少可规范化的报告期",
                    unit_id=unit_row["unit_id"], page_number=sourced.page_number,
                    attempt_id=sourced.attempt_id, snapshot_id=sourced.snapshot_id,
                )

            raw_notice = mapped.get("notice_date")
            notice_date = normalize_date(raw_notice)
            diagnostics: list[dict] = []
            if raw_notice not in (None, "") and notice_date is None:
                # 非法非空公告日：置未知并记录诊断，不借 fetched_at 补公告日
                diagnostics.append({
                    "field": "notice_date",
                    "reason_code": "invalid_value",
                    "original_value": str(raw_notice),
                })

            raw_record = {k: v for k, v in mapped.items() if v is not None}
            raw_safe = _json_safe(raw_record)
            # 兼容 raw 保留原计量（SQLite 可存 inf；版本证据需 JSON 安全）
            compat_fields = raw_record
            standardized, std_diagnostics = _standardize_row(raw_record)
            diagnostics.extend(std_diagnostics)

            entry_time = mapped.get("entry_time")
            prepared.append(_PreparedRow(
                record=sourced,
                raw_fields=raw_safe,
                compat_fields=compat_fields,
                standardized=standardized,
                diagnostics_json=json.dumps(
                    diagnostics, sort_keys=True, ensure_ascii=False, allow_nan=False
                ),
                report_date=report_date,
                notice_date=notice_date,
                entry_time=entry_time if isinstance(entry_time, str) and entry_time else None,
            ))
        return prepared

    def _select_recent_periods(self, rows: list[_PreparedRow]) -> list[_PreparedRow]:
        """完整分页后按规范化报告期选最近 N 个不同期的全部修订。"""
        n = self.recent_periods
        if n is None:
            return rows
        periods = sorted({row.report_date for row in rows}, reverse=True)
        target = set(periods[:n])
        return [row for row in rows if row.report_date in target]

    def _build_evidence(self, unit_row, result) -> UnitEvidence:
        """单元完整性证明：页集合 + 行下标可重建映射/标准值。"""
        report_name = unit_row["report_name"]
        rows_by_snapshot = {page.snapshot_id: page.rows for page in result.pages}

        def verifier(snapshot_id, row_index, standardized, raw_fields) -> bool:
            rows = rows_by_snapshot.get(snapshot_id)
            if rows is None or row_index < 0 or row_index >= len(rows):
                return False
            mapped = map_fields(rows[row_index], report_name)
            raw_record = {k: v for k, v in mapped.items() if v is not None}
            if _json_safe(raw_record) != raw_fields:
                return False
            rebuilt_standardized, _ = _standardize_row(raw_record)
            return rebuilt_standardized == standardized

        return UnitEvidence(
            unit_id=unit_row["unit_id"],
            complete=True,
            page_snapshot_ids=tuple(page.snapshot_id for page in result.pages),
            row_verifier=verifier,
        )

    # ------------------------------------------------------------------
    # 业务事务：兼容 raw + 版本 + 观察 + 当前投影 + 水位 + unit 状态
    # ------------------------------------------------------------------
    def _write_business(self, unit_row, prepared: list[_PreparedRow], evidence) -> dict:
        repo = self.repository
        run_id = unit_row["run_id"]
        report_name = unit_row["report_name"]
        table_type = REPORT_TO_TABLE_TYPE[report_name]
        secucode = unit_row["secucode"]
        source_name = unit_row["source_name"]

        selected = self._select_recent_periods(prepared)
        # 与阶段一相同：按选值键升序写入，最终当前行即公告最新
        ordered = sorted(selected, key=_selection_key)

        before_versions = repo.count_versions_for_run(run_id)
        before_observations = repo.count_observations_for_run(run_id)

        # 兼容 raw：保留原计量；空公告按内容幂等，不无限新增
        compat_writes = repo.upsert_compat_raw(
            table_type, [dict(row.compat_fields) for row in ordered]
        )

        for row in ordered:
            repo.create_statement_version(
                secucode=secucode,
                report_date=row.report_date,
                table_type=table_type,
                source_name=source_name,
                snapshot_id=row.record.snapshot_id,
                run_id=run_id,
                notice_date=row.notice_date,
                revision_notice_date=None,
                revision_type="unknown",
                standardized_fields=canonical_json(row.standardized),
                raw_fields=canonical_json(row.raw_fields),
                row_index=row.record.row_index,
                entry_time=row.entry_time,
                normalization_diagnostics=row.diagnostics_json,
                # H1：observed_at = 该响应真实接收时间（UTC），不是提交时间
                observed_at=row.record.fetched_at,
                unit_evidence=evidence,
                defer_projection=True,
            )

        # 同一业务事务内合并历史候选，重建每组唯一当前投影
        for report_date in sorted({row.report_date for row in selected}):
            self._rebuild_projection(
                secucode, report_date, table_type, source_name
            )

        # 水位：仅完整成功单元在同一业务事务内单调推进；无有效公告则不变
        notice_dates = [row.notice_date for row in selected if row.notice_date]
        if notice_dates:
            latest = max(notice_dates)
            current = repo.get_watermark(report_name, secucode)
            current_normalized = normalize_date(current) if current else None
            if current_normalized is None or latest > current_normalized:
                repo.update_watermark(report_name, secucode, latest)

        return {
            "source_rows": len(prepared),
            "selected_rows": len(selected),
            "new_versions": repo.count_versions_for_run(run_id) - before_versions,
            "new_observations": (
                repo.count_observations_for_run(run_id) - before_observations
            ),
            "compat_writes": compat_writes,
        }

    def _rebuild_projection(
        self, secucode: str, report_date: str, table_type: str, source_name: str
    ) -> str | None:
        """按阶段一选值规则从全部已持久化候选选唯一当前版本。

        候选键 = 版本原始映射字段（排除 crawled_at）+ 该版本各次观察的
        (notice_date, entry_time)；取最大。这样：
        - 本次只返回较旧公告时，已有新版不会降级；
        - A→B→A 的恢复由优先的 A 观察支持；
        - 并列时按内容摘要、再按 version_id 确定性选取并记录歧义。
        """
        repo = self.repository
        versions = repo.get_statement_versions(
            secucode, report_date, table_type, source_name
        )
        if not versions:
            return None

        candidates: list[tuple[tuple, str]] = []
        for version in versions:
            try:
                raw_fields = json.loads(version["raw_fields"] or "{}")
            except (json.JSONDecodeError, TypeError):
                raw_fields = {}
            observations = repo.get_observations_for_version(version["version_id"])
            keys = []
            for observation in observations:
                keys.append(sort_key(_selection_record(
                    raw_fields, observation["notice_date"], observation["entry_time"]
                )))
            if not keys:
                keys.append(sort_key(_selection_record(
                    raw_fields, version["notice_date"], raw_fields.get("entry_time")
                )))
            candidates.append((max(keys), version["version_id"]))

        ordered = sorted(candidates, key=lambda item: (item[0], item[1]), reverse=True)
        if len(ordered) >= 2:
            first_key, second_key = ordered[0][0], ordered[1][0]
            if first_key[0] == second_key[0] and first_key[1] == second_key[1]:
                repo.log_clean_action(
                    secucode, report_date, table_type, "dedup_ambiguous",
                    f"公告/entry 并列（notice_date={first_key[0] or 'NULL'}, "
                    f"entry_time={first_key[1] or 'NULL'}），按确定内容摘要选取，"
                    "不声称恢复真实修订先后",
                )

        chosen = ordered[0][1]
        repo.apply_current_version(
            secucode, report_date, table_type, source_name, chosen
        )
        return chosen
