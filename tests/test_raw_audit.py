"""2B-1 验收矩阵 B42：audit-raw 只读对账。

对应 docs/stage-2/2b-test-matrix.md（2B-3 列出的 B42，其存储/对账部分属 2B-1）。
用例覆盖：缺文件、篡改、孤儿、临时文件、legacy 引用、未完成 run/unit/attempt、
成功单元缺证据，以及"报告前后数据库与文件状态不变、不补抓不删除"。
"""

import gzip
import json
import sqlite3
from argparse import Namespace
from pathlib import Path

import pytest

from fin_feature.storage.raw_audit import audit_raw
from fin_feature.storage.raw_store import PublishContext


def audit_codes(report) -> set[str]:
    return {f.code for f in report.findings}


def make_healthy_run(env) -> dict:
    """一个干净批次：单元成功、页证据有效、run 已终态。"""
    env.new_run()
    unit = env.new_unit()
    env.repo.mark_unit_running(unit["unit_id"])
    captured = env.capture(unit, rows=[{"TOTAL_OPERATE_INCOME": 100}])
    env.repo.finish_unit(unit["unit_id"], "success", source_rows=1, selected_rows=1)
    env.repo.finalize_run(unit["run_id"])
    return {"unit": unit, "captured": captured}


def db_fingerprint(conn) -> dict:
    tables = [
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
    ]
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


def file_fingerprint(root: Path) -> dict:
    return {
        p.relative_to(root).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*")) if p.is_file()
    }


class TestB42CleanAndFindings:
    def test_healthy_run_is_clean(self, evidence_env):
        env = evidence_env
        make_healthy_run(env)
        report = audit_raw(env.conn, env.store)

        assert report.errors == []
        assert report.clean is True
        assert report.exit_code() == 0
        assert report.scope == "all"
        assert "audit-raw" in report.format_text()

    def test_legal_empty_unit_is_clean(self, evidence_env):
        """合法空页：success_empty + 有效空响应证据 → 干净。"""
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        env.repo.mark_unit_running(unit["unit_id"])
        env.capture(unit, rows=[])
        env.repo.finish_unit(unit["unit_id"], "success_empty")
        env.repo.finalize_run(unit["run_id"])

        report = audit_raw(env.conn, env.store)
        assert report.clean is True

    def test_missing_file_reported(self, evidence_env):
        env = evidence_env
        state = make_healthy_run(env)
        (env.store.root / state["captured"].file_path).unlink()

        report = audit_raw(env.conn, env.store)
        assert "missing_file" in audit_codes(report)
        assert report.exit_code() == 1

    def test_tampered_file_reported(self, evidence_env):
        env = evidence_env
        state = make_healthy_run(env)
        target = env.store.root / state["captured"].file_path
        target.write_bytes(gzip.compress(b'{"tampered":1}', mtime=0))

        report = audit_raw(env.conn, env.store)
        assert "file_verification_failed" in audit_codes(report)
        assert report.exit_code() == 1

    def test_orphan_and_temp_files_reported(self, evidence_env):
        env = evidence_env
        state = make_healthy_run(env)
        target = env.store.root / state["captured"].file_path
        orphan = target.parent / "a000002.body.gz"
        orphan.write_bytes(gzip.compress(b'{"orphan":1}', mtime=0))
        temp = target.parent / ".a000003.body.gz.deadbeef.tmp"
        temp.write_bytes(b"partial")

        report = audit_raw(env.conn, env.store)
        codes = audit_codes(report)
        assert {"orphan_file", "temp_file"} <= codes

        # 只读承诺：孤儿与临时文件不被删除
        assert orphan.exists() and temp.exists()

    def test_legacy_snapshot_is_warning_only(self, evidence_env):
        env = evidence_env
        env.new_run()
        env.repo.save_raw_snapshot(
            env.run_id, env.source_name, "RPT_LICO_FN_CPD", "000001.SZ", 1,
            "{}", "a" * 64, "/legacy/old.json.gz", 1,
        )
        env.repo.finish_ingest_run(env.run_id, "success")

        report = audit_raw(env.conn, env.store)
        assert "legacy_unverified" in audit_codes(report)
        assert [f.code for f in report.warnings] == ["legacy_unverified"]
        assert report.clean is True
        assert report.exit_code() == 0

    def test_unfinished_run_unit_attempt_reported(self, evidence_env):
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        env.repo.mark_unit_running(unit["unit_id"])
        attempt = env.repo.register_attempt(unit["unit_id"], 1, 1)  # 未结算
        failed_attempt = env.repo.register_attempt(unit["unit_id"], 2, 1)
        env.repo.finish_attempt(failed_attempt, "http_error", error_code="500")

        report = audit_raw(env.conn, env.store)
        codes = audit_codes(report)
        assert {"unfinished_run", "unfinished_unit", "unfinished_attempt",
                "attempt_without_snapshot"} <= codes
        assert report.exit_code() == 1
        # 未结算 attempt 不被自动判成功
        assert env.repo.get_attempt(attempt)["outcome"] == "running"
        assert env.repo.get_unit(unit["unit_id"])["status"] == "running"

    def test_transport_error_without_snapshot_allowed(self, evidence_env):
        """网络无响应：只有 attempt、无 snapshot，不是 attempt_without_snapshot 异常。

        但带 transport_error 最终页的单元不应标成功——这是失败单元，对账应报告
        其证据缺口（R8：成功单元的全部最终页必须已核验）。
        """
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        env.repo.mark_unit_running(unit["unit_id"])
        attempt = env.repo.register_attempt(unit["unit_id"], 1, 1)
        env.repo.finish_attempt(attempt, "transport_error", error_code="timeout")
        env.capture(unit, page=2, rows=[{"TOTAL_OPERATE_INCOME": 100}])
        env.repo.finish_unit(unit["unit_id"], "failed", failure_code="transport_error")
        env.repo.finalize_run(unit["run_id"])

        report = audit_raw(env.conn, env.store)
        # transport_error 允许无 snapshot（第 3 类保留对象），不报 attempt_without_snapshot
        assert "attempt_without_snapshot" not in audit_codes(report)
        # 失败单元不要求证据完整；对账干净
        assert report.clean is True

    def test_transport_error_attempt_not_flagged_without_snapshot(self, evidence_env):
        """独立检查：transport_error 的 attempt 本身不报 attempt_without_snapshot。"""
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        env.repo.mark_unit_running(unit["unit_id"])
        attempt = env.repo.register_attempt(unit["unit_id"], 1, 1)
        env.repo.finish_attempt(attempt, "transport_error", error_code="timeout")
        env.repo.finish_unit(unit["unit_id"], "failed", failure_code="transport_error")
        env.repo.finalize_run(unit["run_id"])

        report = audit_raw(env.conn, env.store)
        assert "attempt_without_snapshot" not in audit_codes(report)

    def test_success_unit_without_evidence_reported(self, evidence_env):
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        env.repo.mark_unit_running(unit["unit_id"])
        env.repo.finish_unit(unit["unit_id"], "success")

        report = audit_raw(env.conn, env.store)
        assert "unit_success_without_evidence" in audit_codes(report)

    def test_run_scope_filter(self, evidence_env):
        env = evidence_env
        healthy = make_healthy_run(env)
        other_run = env.repo.create_ingest_run(env.source_name)
        broken = env.new_unit(run_id=other_run, secucode="600000.SH")
        env.repo.mark_unit_running(broken["unit_id"])
        captured = env.capture(broken, rows=[{"TOTAL_OPERATE_INCOME": 5}])
        (env.store.root / captured.file_path).unlink()

        scoped = audit_raw(env.conn, env.store, run_id=healthy["unit"]["run_id"])
        assert scoped.clean is True
        assert scoped.scope == healthy["unit"]["run_id"]

        everything = audit_raw(env.conn, env.store)
        assert "missing_file" in audit_codes(everything)
        assert everything.exit_code() == 1


class TestB42ReadOnly:
    def test_database_and_files_unchanged(self, evidence_env):
        env = evidence_env
        state = make_healthy_run(env)
        target = env.store.root / state["captured"].file_path
        orphan = target.parent / "a000009.body.gz"
        orphan.write_bytes(gzip.compress(b'{"orphan":1}', mtime=0))

        db_before = db_fingerprint(env.conn)
        files_before = file_fingerprint(env.store.root)

        first = audit_raw(env.conn, env.store)
        second = audit_raw(env.conn, env.store)

        assert db_fingerprint(env.conn) == db_before
        assert file_fingerprint(env.store.root) == files_before
        assert audit_codes(first) == audit_codes(second)
        assert orphan.exists()

    def test_report_serialisation(self, evidence_env):
        env = evidence_env
        make_healthy_run(env)
        report = audit_raw(env.conn, env.store)
        payload = report.as_dict()
        assert payload["clean"] is True
        assert payload["scope"] == "all"
        assert payload["findings"] == []


class TestAuditRawCLI:
    def _write_config(self, tmp_path: Path, db_path: Path, raw_root: Path) -> Path:
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "database:\n"
            f"  path: {db_path}\n"
            "raw_store:\n"
            f"  root: {raw_root}\n"
            "logging:\n"
            "  level: INFO\n"
            f"  file: {tmp_path / 'cli.log'}\n",
            encoding="utf-8",
        )
        return cfg

    @staticmethod
    def _file_env(tmp_path, name):
        """真实文件库 + 真实证据目录的证据环境（CLI 需要独立连接文件库）。"""
        from evidence_helpers import EvidenceEnv
        from fin_feature.storage.database import get_connection
        from fin_feature.storage.migrations import run_migrations

        db_path = tmp_path / f"{name}.db"
        conn = get_connection(str(db_path))
        run_migrations(conn)
        env = EvidenceEnv(conn=conn, raw_root=str(tmp_path / f"{name}-raw"))
        return env, db_path

    def test_cli_reports_findings_and_writes_report(self, tmp_path):
        from fin_feature.cli import cmd_audit_raw

        env, db_path = self._file_env(tmp_path, "broken")
        state = make_healthy_run(env)
        (env.store.root / state["captured"].file_path).unlink()
        env.conn.commit()
        env.conn.close()

        report_dir = tmp_path / "audit"
        args = Namespace(
            config=str(self._write_config(tmp_path, db_path, env.store.root)),
            run_id=None, all_runs=True, report_dir=str(report_dir),
        )
        assert cmd_audit_raw(args) == 1

        reports = list(report_dir.glob("audit-raw_*.txt"))
        assert len(reports) == 1
        assert "missing_file" in reports[0].read_text(encoding="utf-8")

    def test_cli_clean_run_exits_zero(self, tmp_path):
        from fin_feature.cli import cmd_audit_raw

        env, db_path = self._file_env(tmp_path, "clean")
        make_healthy_run(env)
        env.conn.commit()
        env.conn.close()

        args = Namespace(
            config=str(self._write_config(tmp_path, db_path, env.store.root)),
            run_id=None, all_runs=True, report_dir=str(tmp_path / "audit"),
        )
        assert cmd_audit_raw(args) == 0

    def test_cli_requires_scope_or_all(self, tmp_path):
        from fin_feature.cli import cmd_audit_raw

        args = Namespace(
            config=str(self._write_config(tmp_path, tmp_path / "x.db", tmp_path / "raw")),
            run_id=None, all_runs=False, report_dir=str(tmp_path / "audit"),
        )
        assert cmd_audit_raw(args) == 2
        assert not (tmp_path / "audit").exists()

    def test_cli_missing_v4_structure_returns_one(self, tmp_path):
        from fin_feature.cli import cmd_audit_raw

        import sqlite3

        db_path = tmp_path / "empty.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.commit()
        conn.close()

        args = Namespace(
            config=str(self._write_config(tmp_path, db_path, tmp_path / "raw")),
            run_id="run-x", all_runs=False, report_dir=str(tmp_path / "audit"),
        )
        assert cmd_audit_raw(args) == 1


# ===========================================================================
# 复审修复 R2/R3（2026-10-17，响应独立审阅 b8b2b3f）
# ===========================================================================

class TestR2AuditReadOnly:
    """R2：audit-raw 不改 journal_mode、不创建数据库/证据根目录。"""

    def test_journal_mode_unchanged_and_root_not_created(self, tmp_path):
        from fin_feature.cli import cmd_audit_raw

        # 用 delete 模式建库（非 WAL），证明 audit 不强制改 journal_mode
        db_path = tmp_path / "existing.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        from fin_feature.storage.migrations import run_migrations
        run_migrations(conn)
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.commit()
        conn.close()

        before = sqlite3.connect(str(db_path)).execute("PRAGMA journal_mode").fetchone()[0]
        raw_root = tmp_path / "raw-not-created"
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "database:\n  path: " + str(db_path) + "\n"
            "raw_store:\n  root: " + str(raw_root) + "\n"
            "logging:\n  level: ERROR\n  file: " + str(tmp_path / "cli.log") + "\n",
            encoding="utf-8",
        )
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            code = cmd_audit_raw(Namespace(
                config=str(cfg), run_id=None, all_runs=True,
                report_dir=str(tmp_path / "reports"),
            ))

        after = sqlite3.connect(str(db_path)).execute("PRAGMA journal_mode").fetchone()[0]
        assert before == "delete" and after == "delete"
        assert not raw_root.exists(), "只读对账不得创建证据根目录"

    def test_audit_with_existing_root_writes_report(self, tmp_path):
        """证据根已存在时，audit 正常运行并写报告，但不改 journal_mode。"""
        from fin_feature.cli import cmd_audit_raw

        db_path = tmp_path / "existing.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        from fin_feature.storage.migrations import run_migrations
        run_migrations(conn)
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.commit()
        conn.close()

        raw_root = tmp_path / "raw"
        raw_root.mkdir()
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "database:\n  path: " + str(db_path) + "\n"
            "raw_store:\n  root: " + str(raw_root) + "\n"
            "logging:\n  level: ERROR\n  file: " + str(tmp_path / "cli.log") + "\n",
            encoding="utf-8",
        )
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            code = cmd_audit_raw(Namespace(
                config=str(cfg), run_id=None, all_runs=True,
                report_dir=str(tmp_path / "reports"),
            ))
        assert code == 0
        after = sqlite3.connect(str(db_path)).execute("PRAGMA journal_mode").fetchone()[0]
        assert after == "delete"
        assert (tmp_path / "reports").exists()

    def test_missing_database_returns_one(self, tmp_path):
        from fin_feature.cli import cmd_audit_raw

        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "database:\n  path: " + str(tmp_path / "nope.db") + "\n"
            "raw_store:\n  root: " + str(tmp_path / "raw") + "\n"
            "logging:\n  level: ERROR\n  file: " + str(tmp_path / "cli.log") + "\n",
            encoding="utf-8",
        )
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            code = cmd_audit_raw(Namespace(
                config=str(cfg), run_id=None, all_runs=True,
                report_dir=str(tmp_path / "reports"),
            ))
        assert code == 1
        assert not (tmp_path / "nope.db").exists(), "只读连接不得创建空库"

    def test_missing_raw_root_returns_one(self, tmp_path):
        from fin_feature.cli import cmd_audit_raw

        db_path = tmp_path / "existing.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        from fin_feature.storage.migrations import run_migrations
        run_migrations(conn)
        conn.commit()
        conn.close()

        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "database:\n  path: " + str(db_path) + "\n"
            "raw_store:\n  root: " + str(tmp_path / "raw-missing") + "\n"
            "logging:\n  level: ERROR\n  file: " + str(tmp_path / "cli.log") + "\n",
            encoding="utf-8",
        )
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            code = cmd_audit_raw(Namespace(
                config=str(cfg), run_id=None, all_runs=True,
                report_dir=str(tmp_path / "reports"),
            ))
        assert code == 1
        assert not (tmp_path / "raw-missing").exists()


class TestR3NoFalseOrphans:
    """R3：按 run 过滤时不把其他健康批次的合法文件误报为孤儿。"""

    def test_two_healthy_runs_filtered_clean(self, evidence_env):
        env = evidence_env
        state_a = make_healthy_run(env)
        make_healthy_run(env)

        all_report = audit_raw(env.conn, env.store)
        one_report = audit_raw(env.conn, env.store, run_id=state_a["unit"]["run_id"])

        assert all_report.clean is True
        assert one_report.clean is True, "只查一个 run 不应把另一个 run 的文件报为孤儿"
        assert "orphan_file" not in audit_codes(one_report)

    def test_other_run_orphan_not_in_scope(self, evidence_env):
        """另一批次的孤儿不串入当前 run 的对账范围。"""
        env = evidence_env
        state_a = make_healthy_run(env)
        state_b = make_healthy_run(env)
        # 在 b 的目录下放一个孤儿
        orphan = (env.store.root / state_b["captured"].file_path).parent / "a000009.body.gz"
        orphan.write_bytes(gzip.compress(b'{"orphan":1}', mtime=0))

        scoped = audit_raw(env.conn, env.store, run_id=state_a["unit"]["run_id"])
        assert scoped.clean is True, "b 的孤儿不串入 a 的范围"

        everything = audit_raw(env.conn, env.store)
        assert "orphan_file" in audit_codes(everything)
        assert everything.exit_code() == 1


class TestR8SuccessUnitAllFinalPagesValid:
    """R8：成功单元的全部最终页必须已核验，不只看"至少一页 valid"。"""

    def test_pending_final_page_reported(self, evidence_env):
        """第一页 valid、第二页 pending → 成功单元对账不干净。"""
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        env.repo.mark_unit_running(unit["unit_id"])
        rows_a = [{"TOTAL_OPERATE_INCOME": 100}]
        rows_b = [{"TOTAL_OPERATE_INCOME": 200}]

        def body(rows):
            return json.dumps(
                {"success": True, "result": {"pages": 2, "count": 2, "data": rows}}
            ).encode()

        env.capture(unit, page=1, rows=rows_a, body=body(rows_a))
        env.capture(unit, page=2, rows=rows_b, body=body(rows_b), validation="pending")
        env.repo.finish_unit(unit["unit_id"], "success", source_rows=2, selected_rows=2)
        env.repo.finalize_run(unit["run_id"])

        report = audit_raw(env.conn, env.store)
        assert not report.clean
        assert "unit_success_without_evidence" in audit_codes(report)
        assert report.exit_code() == 1

    def test_invalid_final_page_reported(self, evidence_env):
        """第一页 valid、第二页 invalid → 成功单元对账不干净。"""
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        env.repo.mark_unit_running(unit["unit_id"])
        rows_a = [{"TOTAL_OPERATE_INCOME": 100}]
        rows_b = [{"TOTAL_OPERATE_INCOME": 200}]

        def body(rows):
            return json.dumps(
                {"success": True, "result": {"pages": 2, "count": 2, "data": rows}}
            ).encode()

        env.capture(unit, page=1, rows=rows_a, body=body(rows_a))
        env.capture(unit, page=2, rows=rows_b, body=body(rows_b), validation="invalid")
        env.repo.finish_unit(unit["unit_id"], "success", source_rows=2, selected_rows=2)
        env.repo.finalize_run(unit["run_id"])

        report = audit_raw(env.conn, env.store)
        assert "unit_success_without_evidence" in audit_codes(report)

    def test_all_final_pages_valid_clean(self, evidence_env):
        """两页都 valid → 成功单元对账干净。"""
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        env.repo.mark_unit_running(unit["unit_id"])
        rows_a = [{"TOTAL_OPERATE_INCOME": 100}]
        rows_b = [{"TOTAL_OPERATE_INCOME": 200}]

        def body(rows):
            return json.dumps(
                {"success": True, "result": {"pages": 2, "count": 2, "data": rows}}
            ).encode()

        env.capture(unit, page=1, rows=rows_a, body=body(rows_a))
        env.capture(unit, page=2, rows=rows_b, body=body(rows_b))
        env.repo.finish_unit(unit["unit_id"], "success", source_rows=2, selected_rows=2)
        env.repo.finalize_run(unit["run_id"])

        report = audit_raw(env.conn, env.store)
        assert report.clean is True

    def test_failed_retry_page_does_not_block(self, evidence_env):
        """历史失败重试页（非最终页）可保留，不要求其也 valid。

        C4：收到 HTTP 错误响应仍必须保留原体（invalid 快照）；只有 transport_error
        可无 snapshot。健康重试 fixture 保存真实失败响应及 invalid 快照，再断言
        后续成功不受影响。
        """
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        env.repo.mark_unit_running(unit["unit_id"])
        rows_a = [{"TOTAL_OPERATE_INCOME": 100}]

        def body(rows):
            return json.dumps(
                {"success": True, "result": {"pages": 1, "count": 1, "data": rows}}
            ).encode()

        # 页 1 第一次失败（http_error），保留失败响应原体（invalid 快照）
        failed_body = json.dumps(
            {"success": False, "result": {"pages": 1, "count": 0, "data": []}}
        ).encode()
        env.capture(
            unit, page=1, http_attempt=1, rows=[], body=failed_body,
            http_status=500, validation="invalid", outcome="http_error",
        )
        # 第二次成功
        env.capture(unit, page=1, http_attempt=2, rows=rows_a, body=body(rows_a))
        env.repo.finish_unit(unit["unit_id"], "success", source_rows=1, selected_rows=1)
        env.repo.finalize_run(unit["run_id"])

        report = audit_raw(env.conn, env.store)
        assert report.clean is True, "保留失败响应原体后，后续成功不应被阻断"


class TestC2AuditTerminalFailure:
    """C2：终态失败响应仍被接受为充分版本时，对账必须报告。"""

    def test_http_error_final_page_reported(self, evidence_env):
        """HTTP 500、outcome=http_error、快照标 valid 的成功单元 → 对账非干净。"""
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        env.repo.mark_unit_running(unit["unit_id"])
        rows = [{"TOTAL_OPERATE_INCOME": 100}]

        env.capture(
            unit, rows=rows, http_status=500, validation="valid", outcome="http_error",
        )
        env.repo.finish_unit(unit["unit_id"], "success", source_rows=1, selected_rows=1)
        env.repo.finalize_run(unit["run_id"])

        report = audit_raw(env.conn, env.store)
        assert not report.clean
        assert "terminal_failure_as_evidence" in audit_codes(report)

    def test_non_2xx_final_page_reported(self, evidence_env):
        """outcome=ok 但 HTTP 500 的成功单元 → 对账非干净。"""
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        env.repo.mark_unit_running(unit["unit_id"])
        rows = [{"TOTAL_OPERATE_INCOME": 100}]

        env.capture(
            unit, rows=rows, http_status=500, validation="valid", outcome="ok",
        )
        env.repo.finish_unit(unit["unit_id"], "success", source_rows=1, selected_rows=1)
        env.repo.finalize_run(unit["run_id"])

        report = audit_raw(env.conn, env.store)
        assert not report.clean
        assert "terminal_failure_as_evidence" in audit_codes(report)


class TestC3MissingDeclaredPage:
    """C3：声明页数与实际页数不符时，对账必须报告。"""

    def test_missing_middle_page_reported(self, evidence_env):
        """声明 3 页，只有页 1 和页 3 → 对账报告缺页。"""
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        env.repo.mark_unit_running(unit["unit_id"])
        rows = [{"TOTAL_OPERATE_INCOME": 100}]

        def body(rows, pages, count):
            return json.dumps(
                {"success": True, "result": {"pages": pages, "count": count, "data": rows}}
            ).encode()

        env.capture(unit, page=1, rows=rows, body=body(rows, 3, 3))
        env.capture(unit, page=3, rows=rows, body=body(rows, 3, 3))
        env.repo.finish_unit(unit["unit_id"], "success", source_rows=2, selected_rows=2)
        env.repo.finalize_run(unit["run_id"])

        report = audit_raw(env.conn, env.store)
        assert not report.clean
        assert "missing_declared_page" in audit_codes(report)

    def test_missing_tail_page_reported(self, evidence_env):
        """声明 2 页，只有页 1 → 对账报告缺页。"""
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        env.repo.mark_unit_running(unit["unit_id"])
        rows = [{"TOTAL_OPERATE_INCOME": 100}]

        def body(rows, pages, count):
            return json.dumps(
                {"success": True, "result": {"pages": pages, "count": count, "data": rows}}
            ).encode()

        env.capture(unit, page=1, rows=rows, body=body(rows, 2, 2))
        env.repo.finish_unit(unit["unit_id"], "success", source_rows=1, selected_rows=1)
        env.repo.finalize_run(unit["run_id"])

        report = audit_raw(env.conn, env.store)
        assert not report.clean
        assert "missing_declared_page" in audit_codes(report)

    def test_complete_pages_clean(self, evidence_env):
        """声明 2 页，页 1 和页 2 都在 → 对账干净（不回归）。"""
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        env.repo.mark_unit_running(unit["unit_id"])
        rows_a = [{"TOTAL_OPERATE_INCOME": 100}]
        rows_b = [{"TOTAL_OPERATE_INCOME": 200}]

        def body(rows, pages, count):
            return json.dumps(
                {"success": True, "result": {"pages": pages, "count": count, "data": rows}}
            ).encode()

        env.capture(unit, page=1, rows=rows_a, body=body(rows_a, 2, 2))
        env.capture(unit, page=2, rows=rows_b, body=body(rows_b, 2, 2))
        env.repo.finish_unit(unit["unit_id"], "success", source_rows=2, selected_rows=2)
        env.repo.finalize_run(unit["run_id"])

        report = audit_raw(env.conn, env.store)
        assert report.clean, f"完整页应干净：{[f.code for f in report.findings]}"


class TestC4FailedResponseEvidence:
    """C4：历史失败尝试收到响应但未留原体 → 对账报告证据缺口。"""

    def test_http_error_without_snapshot_reported(self, evidence_env):
        """页 1 第 1 次 http_error 无 snapshot，第 2 次成功 → 对账报告证据丢失。"""
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        env.repo.mark_unit_running(unit["unit_id"])
        rows = [{"TOTAL_OPERATE_INCOME": 100}]

        def body(rows):
            return json.dumps(
                {"success": True, "result": {"pages": 1, "count": 1, "data": rows}}
            ).encode()

        # 第 1 次 http_error，收到响应但未保留原体（无 snapshot）
        failed_attempt = env.repo.register_attempt(unit["unit_id"], 1, 1)
        env.repo.finish_attempt(failed_attempt, "http_error", error_code="500")
        # 第 2 次成功
        env.capture(unit, page=1, http_attempt=2, rows=rows, body=body(rows))
        env.repo.finish_unit(unit["unit_id"], "success", source_rows=1, selected_rows=1)
        env.repo.finalize_run(unit["run_id"])

        report = audit_raw(env.conn, env.store)
        assert not report.clean
        assert "failed_response_without_evidence" in audit_codes(report)

    def test_transport_error_without_snapshot_clean(self, evidence_env):
        """transport_error（无响应）无 snapshot → 对账干净（不伪造证据）。"""
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        env.repo.mark_unit_running(unit["unit_id"])
        rows = [{"TOTAL_OPERATE_INCOME": 100}]

        def body(rows):
            return json.dumps(
                {"success": True, "result": {"pages": 1, "count": 1, "data": rows}}
            ).encode()

        # 第 1 次 transport_error（无响应），无 snapshot
        failed_attempt = env.repo.register_attempt(unit["unit_id"], 1, 1)
        env.repo.finish_attempt(failed_attempt, "transport_error")
        # 第 2 次成功
        env.capture(unit, page=1, http_attempt=2, rows=rows, body=body(rows))
        env.repo.finish_unit(unit["unit_id"], "success", source_rows=1, selected_rows=1)
        env.repo.finalize_run(unit["run_id"])

        report = audit_raw(env.conn, env.store)
        assert report.clean, f"transport_error 无响应不应报缺口：{[f.code for f in report.findings]}"


class TestD3EnvelopeCompleteness:
    """D3：可选 count 缺失或解析失败时，分页完整性检查不被跳过。"""

    def test_missing_count_missing_page_reported(self, evidence_env):
        """无 count、声明 3 页、仅保存页 1 → 对账报告缺页。"""
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        env.repo.mark_unit_running(unit["unit_id"])
        rows = [{"TOTAL_OPERATE_INCOME": 100}]

        page = json.dumps(
            {"success": True, "result": {"pages": 3, "data": rows}}
        ).encode()
        env.capture(unit, page=1, rows=rows, body=page)
        env.repo.finish_unit(unit["unit_id"], "success", source_rows=1, selected_rows=1)
        env.repo.finalize_run(unit["run_id"])

        report = audit_raw(env.conn, env.store)
        assert not report.clean
        assert "missing_declared_page" in audit_codes(report)

    def test_missing_count_complete_pages_clean(self, evidence_env):
        """无 count、声明 2 页、页 1 和页 2 都在 → 对账干净（不回归）。"""
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        env.repo.mark_unit_running(unit["unit_id"])
        rows_a = [{"TOTAL_OPERATE_INCOME": 100}]
        rows_b = [{"TOTAL_OPERATE_INCOME": 200}]

        def body_no_count(rows, pages):
            return json.dumps(
                {"success": True, "result": {"pages": pages, "data": rows}}
            ).encode()

        env.capture(unit, page=1, rows=rows_a, body=body_no_count(rows_a, 2))
        env.capture(unit, page=2, rows=rows_b, body=body_no_count(rows_b, 2))
        env.repo.finish_unit(unit["unit_id"], "success", source_rows=2, selected_rows=2)
        env.repo.finalize_run(unit["run_id"])

        report = audit_raw(env.conn, env.store)
        assert report.clean, f"无 count 完整页应干净：{[f.code for f in report.findings]}"

    def test_html_body_reported(self, evidence_env):
        """HTTP 200/ok 的 HTML 原体被错误标 valid → 对账报告信封解析失败。"""
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        env.repo.mark_unit_running(unit["unit_id"])
        rows = [{"TOTAL_OPERATE_INCOME": 100}]

        env.capture(
            unit, rows=rows, body=b"<html>not-json</html>",
            validation="valid", outcome="ok",
        )
        env.repo.finish_unit(unit["unit_id"], "success", source_rows=1, selected_rows=1)
        env.repo.finalize_run(unit["run_id"])

        report = audit_raw(env.conn, env.store)
        assert not report.clean
        assert "envelope_parse_failed" in audit_codes(report)

    def test_non_json_body_reported(self, evidence_env):
        """非法 JSON 原体 → 对账报告信封解析失败。"""
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        env.repo.mark_unit_running(unit["unit_id"])
        rows = [{"TOTAL_OPERATE_INCOME": 100}]

        env.capture(
            unit, rows=rows, body=b"{not valid json",
            validation="valid", outcome="ok",
        )
        env.repo.finish_unit(unit["unit_id"], "success", source_rows=1, selected_rows=1)
        env.repo.finalize_run(unit["run_id"])

        report = audit_raw(env.conn, env.store)
        assert not report.clean
        assert "envelope_parse_failed" in audit_codes(report)
