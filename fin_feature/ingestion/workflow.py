"""Shared run workflow for crawl and incremental entry points."""
import re

from fin_feature.ingestion.capture import ActiveTransactionError, RunResult
from fin_feature.ingestion.evidence import MAPPING_VERSION
from fin_feature.ingestion.service import FinancialIngestionCoordinator, FinancialUnitSpec, CRAWLER_VERSION
from fin_feature.ingestion.stock_discovery import discover_stocks
from fin_feature.storage.hashing import canonical_json
from fin_feature.storage.repository import utc_now_iso


class IngestionWorkflow:
    def __init__(self, repo, store, financial_executor, settings, *, stock_executor=None, clock=utc_now_iso):
        self.repo, self.store = repo, store
        self.settings, self.stock_executor = settings, stock_executor
        self.clock = clock
        self.financial = FinancialIngestionCoordinator(
            repo, store, financial_executor, page_size=settings.page_size,
            recent_periods=settings.recent_periods, max_pages_per_unit=settings.max_pages, clock=clock,
        )

    def _specs(self, companies):
        codes = list(dict.fromkeys(companies))
        if any(not isinstance(code, str) or re.fullmatch(r"[0-9]{6}\.(SZ|SH)", code) is None for code in codes):
            raise ValueError("stock_list contains an invalid company identity")
        return [FinancialUnitSpec(code, report) for report in self.settings.report_names for code in codes]

    def run(self, mode):
        if self.repo.conn.in_transaction or self.repo.transaction_broken:
            raise ActiveTransactionError("ingestion cannot start with an active or broken transaction")
        if mode == "incremental":
            companies = [row[0] for row in self.repo.conn.execute("SELECT secucode FROM stock_list ORDER BY secucode")]
            return self.financial.run(self._specs(companies))
        if mode != "crawl" or self.stock_executor is None:
            raise ValueError("crawl requires a stock discovery executor")
        with self.repo.unit_of_work():
            run_id = self.repo.create_ingest_run(
                "eastmoney", request_scope=canonical_json({
                    "command": mode, "stock_count": self.settings.stock_count,
                    "report_names": self.settings.report_names, "recent_periods": self.settings.recent_periods,
                    "max_pages_per_unit": self.settings.max_pages, "max_body_bytes": self.settings.max_body_bytes, "mapping_version": MAPPING_VERSION,
                }), crawler_version=CRAWLER_VERSION, started_at=self.clock(),
            )
        try:
            stocks = discover_stocks(
                self.repo, self.store, self.stock_executor, run_id,
                stock_count=self.settings.stock_count, max_pages=self.settings.max_pages, clock=self.clock,
            )
        except Exception:
            if not self.repo.transaction_broken:
                try:
                    with self.repo.unit_of_work():
                        summary = self.repo.finalize_run(run_id, unit_kinds=("stock_list",))
                    return RunResult(units=(), **summary)
                except Exception:
                    pass
            return RunResult(run_id, "failed", 1, 1, 0, 0, 1)
        return self.financial.run(self._specs([row["secucode"] for row in stocks]), run_id=run_id)
