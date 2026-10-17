"""B34-B46: real CLI commands, temporary databases/evidence and scripted HTTP."""
import json
import logging
import sqlite3
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest
import requests
import yaml

import fin_feature.cli as cli
from fin_feature.config import load_config
from fin_feature.constants import REPORT_FINANCIAL_INDICATOR as FIN
from fin_feature.ingestion.service import FinancialIngestionCoordinator
from fin_feature.storage.database import Database, get_readonly_connection
from fin_feature.storage.raw_audit import audit_raw
from fin_feature.storage.raw_store import RawStore
from fin_feature.storage.repository import Repository
from ingestion_helpers import FakeResponse, body_for, fi_row


def stock(code="000001", market=0, name="Example"):
    return {"f12": code, "f13": market, "f14": name}


def stock_body(rows, total, **extra):
    return json.dumps({"rc": 0, "data": {"diff": rows, "total": total}, **extra}).encode()


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def query(path, sql, params=()):
    conn = connect(path)
    try:
        return [dict(row) for row in conn.execute(sql, params)]
    finally:
        conn.close()


def count(path, table):
    return query(path, f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]


def configure(tmp_path, **updates):
    data = {
        "database": {"path": str(tmp_path / "finance.db")},
        "raw_store": {"root": "evidence"},
        "crawler": {"base_url": "https://example.test/financial", "stock_list_url": "https://example.test/stocks",
                    "min_interval": 0, "max_retries": 1, "page_size": 2},
        "crawl": {"stock_count": 2, "recent_periods": 2, "max_pages_per_unit": 10,
                  "report_names": [FIN]},
        "logging": {"level": "INFO", "file": None},
    }
    for dotted, value in updates.items():
        section, key = dotted.split(".")
        data[section][key] = value
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    return path, tmp_path / "finance.db"


def seed(path, codes=("000001.SZ", "000002.SZ"), watermarks=False):
    db = Database(str(path));db.init();repo = Repository(db.conn)
    with repo.unit_of_work():
        repo.upsert_stock_list([{"secucode": code, "security_code": code[:6], "security_name": "Old", "market": code[-2:]} for code in codes])
        if watermarks:
            for code in codes:
                repo.update_watermark(FIN, code, "2025-01-01")
    db.close()


class Session:
    def __init__(self, path, *, financial=None, stocks=None):
        self.path, self.financial, self.stocks = path, financial, stocks
        self.calls = []
        self.closed = False

    def get(self, url, params=None, timeout=None):
        params = dict(params or {})
        # The actual request boundary must not hold SQLite's writer lock.
        probe = sqlite3.connect(self.path, timeout=0)
        try:
            probe.execute("BEGIN IMMEDIATE");probe.rollback()
        finally:
            probe.close()
        kind = "stock" if url.endswith("/stocks") else "financial"
        page = int(params.get("pn", params.get("pageNumber", 1)))
        self.calls.append({"kind": kind, "page": page, "params": params, "url": url})
        callback = self.stocks if kind == "stock" else self.financial
        if callback is not None:
            result = callback(page, params)
            if isinstance(result, BaseException):
                raise result
            return result
        if kind == "stock":
            return FakeResponse(body=stock_body([stock(), stock("000002")], 2))
        code = params["filter"].split('"')[1]
        row = fi_row(secucode=code, PARENT_NETPROFIT=20.0)
        if params["reportName"] != FIN:
            row["REPORT_DATE"] = row.pop("REPORTDATE")
        return FakeResponse(body=body_for([row], count=1))

    def close(self):
        self.closed = True


def invoke(monkeypatch, config_path, session, mode):
    monkeypatch.setattr(cli, "_new_ingestion_session", lambda settings: session)
    monkeypatch.setattr("fin_feature.ingestion.http_evidence.EvidenceRequestExecutor._backoff", lambda self, attempt: None)
    result = {"crawl": cli.cmd_crawl, "incremental": cli.cmd_incremental}[mode](Namespace(config=str(config_path)))
    return result


def run_row(path):
    rows = query(path, "SELECT * FROM ingest_run")
    assert len(rows) == 1
    return rows[0]


def test_b34_both_commands_use_the_same_financial_service(tmp_path, monkeypatch):
    plans = []
    original = FinancialIngestionCoordinator.run
    def spy(self, specs, **kwargs):
        plans.append([(row.secucode, row.report_name) for row in specs])
        return original(self, specs, **kwargs)
    monkeypatch.setattr(FinancialIngestionCoordinator, "run", spy)
    results = []
    for mode in ("crawl", "incremental"):
        d = tmp_path / mode;d.mkdir();cfg, db = configure(d);seed(db)
        session = Session(db)
        assert invoke(monkeypatch, cfg, session, mode) == 0 and session.closed
        assert run_row(db)["status"] == "success"
        results.append((query(db, "SELECT secucode,report_date,standardized_fields FROM statement_version ORDER BY secucode"),
                        query(db, "SELECT * FROM crawl_watermark ORDER BY secucode"),
                        query(db, "SELECT unit_key,status FROM ingest_unit WHERE unit_kind='financial' ORDER BY secucode")))
        assert count(db, "ingest_run") == 1
        assert count(db, "ingest_unit") == (3 if mode == "crawl" else 2)
    assert plans[0] == plans[1]
    # Watermark observed timestamps are not business identity.
    assert results[0][0] == results[1][0] and results[0][2] == results[1][2]
    assert [(r["secucode"],r["last_notice_date"]) for r in results[0][1]] == [(r["secucode"],r["last_notice_date"]) for r in results[1][1]]


@pytest.mark.parametrize("mode", ["crawl", "incremental"])
@pytest.mark.parametrize("empty_codes", [(), ("000001.SZ",), ("000001.SZ", "000002.SZ")])
def test_b35_b43_empty_and_nonempty_success_preserve_watermarks(tmp_path, monkeypatch, mode, empty_codes):
    cfg, db = configure(tmp_path);seed(db, watermarks=True)
    def financial(page, params):
        code=params["filter"].split('"')[1]
        rows=[] if code in empty_codes else [fi_row(secucode=code)]
        return FakeResponse(body=body_for(rows,pages=0 if not rows else 1,count=len(rows)))
    session=Session(db,financial=financial)
    assert invoke(monkeypatch,cfg,session,mode)==0
    result=run_row(db);assert result["status"]=="success" and result["failed_units"]==0
    assert count(db,"statement_version")==2-len(empty_codes)
    units=query(db,"SELECT * FROM ingest_unit WHERE unit_kind='financial'")
    assert sum(u["status"]=="success_empty" for u in units)==len(empty_codes)
    assert all(r["last_notice_date"]=="2025-01-01" for r in query(db,"SELECT * FROM crawl_watermark"))
    for call in session.calls:
        if call["kind"]=="financial":
            assert call["params"]["filter"].count("SECUCODE")==1
            assert "REPORTDATE>=" not in call["params"]["filter"]
    assert count(db,"raw_snapshot")==2+(mode=="crawl")


@pytest.mark.parametrize("mode", ["crawl", "incremental"])
@pytest.mark.parametrize("all_failed", [False,True])
def test_b36_failure_counts_financial_units_not_retries(tmp_path,monkeypatch,mode,all_failed):
    cfg,db=configure(tmp_path,**{"crawler.max_retries":2});seed(db)
    def financial(page,params):
        code=params["filter"].split('"')[1]
        if all_failed or code=="000002.SZ":return FakeResponse(status_code=500,body=b"failure evidence")
        return FakeResponse(body=body_for([fi_row(secucode=code)],count=1))
    session=Session(db,financial=financial)
    assert invoke(monkeypatch,cfg,session,mode)==1
    result=run_row(db)
    assert result["status"]==("failed" if all_failed else "partial_failure")
    assert result["failed_units"]==(2 if all_failed else 1)
    assert count(db,"statement_version")==int(not all_failed)
    assert count(db,"crawl_watermark")==int(not all_failed)
    assert count(db,"raw_snapshot")==((4 if all_failed else 3)+(mode=="crawl"))


def test_b37_local_empty_universe_has_no_fake_failure_units(tmp_path,monkeypatch):
    cfg,db=configure(tmp_path);seed(db,codes=())
    session=Session(db)
    assert invoke(monkeypatch,cfg,session,"incremental")==1
    result=run_row(db)
    assert result["status"]=="failed" and result["detail"]=="no_units" and result["failed_units"]==0
    assert count(db,"ingest_unit")==0 and session.calls==[]


def test_b37_discovery_empty_is_a_single_prerequisite_failure(tmp_path,monkeypatch):
    cfg,db=configure(tmp_path);seed(db)
    before=query(db,"SELECT * FROM stock_list ORDER BY secucode")
    session=Session(db,stocks=lambda page,params:FakeResponse(body=stock_body([],0)))
    assert invoke(monkeypatch,cfg,session,"crawl")==1
    result=run_row(db);assert result["status"]=="failed" and result["failed_units"]==1
    units=query(db,"SELECT * FROM ingest_unit");assert len(units)==1 and units[0]["failure_code"]=="empty_universe"
    assert query(db,"SELECT * FROM stock_list ORDER BY secucode")==before
    assert all(c["kind"]=="stock" for c in session.calls)


@pytest.mark.parametrize("mode",["crawl","incremental"])
@pytest.mark.parametrize("key,value",[
    ("crawl.recent_periods",0),("crawl.recent_periods",True),("crawl.stock_count",0),
    ("crawler.page_size",False),("crawler.max_retries",0),("crawl.max_pages_per_unit",-1),
    ("raw_store.max_body_bytes",0),("raw_store.root",""),("raw_store.root",42),
    ("crawl.report_names",[]),("crawl.report_names",["unsupported"]),
    ("crawler.timeout","15"),("crawler.min_interval",float("nan")),
    ("crawler.base_url","https://user:secret@example.test/path"),
])
def test_b38_invalid_scope_is_rejected_before_database_or_requests(tmp_path,monkeypatch,mode,key,value):
    cfg,db=configure(tmp_path,**{key:value});session=Session(db)
    def forbidden(config):raise AssertionError("configuration validation must precede DB initialization")
    monkeypatch.setattr(cli,"_get_db_and_repo",forbidden)
    assert invoke(monkeypatch,cfg,session,mode)==2
    assert session.calls==[] and not db.exists()


@pytest.mark.parametrize("shape",["missing_file","yaml_list","raw_file_ancestor","db_directory"])
def test_b38_invalid_filesystem_or_configuration_shape(tmp_path,monkeypatch,shape):
    cfg,db=configure(tmp_path)
    if shape=="missing_file":cfg.unlink()
    elif shape=="yaml_list":cfg.write_text("- bad-root\n")
    else:
        data=yaml.safe_load(cfg.read_text())
        if shape=="raw_file_ancestor":
            p=tmp_path/"blocked";p.write_text("file");data["raw_store"]["root"]=str(p/"raw")
        else:data["database"]["path"]=str(tmp_path)
        cfg.write_text(yaml.safe_dump(data))
    session=Session(db)
    assert invoke(monkeypatch,cfg,session,"crawl")==2 and session.calls==[]
    assert not db.exists()


@pytest.mark.parametrize("bad",["http","structure","changed_total","empty_page","market","bool_market","code","ret","missing_total","bool_total"])
def test_b39_discovery_failure_retains_bodies_without_updating_list(tmp_path,monkeypatch,bad):
    cfg,db=configure(tmp_path,**{"crawl.stock_count":3});seed(db)
    before=query(db,"SELECT * FROM stock_list ORDER BY secucode")
    first=stock_body([stock(),stock("000002")],3)
    second=stock_body([stock("000003")],3)
    if bad=="http":response=FakeResponse(status_code=500,body=b"failed second page")
    elif bad=="structure":response=FakeResponse(body=json.dumps({"rc":0,"data":{"diff":{},"total":3}}).encode())
    elif bad=="changed_total":response=FakeResponse(body=stock_body([stock("000003")],4))
    elif bad=="empty_page":response=FakeResponse(body=stock_body([],3))
    elif bad=="market":response=FakeResponse(body=stock_body([stock("000003",2)],3))
    elif bad=="bool_market":response=FakeResponse(body=stock_body([stock("000003",True)],3))
    elif bad=="code":response=FakeResponse(body=stock_body([stock("BAD")],3))
    elif bad=="ret":response=FakeResponse(body=stock_body([stock("000003")],3,ret=7))
    elif bad=="missing_total":response=FakeResponse(body=json.dumps({"data":{"diff":[stock("000003")]}}).encode())
    else:response=FakeResponse(body=stock_body([stock("000003")],True))
    session=Session(db,stocks=lambda page,params:FakeResponse(body=first) if page==1 else response)
    assert invoke(monkeypatch,cfg,session,"crawl")==1
    assert query(db,"SELECT * FROM stock_list ORDER BY secucode")==before
    assert count(db,"statement_version")==0 and count(db,"crawl_watermark")==0
    assert count(db,"raw_snapshot")==2
    assert all(c["kind"]=="stock" for c in session.calls)
    assert run_row(db)["status"]=="failed" and run_row(db)["failed_units"]==1


@pytest.mark.parametrize("requested,rows,total",[(1,[stock(),stock("000002")],2),(10,[stock()],1)])
def test_b40_last_page_is_complete_and_declared_exhaustion_succeeds(tmp_path,monkeypatch,requested,rows,total):
    cfg,db=configure(tmp_path,**{"crawl.stock_count":requested})
    body=stock_body(rows,total)
    session=Session(db,stocks=lambda page,params:FakeResponse(body=body))
    assert invoke(monkeypatch,cfg,session,"crawl")==0
    selected=min(requested,len(rows))
    assert count(db,"stock_list")==selected and count(db,"statement_version")==selected
    unit=query(db,"SELECT * FROM ingest_unit WHERE unit_kind='stock_list'")[0]
    assert unit["source_rows"]==len(rows) and unit["selected_rows"]==selected
    snap=query(db,"SELECT * FROM raw_snapshot WHERE report_name='stock_list'")[0]
    store=RawStore(tmp_path/"evidence",readonly=True)
    assert store.verify({"file_path":snap["file_path"],"raw_hash":snap["raw_content_hash"],"raw_bytes":snap["raw_bytes"]}).body==body
    assert len([c for c in session.calls if c["kind"]=="stock"])==1


def test_b41_terminal_write_failure_preserves_committed_units(tmp_path,monkeypatch):
    cfg,db=configure(tmp_path);seed(db)
    original=Repository.finalize_run
    def broken(self,*args,**kwargs):
        original(self,*args,**kwargs)
        raise sqlite3.OperationalError("injected terminal write failure")
    monkeypatch.setattr(Repository,"finalize_run",broken)
    assert invoke(monkeypatch,cfg,Session(db),"incremental")==1
    assert run_row(db)["status"]=="running"
    assert count(db,"statement_version")==2 and count(db,"crawl_watermark")==2
    conn=get_readonly_connection(db)
    try:assert "unfinished_run" in {f.code for f in audit_raw(conn,RawStore(tmp_path/"evidence",readonly=True)).findings}
    finally:conn.close()


def test_b41_keyboard_interrupt_retains_unfinished_attempt(tmp_path,monkeypatch):
    cfg,db=configure(tmp_path);seed(db)
    session=Session(db,financial=lambda page,params:KeyboardInterrupt())
    assert invoke(monkeypatch,cfg,session,"incremental")==1 and session.closed
    assert run_row(db)["status"]=="running"
    assert count(db,"statement_version")==0
    assert query(db,"SELECT outcome FROM ingest_attempt")[0]["outcome"]=="running"
    assert count(db,"raw_snapshot")==0


def test_b42_cli_audit_of_new_capture_is_read_only(tmp_path,monkeypatch):
    import hashlib
    cfg,db=configure(tmp_path)
    assert invoke(monkeypatch,cfg,Session(db),"crawl")==0
    root=tmp_path/"evidence"
    before={p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob("*") if p.is_file()}
    db_hash=hashlib.sha256(db.read_bytes()).hexdigest()
    args=Namespace(config=str(cfg),run_id=None,all_runs=True,report_dir=str(tmp_path/"audit"))
    assert cli.cmd_audit_raw(args)==0
    assert before=={p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert hashlib.sha256(db.read_bytes()).hexdigest()==db_hash
    assert list((tmp_path/"audit").glob("*.txt"))


def test_b44_body_never_enters_logs_and_params_remain_whitelisted(tmp_path,monkeypatch):
    cfg,db=configure(tmp_path);seed(db)
    marker="PRIVATE_BODY_MARKER_7421"
    body=json.dumps({"success":False,"message":marker}).encode()
    messages=[];original=logging.Logger._log
    def capture(self,level,msg,args,*a,**kw):
        messages.append(str(msg)%args if args else str(msg))
        return original(self,level,msg,args,*a,**kw)
    monkeypatch.setattr(logging.Logger,"_log",capture)
    assert invoke(monkeypatch,cfg,Session(db,financial=lambda page,params:FakeResponse(body=body)),"incremental")==1
    assert marker not in "\n".join(messages)
    for snap in query(db,"SELECT * FROM raw_snapshot"):
        params=json.loads(snap["request_params"])
        assert set(params)<={"reportName","columns","pageSize","pageNumber","sortColumns","sortTypes","filter"}
        assert marker not in snap["request_params"]


def test_b44_generated_evidence_paths_are_git_ignored():
    paths=["data/raw/eastmoney/run/aa/unit/p000001/a000001.body.gz",
           "custom/eastmoney/run/aa/unit/p000001/.a000001.body.gz.uuid.tmp",
           "reports/generated.txt","logs/generated.log"]
    for path in paths:
        assert subprocess.run(["git","check-ignore","--no-index",path],capture_output=True).returncode==0,path


def test_b45_cli_does_not_run_cleaning_or_switch_consumers(tmp_path,monkeypatch):
    cfg,db=configure(tmp_path)
    assert invoke(monkeypatch,cfg,Session(db),"crawl")==0
    assert count(db,"clean_financial_indicator")==0 and count(db,"features")==0
    assert all(row["weightavg_roe"]==16.75 for row in query(db,"SELECT weightavg_roe FROM raw_financial_indicator"))
    assert all(json.loads(row["standardized_fields"])["weightavg_roe"]==0.1675 for row in query(db,"SELECT standardized_fields FROM statement_version"))
    before=count(db,"statement_observation")
    cli.cmd_clean(Namespace(config=str(cfg)))
    cli.cmd_features(Namespace(config=str(cfg)))
    assert count(db,"statement_observation")==before
    assert count(db,"ingest_run")==1
    assert count(db,"clean_financial_indicator")==2
    assert all(row["weightavg_roe"]==16.75 for row in query(db,"SELECT weightavg_roe FROM clean_financial_indicator"))


def test_b46_cli_versions_have_real_file_and_row_evidence(tmp_path,monkeypatch):
    cfg,db=configure(tmp_path)
    assert invoke(monkeypatch,cfg,Session(db),"crawl")==0
    store=RawStore(tmp_path/"evidence",readonly=True)
    assert {row["evidence_sufficient"] for row in query(db,"SELECT evidence_sufficient FROM statement_version")}=={1}
    rows=query(db,"SELECT o.row_index,s.* FROM statement_observation o JOIN raw_snapshot s ON o.snapshot_id=s.snapshot_id")
    assert len(rows)==2
    for row in rows:
        original=store.verify({"file_path":row["file_path"],"raw_hash":row["raw_content_hash"],"raw_bytes":row["raw_bytes"]}).body
        assert json.loads(original)["result"]["data"][row["row_index"]]["SECUCODE"]==row["secucode"]
        assert row["raw_format"]=="http-body-v1" and row["validation_status"]=="valid"

@pytest.mark.parametrize("declared_total", [4, 1000])
def test_b40_multiple_pages_can_finish_at_n_without_exhausting_source(tmp_path, monkeypatch, declared_total):
    cfg, db = configure(tmp_path, **{"crawl.stock_count": 3})
    def stocks(page, params):
        rows = [stock(), stock("000002")] if page == 1 else [stock("000003"), stock("000004")]
        return FakeResponse(body=stock_body(rows, declared_total))
    session = Session(db, stocks=stocks)
    assert invoke(monkeypatch, cfg, session, "crawl") == 0
    assert [c["page"] for c in session.calls if c["kind"] == "stock"] == [1, 2]
    assert count(db, "stock_list") == count(db, "statement_version") == 3
    assert not query(db, "SELECT * FROM stock_list WHERE secucode='000004.SZ'")
    unit = query(db, "SELECT * FROM ingest_unit WHERE unit_kind='stock_list'")[0]
    assert unit["source_rows"] == 4 and unit["selected_rows"] == 3
    conn = get_readonly_connection(db)
    try:
        assert audit_raw(conn, RawStore(tmp_path / "evidence", readonly=True)).clean
    finally:
        conn.close()


def test_b39_discovery_limit_keeps_partial_evidence_without_list_update(tmp_path, monkeypatch):
    cfg, db = configure(tmp_path, **{"crawl.stock_count": 3, "crawl.max_pages_per_unit": 1})
    seed(db);before = query(db, "SELECT * FROM stock_list ORDER BY secucode")
    session = Session(db, stocks=lambda p, params: FakeResponse(body=stock_body([stock(), stock("000002")], 4)))
    assert invoke(monkeypatch, cfg, session, "crawl") == 1
    assert query(db, "SELECT * FROM stock_list ORDER BY secucode") == before
    assert len(session.calls) == 1 and count(db, "raw_snapshot") == 1
    assert query(db, "SELECT failure_code FROM ingest_unit")[0]["failure_code"] == "incomplete_scope"


def test_b39_stock_list_write_failure_rolls_back_before_financial_plan(tmp_path, monkeypatch):
    cfg, db = configure(tmp_path);seed(db)
    before = query(db, "SELECT * FROM stock_list ORDER BY secucode")
    original = Repository.upsert_stock_list
    def broken(self, rows):
        original(self, rows)
        raise sqlite3.OperationalError("injected after stock list write")
    monkeypatch.setattr(Repository, "upsert_stock_list", broken)
    session = Session(db)
    assert invoke(monkeypatch, cfg, session, "crawl") == 1
    assert query(db, "SELECT * FROM stock_list ORDER BY secucode") == before
    assert count(db, "raw_snapshot") == 1 and count(db, "statement_version") == 0
    assert all(c["kind"] == "stock" for c in session.calls)
    assert query(db, "SELECT status FROM ingest_unit")[0]["status"] == "failed"


@pytest.mark.parametrize("mode", ["crawl", "incremental"])
def test_b31_cli_actual_commit_failure_has_nonzero_exit_and_zero_business_rows(tmp_path, monkeypatch, mode):
    cfg, db = configure(tmp_path);seed(db)
    class CommitFault(sqlite3.Connection):
        armed = True
        def commit(self):
            if self.armed and self.execute("SELECT 1 FROM ingest_unit WHERE unit_kind='financial' AND status='success'").fetchone():
                self.armed = False
                raise sqlite3.OperationalError("injected commit fault, no prior rollback")
            return super().commit()
    def database(config):
        conn = sqlite3.connect(db, factory=CommitFault)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        database = Database(str(db));database._conn = conn
        return database, Repository(conn)
    monkeypatch.setattr(cli, "_get_db_and_repo", database)
    # Fail both financial units, each at its actual commit boundary.
    original = Repository.mark_unit_running
    def arm(self, unit_id, **kwargs):
        if self.get_unit(unit_id)["unit_kind"] == "financial": self.conn.armed = True
        return original(self, unit_id, **kwargs)
    monkeypatch.setattr(Repository, "mark_unit_running", arm)
    assert invoke(monkeypatch, cfg, Session(db), mode) == 1
    assert run_row(db)["status"] == "failed" and run_row(db)["failed_units"] == 2
    assert count(db, "statement_version") == count(db, "statement_observation") == 0
    assert count(db, "raw_financial_indicator") == count(db, "crawl_watermark") == 0
    assert count(db, "raw_snapshot") == 2 + (mode == "crawl")


def test_b18_cli_storage_failure_is_not_retried(tmp_path, monkeypatch):
    from fin_feature.storage.raw_store import RawStoreError
    cfg, db = configure(tmp_path, **{"crawler.max_retries": 3});seed(db, codes=("000001.SZ",))
    def broken(*args, **kwargs): raise RawStoreError("injected evidence failure")
    monkeypatch.setattr(RawStore, "publish", broken)
    session = Session(db)
    assert invoke(monkeypatch, cfg, session, "incremental") == 1
    assert len(session.calls) == 1
    assert count(db, "statement_version") == count(db, "crawl_watermark") == 0
    assert query(db, "SELECT outcome FROM ingest_attempt")[0]["outcome"] == "storage_error"


def test_b42_stock_success_claim_must_satisfy_n_or_source_exhaustion(evidence_env):
    env = evidence_env;env.new_run()
    unit = env.new_unit(unit_kind="stock_list", request_scope={"stock_count": 3, "page_size": 2})
    env.repo.mark_unit_running(unit["unit_id"])
    rows = [stock(), stock("000002")]
    env.capture(unit, rows=rows, body=stock_body(rows, 3))
    env.repo.finish_unit(unit["unit_id"], "success", source_rows=2, selected_rows=2)
    env.repo.finalize_run(unit["run_id"], unit_kinds=("stock_list",))
    report = audit_raw(env.conn, env.store)
    assert not report.clean and "missing_declared_page" in {f.code for f in report.findings}


def test_b38_empty_yaml_list_is_not_silently_defaulted(tmp_path, monkeypatch):
    cfg, db = configure(tmp_path);cfg.write_text("[]\n")
    session = Session(db)
    assert invoke(monkeypatch, cfg, session, "crawl") == 2
    assert not db.exists() and session.calls == []


def test_b34_configured_report_whitelist_drives_both_entries(tmp_path, monkeypatch):
    from fin_feature.constants import REPORT_INCOME_STATEMENT, REPORT_BALANCE_SHEET
    cfg, db = configure(tmp_path, **{"crawl.report_names": [FIN, REPORT_INCOME_STATEMENT, REPORT_BALANCE_SHEET]})
    session = Session(db)
    assert invoke(monkeypatch, cfg, session, "crawl") == 0
    assert count(db, "ingest_run") == 1 and count(db, "ingest_unit") == 7
    assert count(db, "statement_version") == 6
    assert {call["params"]["reportName"] for call in session.calls if call["kind"] == "financial"} == {FIN, REPORT_INCOME_STATEMENT, REPORT_BALANCE_SHEET}
