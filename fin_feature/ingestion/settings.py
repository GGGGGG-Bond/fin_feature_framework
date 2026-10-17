"""Validated CLI ingestion settings; validation has no write or network side effects."""
from dataclasses import dataclass
import math
import os
from pathlib import Path
from urllib.parse import urlsplit

from fin_feature.addons.incremental import validate_recent_periods
from fin_feature.ingestion.service import REPORT_TO_TABLE_TYPE
from fin_feature.storage.raw_store import resolve_raw_root, resolve_max_body_bytes
from fin_feature.ingestion.evidence import resolve_max_pages_per_unit


def _positive(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _url(value, name):
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an HTTP URL")
    p = urlsplit(value)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password or p.query or p.fragment:
        raise ValueError(f"{name} must be an HTTP endpoint without credentials or query parameters")
    try:
        p.port
    except ValueError as exc:
        raise ValueError(f"{name} has an invalid port") from exc
    return value


def _directory_target(path):
    p = path
    while not p.exists() and p != p.parent:
        p = p.parent
    if not p.is_dir() or not os.access(p, os.W_OK | os.X_OK):
        raise ValueError("configured output directory is not writable or has a file ancestor")


@dataclass(frozen=True)
class IngestionSettings:
    db_path: str
    raw_root: Path
    max_body_bytes: int
    stock_count: int
    page_size: int
    recent_periods: int | None
    max_pages: int
    max_retries: int
    timeout: int
    min_interval: float
    financial_url: str
    stock_url: str
    report_names: tuple[str, ...]
    user_agent: str
    referer: str


def validate_ingestion_settings(config):
    for name in ("database", "crawler", "crawl", "raw_store", "logging"):
        if not isinstance(config.get(name), dict):
            raise ValueError(f"{name} must be an object")
    crawler, crawl = config.crawler, config.crawl
    db_path = config.db_path
    if not isinstance(db_path, str) or not db_path.strip() or db_path == ":memory:":
        raise ValueError("database.path must name a persistent database file")
    db = Path(db_path).resolve()
    if db.exists() and (not db.is_file() or not os.access(db, os.W_OK)):
        raise ValueError("database.path is not a writable file")
    _directory_target(db.parent)
    root_value = config.raw_store.get("root")
    if root_value is not None and (not isinstance(root_value, str) or not root_value.strip()):
        raise ValueError("raw_store.root must be a non-empty path or null")
    root = resolve_raw_root(config, str(db)).resolve()
    if root == db or root in db.parents:
        raise ValueError("raw_store.root must not contain the database file")
    _directory_target(root)
    interval = crawler.get("min_interval", 1.5)
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not math.isfinite(interval) or interval < 0:
        raise ValueError("crawler.min_interval must be finite and non-negative")
    reports = crawl.get("report_names", tuple(REPORT_TO_TABLE_TYPE))
    if isinstance(reports, dict):
        expected = {v: k for k, v in REPORT_TO_TABLE_TYPE.items()}
        if any(expected.get(k) != v for k, v in reports.items()):
            raise ValueError("crawl.report_names contains an unsupported report mapping")
        reports = tuple(reports.values())
    if not isinstance(reports, (tuple, list)) or not reports or any(not isinstance(v, str) or v not in REPORT_TO_TABLE_TYPE for v in reports):
        raise ValueError("crawl.report_names must contain supported financial reports")
    level = config.logging.get("level", "INFO")
    if not isinstance(level, str) or level.upper() not in {"DEBUG", "INFO", "WARNING", "WARN", "ERROR", "CRITICAL"}:
        raise ValueError("logging.level is invalid")
    log_file = config.logging.get("file")
    if log_file is not None:
        if not isinstance(log_file, str) or not log_file.strip():
            raise ValueError("logging.file must be a path or null")
        log_path = Path(log_file).expanduser().resolve()
        if log_path.exists() and not log_path.is_file():
            raise ValueError("logging.file is not a file")
        _directory_target(log_path.parent)
    user_agent = crawler.get("user_agent", "Mozilla/5.0")
    referer = crawler.get("referer", "")
    if not isinstance(user_agent, str) or not user_agent or not isinstance(referer, str):
        raise ValueError("HTTP identification headers must be strings")
    return IngestionSettings(
        db_path=db_path, raw_root=root, max_body_bytes=resolve_max_body_bytes(config),
        stock_count=_positive(crawl.get("stock_count", 50), "crawl.stock_count"),
        page_size=_positive(crawler.get("page_size", 50), "crawler.page_size"),
        recent_periods=validate_recent_periods(crawl.get("recent_periods")),
        max_pages=resolve_max_pages_per_unit(config),
        max_retries=_positive(crawler.get("max_retries", 3), "crawler.max_retries"),
        timeout=_positive(crawler.get("timeout", 15), "crawler.timeout"),
        min_interval=interval,
        financial_url=_url(crawler.get("base_url"), "crawler.base_url"),
        stock_url=_url(crawler.get("stock_list_url"), "crawler.stock_list_url"),
        report_names=tuple(dict.fromkeys(reports)), user_agent=user_agent, referer=referer,
    )
