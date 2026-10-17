"""Evidence-backed stock discovery, using the financial HTTP request executor."""
import re
from urllib.parse import urlsplit

from fin_feature.crawler.stock_list import MARKET_FILTER, PUSH2_FIELDS
from fin_feature.ingestion.capture import IngestionError, IncompleteScope, SchemaFailure
from fin_feature.ingestion.evidence import MAPPING_VERSION, compute_unit_key
from fin_feature.storage.hashing import canonical_json
from fin_feature.storage.repository import utc_now_iso


class EmptyUniverse(IngestionError):
    code = "empty_universe"


def map_stock_row(row):
    code, market_code = row.get("f12"), row.get("f13")
    if not isinstance(code, str) or re.fullmatch(r"[0-9]{6}", code) is None:
        raise SchemaFailure("stock discovery returned an invalid security code")
    if isinstance(market_code, bool) or not isinstance(market_code, int) or market_code not in (0, 1):
        raise SchemaFailure("stock discovery returned an invalid market code")
    name = row.get("f14")
    if not isinstance(name, str):
        raise SchemaFailure("stock discovery returned an invalid security name")
    market = "SZ" if market_code == 0 else "SH"
    return {"secucode": f"{code}.{market}", "security_code": code,
            "security_name": name, "market": market}


def discovery_scope(executor, stock_count, page_size):
    return {"endpoint": urlsplit(executor.url).path, "report_name": "stock_list",
            "stock_count": stock_count, "page_size": page_size,
            "market_filter": MARKET_FILTER, "fields": PUSH2_FIELDS,
            "sort_types": "1", "mapping_version": MAPPING_VERSION}


def discover_stocks(repo, store, executor, run_id, *, stock_count, max_pages, clock=utc_now_iso):
    """Create one discovery unit; partial discovery never updates stock_list."""
    page_size = min(stock_count, 100)
    scope = canonical_json(discovery_scope(executor, stock_count, page_size))
    key = compute_unit_key(source_name="eastmoney", unit_kind="stock_list",
                           report_name="stock_list", secucode="", request_scope=scope)
    with repo.unit_of_work():
        unit_id = repo.create_ingest_unit(run_id, key, "eastmoney", "stock_list", "stock_list", "", scope)
        repo.mark_unit_running(unit_id, started_at=clock())
    unit = repo.get_unit(unit_id)
    try:
        stocks, pages = {}, []
        total = None
        source_rows = 0
        for page_number in range(1, max_pages + 1):
            params = {"pn": str(page_number), "pz": str(page_size), "po": "1", "np": "1",
                      "fltt": "2", "invt": "2", "fs": MARKET_FILTER, "fields": PUSH2_FIELDS}
            page = executor.fetch_page(unit, page_number=page_number, params=params)
            pages.append(page)
            if total is None:
                total = page.count
            if page.count != total:
                raise SchemaFailure("stock discovery total changed between pages", unit_id=unit_id)
            if total == 0:
                if page.rows:
                    raise SchemaFailure("stock discovery total=0 with nonempty rows", unit_id=unit_id)
                raise EmptyUniverse("stock discovery found zero companies", unit_id=unit_id)
            if not page.rows:
                raise SchemaFailure("stock discovery ended before the declared total", unit_id=unit_id)
            for raw in page.rows:
                stock = map_stock_row(raw)
                stocks.setdefault(stock["secucode"], stock)
            source_rows += len(page.rows)
            if source_rows > total:
                raise SchemaFailure("stock discovery rows exceed the declared total", unit_id=unit_id)
            if len(stocks) >= stock_count or source_rows == total:
                break
        else:
            raise IncompleteScope("stock discovery exceeded max_pages_per_unit", unit_id=unit_id)
        selected = list(stocks.values())[:stock_count]
        if not selected:
            raise EmptyUniverse("stock discovery found zero companies", unit_id=unit_id)
        with repo.unit_of_work():
            for page in pages:
                snapshot = repo.get_snapshot(page.snapshot_id)
                store.verify({"file_path": snapshot["file_path"],
                              "raw_hash": snapshot["raw_content_hash"], "raw_bytes": snapshot["raw_bytes"]})
            repo.upsert_stock_list(selected)
            repo.finish_unit(unit_id, "success", source_rows=source_rows, selected_rows=len(selected))
        return selected
    except Exception as exc:
        if not repo.transaction_broken:
            with repo.unit_of_work():
                row = repo.get_unit(unit_id)
                if row["status"] == "running":
                    repo.finish_unit(unit_id, "failed", failure_code=getattr(exc, "code", "internal_error"),
                                     detail=f"stock discovery failed: {type(exc).__name__}")
        raise
