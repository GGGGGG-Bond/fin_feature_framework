"""2B-1 验收矩阵 B01-B08：原始证据留存与校验。

对应 docs/stage-2/2b-test-matrix.md「2B-1：证据、模型与失败边界」。
所有用例使用真实临时文件与真实 gzip 字节，不使用假路径。
"""

import errno
import gzip
import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from fin_feature.ingestion.evidence import compute_unit_key
from fin_feature.storage import raw_store as rs
from fin_feature.storage.raw_store import (
    PublishContext,
    RawStore,
    RawStoreConflict,
    RawStoreError,
    RawStorePathError,
    RawStoreVerifyError,
    compute_raw_hash,
)
from fin_feature.storage.repository import Repository

UNIT_KEY_A = "a" * 64
UNIT_KEY_B = "b" * 64


def ctx(**kw) -> PublishContext:
    base = dict(
        run_id="11111111-2222-3333-4444-555555555555",
        unit_key=UNIT_KEY_A,
        unit_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        page_number=1,
        http_attempt=1,
        source_name="eastmoney",
        fetched_at="2026-10-16T08:00:00Z",
        http_status=200,
        content_type="application/json",
    )
    base.update(kw)
    return PublishContext(**base)


class TestB01BodyIdentity:
    """B01：留存的是响应体字节本身，不是解析后的 JSON。"""

    def test_byte_roundtrip_and_hash_of_bytes(self, tmp_path):
        store = RawStore(tmp_path / "raw")
        bodies = [
            b'{"a":1}',
            b'{ "a" : 1 }',                       # 空格差异
            '{"a":"中文"}'.encode("utf-8"),        # 编码差异
            '<html><body>500</body></html>'.encode("utf-8"),  # HTML 错误页
            b"not json at all",                    # 非法 JSON
            b"",                                   # 空 body
        ]
        for i, body in enumerate(bodies, start=1):
            stored = store.publish(ctx(page_number=i), body)
            verified = store.verify(stored)
            assert verified.body == body
            assert verified.raw_bytes == len(body)
            assert stored.raw_hash == hashlib.sha256(body).hexdigest()
            assert verified.raw_hash == compute_raw_hash(body)

    def test_whitespace_variant_has_different_raw_hash(self, tmp_path):
        store = RawStore(tmp_path / "raw")
        compact = b'{"a":1}'
        spaced = b'{ "a" : 1 }'
        s1 = store.publish(ctx(), compact)
        s2 = store.publish(ctx(page_number=2), spaced)
        assert s1.raw_hash != s2.raw_hash
        # 但落盘摘要不是"重序列化 JSON"的哈希：去除空格后哈希必然不同
        reparsed = hashlib.sha256(
            json.dumps(json.loads(spaced), separators=(",", ":")).encode()
        ).hexdigest()
        assert s2.raw_hash == hashlib.sha256(spaced).hexdigest()
        assert s2.raw_hash != reparsed
        assert json.loads(spaced) == json.loads(compact)  # 语义相同，摘要不同

    def test_gzip_file_decompresses_byte_identical(self, tmp_path):
        store = RawStore(tmp_path / "raw")
        body = b'{"result":{"data":[{"x":1}]}}'
        stored = store.publish(ctx(), body)
        raw_file = store.root / stored.file_path
        with gzip.open(raw_file, "rb") as fh:
            assert fh.read() == body


class TestB02ReuseSameIdentity:
    """B02：同 run/unit/页/尝试、相同 body 再发布 → 复用，不改写捕获信息。"""

    def test_republish_reuses_file(self, tmp_path):
        store = RawStore(tmp_path / "raw")
        body = b'{"a":1}'
        first = store.publish(ctx(), body)
        target = store.root / first.file_path
        before_stat = target.stat()
        before_bytes = target.read_bytes()

        second = store.publish(ctx(), body)
        assert second.reused is True
        assert second.file_path == first.file_path
        assert second.raw_hash == first.raw_hash
        after_stat = target.stat()
        assert target.read_bytes() == before_bytes
        assert after_stat.st_mtime_ns == before_stat.st_mtime_ns

    def test_snapshot_reused_for_same_attempt(self, evidence_env):
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        captured = env.capture(unit, rows=[{"TOTAL_OPERATE_INCOME": 100}])
        # 同一 attempt 重登记（attempt 仍为 running 时直接复用既有快照）
        again = env.repo.save_raw_snapshot(
            unit["run_id"], env.source_name, unit["report_name"], unit["secucode"], 1,
            "{}", captured.raw_hash, captured.file_path, 1,
            attempt_id=captured.attempt_id, http_status=200,
            raw_bytes=captured.raw_bytes, validation_status="pending",
            fetched_at="2026-10-16T08:00:00Z",
        )
        assert again == captured.snapshot_id
        assert env.conn.execute("SELECT COUNT(*) FROM raw_snapshot").fetchone()[0] == 1


class TestB03ConflictNoOverwrite:
    """B03：同身份不同 body、目标损坏、并发竞争 → 冲突，绝不覆盖。"""

    def test_different_body_conflicts_and_keeps_original(self, tmp_path):
        store = RawStore(tmp_path / "raw")
        first = store.publish(ctx(), b'{"v":1}')
        target = store.root / first.file_path
        original = target.read_bytes()

        with pytest.raises(RawStoreConflict):
            store.publish(ctx(), b'{"v":2}')

        assert target.read_bytes() == original
        # 既有证据与目录内容不被替换，也不留下新的临时文件
        assert list(target.parent.iterdir()) == [target]

    def test_race_conflict_keeps_incoming_temp_file(self, tmp_path):
        """并发竞争路径：临时文件已写出，冲突时不自动删除，供只读对账识别。"""
        store = RawStore(tmp_path / "raw")
        rel = store.path_for(ctx())
        target = store.root / rel
        real_link = os.link

        def lose_race(src, dst):
            # 模拟"检查不存在后、link 之前"被并发发布抢先创建
            Path(dst).write_bytes(gzip.compress(b'{"other":true}', mtime=0))
            raise FileExistsError(errno.EEXIST, "file exists", str(dst))

        os.link = lose_race
        try:
            with pytest.raises(RawStoreConflict) as excinfo:
                store.publish(ctx(), b'{"v":1}')
        finally:
            os.link = real_link

        # 先创建者生效，本方内容保留在临时文件，不自动删除
        with gzip.open(target, "rb") as fh:
            assert fh.read() == b'{"other":true}'
        temp_path = excinfo.value.temp_path
        assert temp_path is not None and temp_path.exists()
        assert RawStore.is_temp_path(temp_path.relative_to(store.root).as_posix())
        with gzip.open(temp_path, "rb") as fh:
            assert fh.read() == b'{"v":1}'

    def test_corrupted_target_conflicts(self, tmp_path):
        store = RawStore(tmp_path / "raw")
        body = b'{"v":1}'
        first = store.publish(ctx(), body)
        target = store.root / first.file_path
        target.write_bytes(b"corrupted")

        with pytest.raises(RawStoreConflict):
            store.publish(ctx(), body)
        assert target.read_bytes() == b"corrupted"

    def test_preexisting_target_from_concurrent_publish(self, tmp_path):
        """并发：目标已被他人以不同内容创建（模拟 link 竞争失败后的路径）。"""
        store = RawStore(tmp_path / "raw")
        rel = store.path_for(ctx())
        target = store.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(gzip.compress(b'{"other":true}', mtime=0))

        with pytest.raises(RawStoreConflict):
            store.publish(ctx(), b'{"v":1}')
        with gzip.open(target, "rb") as fh:
            assert fh.read() == b'{"other":true}'

    def test_no_overwrite_fallback_when_link_unsupported(self, tmp_path, monkeypatch):
        store = RawStore(tmp_path / "raw")

        def fake_link(src, dst):
            raise OSError(errno.EPERM, "operation not permitted")

        monkeypatch.setattr(os, "link", fake_link)
        with pytest.raises(RawStoreError, match="拒绝回退为覆盖写"):
            store.publish(ctx(), b'{"v":1}')
        assert not (store.root / store.path_for(ctx())).exists()


class TestB04IdentitySeparation:
    """B04：同内容不同 run/公司/页/尝试 → 身份与路径分离，不做全局去重。"""

    def test_same_body_different_identity_separate_paths(self, tmp_path):
        store = RawStore(tmp_path / "raw")
        body = b'{"same":1}'
        paths = {
            store.publish(ctx(run_id="run-a"), body).file_path,
            store.publish(ctx(run_id="run-b"), body).file_path,
            store.publish(ctx(page_number=2), body).file_path,
            store.publish(ctx(http_attempt=2), body).file_path,
            store.publish(ctx(unit_key=UNIT_KEY_B), body).file_path,
        }
        assert len(paths) == 5
        for rel in paths:
            assert store.verify(rel).body == body

    def test_path_layout_is_contract_shape(self, tmp_path):
        store = RawStore(tmp_path / "raw")
        rel = store.path_for(ctx(page_number=7, http_attempt=3))
        run_id = "11111111-2222-3333-4444-555555555555"
        unit_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        assert rel == f"eastmoney/{run_id}/aa/{unit_id}/p000007/a000003.body.gz"

    def test_page_beyond_six_digits_not_truncated(self, tmp_path):
        store = RawStore(tmp_path / "raw")
        rel = store.path_for(ctx(page_number=1234567))
        assert "/p1234567/" in rel


class TestB05MissingOrTamperedEvidence:
    """B05：缺文件/错哈希/错大小/截断 gzip → 拒绝保存或拒绝声明充分。"""

    def test_missing_file_rejected_without_business_rows(self, evidence_env):
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        captured = env.capture(unit, rows=[{"TOTAL_OPERATE_INCOME": 100}])
        (env.store.root / captured.file_path).unlink()

        with pytest.raises(RawStoreVerifyError):
            env.repo.save_raw_snapshot(
                unit["run_id"], env.source_name, unit["report_name"], unit["secucode"], 2,
                "{}", captured.raw_hash, captured.file_path, 1,
                attempt_id=env.repo.register_attempt(unit["unit_id"], 2, 1),
                http_status=200, raw_bytes=captured.raw_bytes,
                fetched_at="2026-10-16T08:00:00Z",
            )
        assert env.conn.execute("SELECT COUNT(*) FROM raw_snapshot").fetchone()[0] == 1
        assert env.conn.execute("SELECT COUNT(*) FROM statement_version").fetchone()[0] == 0

    def test_wrong_hash_rejected(self, evidence_env):
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        captured = env.capture(unit, rows=[{"TOTAL_OPERATE_INCOME": 100}])
        attempt = env.repo.register_attempt(unit["unit_id"], 2, 1)
        with pytest.raises(RawStoreVerifyError, match="哈希不一致"):
            env.repo.save_raw_snapshot(
                unit["run_id"], env.source_name, unit["report_name"], unit["secucode"], 2,
                "{}", "0" * 64, captured.file_path, 1,
                attempt_id=attempt, http_status=200, raw_bytes=captured.raw_bytes,
                fetched_at="2026-10-16T08:00:00Z",
            )

    def test_wrong_size_rejected(self, evidence_env):
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        captured = env.capture(unit, rows=[{"TOTAL_OPERATE_INCOME": 100}])
        attempt = env.repo.register_attempt(unit["unit_id"], 2, 1)
        with pytest.raises(RawStoreVerifyError, match="大小不一致"):
            env.repo.save_raw_snapshot(
                unit["run_id"], env.source_name, unit["report_name"], unit["secucode"], 2,
                "{}", captured.raw_hash, captured.file_path, 1,
                attempt_id=attempt, http_status=200,
                raw_bytes=captured.raw_bytes + 5,
                fetched_at="2026-10-16T08:00:00Z",
            )

    def test_truncated_gzip_rejected(self, tmp_path):
        store = RawStore(tmp_path / "raw")
        stored = store.publish(ctx(), b'{"a":1}' * 100)
        target = store.root / stored.file_path
        data = target.read_bytes()
        target.write_bytes(data[: len(data) // 2])

        with pytest.raises(RawStoreVerifyError):
            store.verify(stored)

    def test_legacy_reference_cannot_claim_sufficient(self, evidence_env):
        """B05/B46：legacy-unverified 引用不能让版本声明 evidence_sufficient=1。"""
        env = evidence_env
        run = env.new_run()
        snap = env.repo.save_raw_snapshot(
            run, "eastmoney", "RPT_LICO_FN_CPD", "000001.SZ", 1,
            "{}", "a" * 64, "/tmp/legacy.json.gz", 1,
        )
        with pytest.raises(ValueError, match="legacy-unverified"):
            env.repo.create_statement_version(
                secucode="000001.SZ", report_date="2024-03-31",
                table_type="financial_indicator", snapshot_id=snap, run_id=run,
                standardized_fields=json.dumps({"total_operate_income": 100}),
                evidence_sufficient=1,
            )
        assert env.conn.execute("SELECT COUNT(*) FROM statement_version").fetchone()[0] == 0

    def test_wrong_size_rejected_at_declaration_time(self, evidence_env):
        """B05：文件在提交后被改动 → 后续充分性声明被拒绝。"""
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        captured = env.capture(unit, rows=[{"TOTAL_OPERATE_INCOME": 100}])
        target = env.store.root / captured.file_path
        target.write_bytes(gzip.compress(b'{"tampered":1}', mtime=0))

        with pytest.raises(RawStoreVerifyError):
            env.create_version(unit, captured)
        assert env.conn.execute("SELECT COUNT(*) FROM statement_version").fetchone()[0] == 0


class TestB06PathContainment:
    """B06：../、绝对路径、symlink 越根、非法路径段 → 拒绝，根外文件不被触碰。"""

    def test_reject_dotdot_and_absolute(self, tmp_path):
        store = RawStore(tmp_path / "raw")
        with pytest.raises(RawStorePathError):
            store.resolve_path("../outside.json.gz")
        with pytest.raises(RawStorePathError):
            store.resolve_path("/etc/passwd")
        with pytest.raises(RawStorePathError):
            store.resolve_path("eastmoney/../../outside.json.gz")

    def test_reject_invalid_segments(self, tmp_path):
        store = RawStore(tmp_path / "raw")
        with pytest.raises(RawStorePathError):
            store.path_for(ctx(run_id="../evil"))
        with pytest.raises(RawStorePathError):
            store.path_for(ctx(unit_key="not-hex"))
        with pytest.raises(RawStorePathError):
            store.path_for(ctx(source_name="../../etc"))
        with pytest.raises(RawStorePathError):
            store.path_for(ctx(page_number=0))
        with pytest.raises(RawStorePathError):
            store.path_for(ctx(http_attempt=True))

    def test_symlink_escape_rejected_and_outside_untouched(self, tmp_path):
        root = tmp_path / "raw"
        outside = tmp_path / "outside"
        outside.mkdir()
        store = RawStore(root)
        (root / "escape").symlink_to(outside, target_is_directory=True)
        victim = outside / "secret.txt"
        victim.write_text("keep me", encoding="utf-8")

        with pytest.raises(RawStorePathError, match="symlink 越根"):
            store.resolve_path("escape/secret.txt")
        assert victim.read_text(encoding="utf-8") == "keep me"

    def test_symlink_target_rejected_at_publish(self, tmp_path):
        root = tmp_path / "raw"
        outside = tmp_path / "outside.gz"
        outside.write_bytes(gzip.compress(b"original", mtime=0))
        store = RawStore(root)
        rel = store.path_for(ctx())
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(outside)

        with pytest.raises((RawStoreConflict, RawStorePathError)):
            store.publish(ctx(), b'{"v":1}')
        with gzip.open(outside, "rb") as fh:
            assert fh.read() == b"original"


class TestB07FailureInjection:
    """B07：临时写入、fsync、发布、DB 插入各点故障：无假成功、证据与孤儿可识别。"""

    def test_fsync_failure_leaves_no_target_and_keeps_temp(self, tmp_path, monkeypatch):
        store = RawStore(tmp_path / "raw")
        real_fsync = os.fsync
        calls = {"n": 0}

        def flaky_fsync(fd):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError(errno.EIO, "fsync failed")
            return real_fsync(fd)

        monkeypatch.setattr(os, "fsync", flaky_fsync)
        with pytest.raises(OSError):
            store.publish(ctx(), b'{"v":1}')

        target = store.root / store.path_for(ctx())
        assert not target.exists()
        temps = [p for p in store.root.rglob("*.tmp")]
        assert temps, "失败时保留临时文件供对账识别，不自动删除"

    def test_db_insert_failure_leaves_orphan_file(self, evidence_env):
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        attempt = env.repo.register_attempt(unit["unit_id"], 1, 1)
        stored = env.store.publish(
            PublishContext(
                run_id=unit["run_id"], unit_key=unit["unit_key"], unit_id=unit["unit_id"],
                page_number=1, http_attempt=1, source_name=env.source_name,
                fetched_at="2026-10-16T08:00:00Z", http_status=200,
            ),
            b'{"a":1}',
        )

        class FailingConn:
            """代理连接：任何写入尝试都失败，模拟 snapshot 插入时的 I/O 故障。"""

            def __init__(self, conn):
                self._conn = conn

            def __getattr__(self, name):
                return getattr(self._conn, name)

            def execute(self, *a, **kw):
                raise sqlite3.OperationalError("disk I/O error")

        env.repo.conn = FailingConn(env.conn)
        with pytest.raises(sqlite3.OperationalError):
            env.repo.save_raw_snapshot(
                unit["run_id"], env.source_name, unit["report_name"], unit["secucode"], 1,
                "{}", stored.raw_hash, stored.file_path, 1, attempt_id=attempt,
                http_status=200, raw_bytes=stored.raw_bytes,
                fetched_at="2026-10-16T08:00:00Z",
            )

        assert env.conn.execute("SELECT COUNT(*) FROM raw_snapshot").fetchone()[0] == 0
        assert (env.store.root / stored.file_path).exists()

        from fin_feature.storage.raw_audit import audit_raw

        report = audit_raw(env.conn, env.store)
        codes = {f.code for f in report.findings}
        assert "orphan_file" in codes
        assert report.exit_code() == 1

    def test_business_transaction_rollback_keeps_evidence(self, evidence_env):
        """业务 UOW 回滚：版本/观察不残留，文件与 attempt 保留。"""
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        captured = env.capture(unit, rows=[{"TOTAL_OPERATE_INCOME": 100}])

        with pytest.raises(RuntimeError):
            with env.repo.unit_of_work():
                env.create_version(unit, captured)
                raise RuntimeError("business failure")

        assert env.conn.execute("SELECT COUNT(*) FROM statement_version").fetchone()[0] == 0
        assert env.conn.execute("SELECT COUNT(*) FROM statement_observation").fetchone()[0] == 0
        assert (env.store.root / captured.file_path).exists()
        assert env.repo.get_attempt(captured.attempt_id) is not None


class TestB08SizeBounds:
    """B08：体积超限、超限解压、gzip 损坏 → 有界失败。"""

    def test_publish_over_limit_rejected(self, tmp_path):
        store = RawStore(tmp_path / "raw", max_body_bytes=16)
        with pytest.raises(RawStoreError, match="max_body_bytes"):
            store.publish(ctx(), b"x" * 17)
        assert not (store.root / store.path_for(ctx())).exists()

    def test_verify_over_limit_rejected(self, tmp_path):
        big = RawStore(tmp_path / "raw")
        stored = big.publish(ctx(), b"y" * 100)
        small = RawStore(tmp_path / "raw", max_body_bytes=10)
        with pytest.raises(RawStoreVerifyError, match="max_body_bytes"):
            small.verify(stored)

    def test_corrupt_gzip_rejected(self, tmp_path):
        store = RawStore(tmp_path / "raw")
        rel = store.path_for(ctx())
        target = store.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x1f\x8b\x08\x00garbage-not-gzip")

        with pytest.raises(RawStoreVerifyError):
            store.verify(rel)

    def test_invalid_max_body_bytes_config(self, tmp_path):
        with pytest.raises(RawStoreError):
            RawStore(tmp_path / "raw", max_body_bytes=0)
        with pytest.raises(RawStoreError):
            RawStore(tmp_path / "raw", max_body_bytes=True)


class TestRawHashPrimitive:
    """compute_raw_hash 只接受 bytes，不做任何规范化。"""

    def test_rejects_non_bytes(self):
        with pytest.raises(RawStoreError):
            compute_raw_hash("text")  # type: ignore[arg-type]

    def test_stable_across_calls(self):
        assert compute_raw_hash(b"abc") == compute_raw_hash(b"abc")
        assert len(compute_raw_hash(b"abc")) == 64


class TestRawRootResolution:
    """raw_root 解析：默认 DB 目录/raw，可显式覆盖，相对路径按 DB 目录解释。"""

    def test_default_and_overrides(self, tmp_path):
        from fin_feature.storage.raw_store import resolve_raw_root

        db_path = tmp_path / "data" / "finance.db"
        assert resolve_raw_root(None, str(db_path)) == (tmp_path / "data" / "raw")

        class Cfg:
            def __init__(self, root):
                self._root = root

            def get(self, *keys, default=None):
                if keys[:2] == ("raw_store", "root"):
                    return self._root
                if keys[:2] == ("raw_store", "max_body_bytes"):
                    return 1024
                return default

        assert resolve_raw_root(Cfg("evidence"), str(db_path)) == \
            (tmp_path / "data" / "evidence")
        absolute = tmp_path / "absolute"
        assert resolve_raw_root(Cfg(str(absolute)), str(db_path)) == absolute.resolve()


class TestUnitKeyIdentity:
    """unit_key：范围参数变化分属不同单元，键序无关。"""

    def test_key_order_invariant(self):
        a = compute_unit_key(
            source_name="eastmoney", unit_kind="financial",
            report_name="RPT_LICO_FN_CPD", secucode="000001.SZ",
            request_scope={"page_size": 50, "recent_periods": 4},
        )
        b = compute_unit_key(
            source_name="eastmoney", unit_kind="financial",
            report_name="RPT_LICO_FN_CPD", secucode="000001.SZ",
            request_scope={"recent_periods": 4, "page_size": 50},
        )
        assert a == b

    def test_scope_change_changes_key(self):
        base = dict(source_name="eastmoney", unit_kind="financial",
                    report_name="RPT_LICO_FN_CPD", secucode="000001.SZ")
        keys = {
            compute_unit_key(request_scope={"page_size": 50}, **base),
            compute_unit_key(request_scope={"page_size": 100}, **base),
            compute_unit_key(request_scope={"recent_periods": 4}, **base),
            compute_unit_key(request_scope={"filters": {"A": 1}}, **base),
        }
        assert len(keys) == 4
