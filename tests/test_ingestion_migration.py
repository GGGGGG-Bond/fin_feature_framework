"""2B-1 验收矩阵 B09-B12：v3→v4 迁移、备份与结构核验。

对应 docs/stage-2/2b-test-matrix.md「2B-1：证据、模型与失败边界」。
所有用例使用真实临时文件数据库（含 WAL），不使用假路径。
"""

import sqlite3

import pytest

from fin_feature.storage import migrations as m
from fin_feature.storage.database import get_connection
from fin_feature.storage.migrations import (
    backup_database_for_migration,
    collect_v4_structure_gaps,
    default_backup_path,
    migrate_with_backup,
    run_migrations,
    verify_v4_structure,
)

LATEST = max(version for version, _, _ in m.MIGRATIONS)


def make_v3_db(path) -> sqlite3.Connection:
    """真实临时文件库：迁移到 v3 并留下历史数据（raw/statement/run/snapshot）。"""
    conn = get_connection(str(path))
    for _version, _name, fn in m.MIGRATIONS:
        if _version > 3:
            break
        fn(conn)
    conn.execute(
        "INSERT INTO schema_version (version, name) VALUES (1, 'v1_baseline'),"
        "(2, 'v2_stage2'), (3, 'v3_rereview_fixes')"
    )
    conn.execute(
        "INSERT INTO raw_financial_indicator(secucode, report_date, notice_date, "
        "crawled_at, weightavg_roe) VALUES ('000001.SZ','2024-03-31','2024-04-15',"
        "'2024-04-15 10:00:00', 16.75)"
    )
    conn.execute(
        "INSERT INTO ingest_run (run_id, source_name, status, started_at) "
        "VALUES ('run-legacy', 'eastmoney', 'success', '2026-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO raw_snapshot (snapshot_id, run_id, source_name, report_name, "
        "secucode, page_number, raw_content_hash, file_path, record_count) "
        "VALUES ('snap-legacy', 'run-legacy', 'eastmoney', 'RPT_LICO_FN_CPD', "
        "'000001.SZ', 1, 'deadbeef', '/legacy/1.json.gz', 1)"
    )
    conn.execute(
        "INSERT INTO statement_version (version_id, snapshot_id, run_id, secucode, "
        "report_date, table_type, source_name, metric_hash, semantic_hash, "
        "observation_seq, standardized_fields, raw_fields, evidence_sufficient) "
        "VALUES ('ver-legacy', 'snap-legacy', 'run-legacy', '000001.SZ', '2024-03-31', "
        "'financial_indicator', 'eastmoney', ?, ?, 1, '{}', '{}', 0)",
        ("a" * 64, "b" * 64),
    )
    conn.execute(
        "INSERT INTO statement_observation (observation_id, version_id, snapshot_id, "
        "run_id, observed_at) VALUES ('obs-legacy', 'ver-legacy', 'snap-legacy', "
        "'run-legacy', '2026-01-01T00:00:00Z')"
    )
    conn.commit()
    return conn


class TestB09UpgradeFromV3:
    """B09：真实 v3 含历史库升级 v4，旧表/行/哈希与标记保持。"""

    def test_history_preserved_and_new_columns_defaulted(self, tmp_path):
        conn = make_v3_db(tmp_path / "legacy.db")
        before = {
            "raw": conn.execute("SELECT COUNT(*) FROM raw_financial_indicator").fetchone()[0],
            "snap": conn.execute("SELECT COUNT(*) FROM raw_snapshot").fetchone()[0],
            "sv": conn.execute("SELECT COUNT(*) FROM statement_version").fetchone()[0],
            "obs": conn.execute("SELECT COUNT(*) FROM statement_observation").fetchone()[0],
            "snap_hash": conn.execute(
                "SELECT raw_content_hash FROM raw_snapshot WHERE snapshot_id='snap-legacy'"
            ).fetchone()[0],
        }
        assert m._get_current_version(conn) == 3

        run_migrations(conn)

        assert m._get_current_version(conn) == LATEST
        assert collect_v4_structure_gaps(conn) == []
        # 旧行不删除、不重写
        assert conn.execute("SELECT COUNT(*) FROM raw_financial_indicator").fetchone()[0] \
            == before["raw"]
        assert conn.execute("SELECT COUNT(*) FROM statement_version").fetchone()[0] \
            == before["sv"]
        assert conn.execute("SELECT COUNT(*) FROM statement_observation").fetchone()[0] \
            == before["obs"]
        assert conn.execute(
            "SELECT raw_content_hash FROM raw_snapshot WHERE snapshot_id='snap-legacy'"
        ).fetchone()[0] == before["snap_hash"]
        # 旧快照标记为 legacy-unverified / legacy_unverified，不被冒充为已核验
        row = conn.execute(
            "SELECT attempt_id, raw_format, validation_status, raw_bytes, http_status "
            "FROM raw_snapshot WHERE snapshot_id='snap-legacy'"
        ).fetchone()
        assert row["raw_format"] == "legacy-unverified"
        assert row["validation_status"] == "legacy_unverified"
        assert row["attempt_id"] is None
        assert row["raw_bytes"] is None and row["http_status"] is None
        # 旧观察行 row_index/entry_time 保持 NULL，不编造行定位
        obs = conn.execute(
            "SELECT row_index, entry_time, normalization_diagnostics "
            "FROM statement_observation WHERE observation_id='obs-legacy'"
        ).fetchone()
        assert obs["row_index"] is None and obs["entry_time"] is None
        assert obs["normalization_diagnostics"] is None
        # 外键一致性
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.close()

    def test_new_tables_columns_and_indexes_present(self, tmp_path):
        conn = make_v3_db(tmp_path / "legacy.db")
        run_migrations(conn)

        for table in ("ingest_unit", "ingest_attempt"):
            assert m._table_exists(conn, table)
        unit_cols = {r[1] for r in conn.execute("PRAGMA table_info(ingest_unit)")}
        assert {"unit_id", "run_id", "unit_key", "unit_kind", "request_scope",
                "status", "source_rows", "selected_rows", "new_versions",
                "new_observations", "compat_writes"} <= unit_cols
        attempt_cols = {r[1] for r in conn.execute("PRAGMA table_info(ingest_attempt)")}
        assert {"attempt_id", "unit_id", "page_number", "http_attempt",
                "started_at", "outcome"} <= attempt_cols
        for index in m.V4_REQUIRED_INDEXES:
            assert m._index_exists(conn, index), index
        conn.close()

    def test_empty_db_migrates_in_order(self, tmp_path):
        conn = get_connection(str(tmp_path / "fresh.db"))
        assert m._get_current_version(conn) == 0
        # v4 对象不在 v1 基线里：空库早期迁移不提前创建 2B 结构
        baseline = " ".join(m.ALL_DDL)
        assert "CREATE TABLE IF NOT EXISTS ingest_unit" not in baseline
        assert "CREATE TABLE IF NOT EXISTS ingest_attempt" not in baseline
        assert "attempt_id" not in baseline
        assert "row_index" not in baseline

        run_migrations(conn)

        versions = [r[0] for r in conn.execute(
            "SELECT version FROM schema_version ORDER BY version"
        )]
        assert versions == [1, 2, 3, 4]
        assert collect_v4_structure_gaps(conn) == []
        conn.close()

    def test_rerun_is_idempotent(self, tmp_path):
        conn = make_v3_db(tmp_path / "legacy.db")
        run_migrations(conn)
        cols_after_first = {r[1] for r in conn.execute("PRAGMA table_info(raw_snapshot)")}
        run_migrations(conn)

        assert {r[1] for r in conn.execute("PRAGMA table_info(raw_snapshot)")} \
            == cols_after_first
        duplicates = conn.execute(
            "SELECT version, COUNT(*) FROM schema_version GROUP BY version HAVING COUNT(*) > 1"
        ).fetchall()
        assert duplicates == []
        assert collect_v4_structure_gaps(conn) == []
        conn.close()


class TestB10MigrationFailureRollback:
    """B10：v4 建表/加列/索引后注入失败 → 全部回滚，外层事务未提交，重试等价。"""

    def test_failure_rolls_back_everything(self, tmp_path, monkeypatch):
        conn = make_v3_db(tmp_path / "legacy.db")
        from fin_feature.storage import schema as schema_module

        real_indexes = list(schema_module.V4_INDEXES)

        class ExplodingIndexes(list):
            def __iter__(self):
                for idx in real_indexes:
                    conn.execute(idx)
                raise RuntimeError("注入失败：索引建好后、登记版本前")

        monkeypatch.setattr(schema_module, "V4_INDEXES", ExplodingIndexes(real_indexes))

        with pytest.raises(RuntimeError, match="迁移 v4"):
            run_migrations(conn)

        # DDL 全部回滚：新表不存在、新列不存在、版本未登记
        assert not m._table_exists(conn, "ingest_unit")
        assert not m._table_exists(conn, "ingest_attempt")
        assert "attempt_id" not in {
            r[1] for r in conn.execute("PRAGMA table_info(raw_snapshot)")
        }
        assert m._get_current_version(conn) == 3
        # 既有历史数据完好
        assert conn.execute("SELECT COUNT(*) FROM raw_snapshot").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM statement_version").fetchone()[0] == 1
        conn.close()

    def test_outer_transaction_not_committed(self, tmp_path, monkeypatch):
        """外层事务中迁移失败：外层数据不提交，且 v4 DDL 不残留。"""
        conn = make_v3_db(tmp_path / "legacy.db")
        from fin_feature.storage import schema as schema_module

        real_indexes = list(schema_module.V4_INDEXES)

        class ExplodingIndexes(list):
            def __iter__(self):
                for idx in real_indexes:
                    conn.execute(idx)
                raise RuntimeError("注入失败")

        monkeypatch.setattr(schema_module, "V4_INDEXES", ExplodingIndexes(real_indexes))

        conn.execute("BEGIN")
        conn.execute(
            "INSERT INTO raw_financial_indicator(secucode, report_date, crawled_at) "
            "VALUES ('600000.SH','2024-06-30','2024-07-01 10:00:00')"
        )
        with pytest.raises(RuntimeError, match="迁移 v4"):
            run_migrations(conn)

        assert not m._table_exists(conn, "ingest_unit")
        assert m._get_current_version(conn) == 3
        # savepoint 回滚只撤销迁移自身的改动：外层未提交的行仍在事务内可见
        assert conn.in_transaction
        assert conn.execute("SELECT COUNT(*) FROM raw_financial_indicator").fetchone()[0] == 2
        conn.rollback()  # 外层自行决定回滚
        conn.close()

        # 另开连接确认外层行与 v4 结构都没有落盘
        check = get_connection(str(tmp_path / "legacy.db"))
        assert check.execute("SELECT COUNT(*) FROM raw_financial_indicator").fetchone()[0] == 1
        assert not m._table_exists(check, "ingest_unit")
        assert m._get_current_version(check) == 3
        check.close()

    def test_retry_equals_single_success(self, tmp_path, monkeypatch):
        conn = make_v3_db(tmp_path / "retry.db")
        from fin_feature.storage import schema as schema_module

        real_indexes = list(schema_module.V4_INDEXES)

        class ExplodingIndexes(list):
            def __iter__(self):
                for idx in real_indexes:
                    conn.execute(idx)
                raise RuntimeError("注入失败")

        monkeypatch.setattr(schema_module, "V4_INDEXES", ExplodingIndexes(real_indexes))
        with pytest.raises(RuntimeError):
            run_migrations(conn)
        monkeypatch.undo()

        run_migrations(conn)

        fresh = make_v3_db(tmp_path / "fresh.db")
        run_migrations(fresh)

        def shape(c):
            return (
                sorted(r[0] for r in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")),
                sorted(r[1] for r in c.execute("PRAGMA table_info(raw_snapshot)")),
                sorted(r[1] for r in c.execute("PRAGMA table_info(statement_observation)")),
                sorted(r[0] for r in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'")),
                [r[0] for r in c.execute("SELECT version FROM schema_version ORDER BY version")],
            )

        assert shape(conn) == shape(fresh)
        conn.close()
        fresh.close()


class TestB11Backup:
    """B11：WAL 已提交数据的备份；失败不迁移；备份不覆盖原文件。"""

    def test_backup_contains_committed_wal_rows(self, tmp_path):
        db_path = tmp_path / "wal.db"
        conn = make_v3_db(db_path)
        # WAL 中的已提交数据（尚未 checkpoint）
        conn.execute(
            "INSERT INTO raw_financial_indicator(secucode, report_date, crawled_at) "
            "VALUES ('600519.SH','2024-03-31','2024-04-20 10:00:00')"
        )
        conn.commit()
        assert (tmp_path / "wal.db-wal").exists()

        target = tmp_path / "backup.db"
        migrate_with_backup(conn, db_path, backup_path=target)

        assert target.exists()
        copy = get_connection(str(target))
        assert copy.execute("SELECT COUNT(*) FROM raw_financial_indicator").fetchone()[0] == 2
        assert copy.execute(
            "SELECT COUNT(*) FROM raw_financial_indicator WHERE secucode='600519.SH'"
        ).fetchone()[0] == 1
        assert m._get_current_version(copy) == 3  # 副本是升级前的快照
        copy.close()
        # 原库已升级
        assert m._get_current_version(conn) == LATEST
        conn.close()

    def test_backup_failure_stops_migration(self, tmp_path):
        db_path = tmp_path / "wal.db"
        conn = make_v3_db(db_path)
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")

        with pytest.raises(OSError):
            migrate_with_backup(conn, db_path, backup_path=blocker / "backup.db")

        assert m._get_current_version(conn) == 3
        assert not m._table_exists(conn, "ingest_unit")
        conn.close()

    def test_existing_backup_target_refused(self, tmp_path):
        db_path = tmp_path / "wal.db"
        conn = make_v3_db(db_path)
        target = tmp_path / "keep.db"
        target.write_bytes(b"existing backup, do not touch")

        with pytest.raises(RuntimeError, match="拒绝覆盖"):
            backup_database_for_migration(conn, target)

        assert target.read_bytes() == b"existing backup, do not touch"
        assert m._get_current_version(conn) == 3
        assert not m._table_exists(conn, "ingest_unit")
        conn.close()

    def test_default_backup_path_is_unique_and_new(self, tmp_path):
        db_path = tmp_path / "wal.db"
        db_path.write_bytes(b"")
        first = default_backup_path(db_path)
        assert not first.exists()
        first.write_bytes(b"x")
        second = default_backup_path(db_path)
        assert second != first and not second.exists()
        assert ".backup-" in second.name

    def test_empty_database_needs_no_backup(self, tmp_path):
        conn = get_connection(str(tmp_path / "fresh.db"))
        assert migrate_with_backup(conn, tmp_path / "fresh.db") is None
        assert collect_v4_structure_gaps(conn) == []
        conn.close()


class TestB12IncompleteV4Rejected:
    """B12：自称 v4 但缺表/列/索引的库必须明确拒绝。"""

    @pytest.mark.parametrize("damage", ["table", "column", "index"])
    def test_incomplete_v4_rejected(self, tmp_path, damage):
        conn = get_connection(str(tmp_path / "v4.db"))
        run_migrations(conn)
        assert collect_v4_structure_gaps(conn) == []

        if damage == "table":
            conn.execute("DROP TABLE ingest_attempt")
        elif damage == "column":
            conn.execute("ALTER TABLE raw_snapshot DROP COLUMN content_type")
        else:
            conn.execute("DROP INDEX uq_observation_source_row")
        conn.commit()

        gaps = collect_v4_structure_gaps(conn)
        assert gaps, damage
        with pytest.raises(RuntimeError, match="结构不完整"):
            verify_v4_structure(conn)
        with pytest.raises(RuntimeError, match="结构不完整"):
            run_migrations(conn)
        conn.close()

    def test_version_number_alone_is_not_enough(self, tmp_path):
        """只有 schema_version=4、没有任何 v4 结构的库 → 拒绝。"""
        conn = get_connection(str(tmp_path / "fake.db"))
        conn.execute(
            "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, "
            "name TEXT NOT NULL, applied_at TEXT)"
        )
        conn.execute("INSERT INTO schema_version (version, name) VALUES (4, 'v4_ingestion_evidence')")
        conn.commit()

        with pytest.raises(RuntimeError, match="结构不完整"):
            run_migrations(conn)
        conn.close()

    def test_gap_report_is_read_only(self, tmp_path):
        conn = get_connection(str(tmp_path / "v4.db"))
        run_migrations(conn)
        conn.execute("DROP INDEX uq_raw_snapshot_attempt")
        conn.commit()
        before = sorted(r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"))

        collect_v4_structure_gaps(conn)
        collect_v4_structure_gaps(conn)

        after = sorted(r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"))
        assert before == after
        conn.close()
