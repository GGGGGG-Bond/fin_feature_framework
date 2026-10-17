"""2B-1 验收矩阵 B13-B15：单元身份、跨表一致性与重复处理。

对应 docs/stage-2/2b-test-matrix.md「2B-1：证据、模型与失败边界」。
"""

import gzip
import json

import pytest

from fin_feature.ingestion.evidence import (
    CaptureConflict,
    compute_unit_key,
    resolve_max_pages_per_unit,
    UnitEvidence,
)
from fin_feature.storage.raw_store import PublishContext, RawStoreVerifyError

SOURCE = "eastmoney"


class TestB13UnitKeyAndPlanning:
    """B13：unit_key 稳定与区分；计划阶段不产生重复单元。"""

    def test_scope_parameters_never_share_a_unit(self, evidence_env):
        env = evidence_env
        env.new_run()
        base = dict(unit_kind="financial", report_name="RPT_LICO_FN_CPD",
                    secucode="000001.SZ")
        scopes = [
            {"page_size": 50},
            {"page_size": 100},                      # 页大小改变
            {"page_size": 50, "recent_periods": 8},  # 最近期数改变
            {"page_size": 50, "filters": {"A": 1}},  # 筛选改变
        ]
        keys = [compute_unit_key(source_name=SOURCE, request_scope=s, **base) for s in scopes]
        assert len(set(keys)) == 4

        # 来源/报表/公司改变也分属不同单元
        other = [
            compute_unit_key(source_name="another", request_scope=scopes[0], **base),
            compute_unit_key(source_name=SOURCE, request_scope=scopes[0],
                             unit_kind="financial", report_name="RPT_DMSK_FN_BALANCE",
                             secucode="000001.SZ"),
            compute_unit_key(source_name=SOURCE, request_scope=scopes[0],
                             unit_kind="financial", report_name="RPT_LICO_FN_CPD",
                             secucode="600000.SH"),
        ]
        assert len(set(keys) | set(other)) == 7

        unit_ids = {
            env.repo.create_ingest_unit(
                run_id=env.run_id, unit_key=key, source_name=SOURCE,
                unit_kind="financial", report_name="RPT_LICO_FN_CPD",
                secucode="000001.SZ",
                request_scope=json.dumps(scopes[0]),
            )
            for key in keys
        }
        assert len(unit_ids) == 4
        assert len(env.repo.get_units_for_run(env.run_id)) == 4

    def test_same_semantics_plans_one_unit(self, evidence_env):
        """同语义（键序不同的 scope）计划两次 → 同一 unit，不重复插入。"""
        env = evidence_env
        env.new_run()
        scope_a = {"page_size": 50, "recent_periods": 4}
        scope_b = {"recent_periods": 4, "page_size": 50}
        key = compute_unit_key(source_name=SOURCE, unit_kind="financial",
                               report_name="RPT_LICO_FN_CPD", secucode="000001.SZ",
                               request_scope=scope_a)
        assert key == compute_unit_key(source_name=SOURCE, unit_kind="financial",
                                       report_name="RPT_LICO_FN_CPD",
                                       secucode="000001.SZ", request_scope=scope_b)

        first = env.repo.create_ingest_unit(
            run_id=env.run_id, unit_key=key, source_name=SOURCE, unit_kind="financial",
            report_name="RPT_LICO_FN_CPD", secucode="000001.SZ",
            request_scope=json.dumps(scope_a),
        )
        second = env.repo.create_ingest_unit(
            run_id=env.run_id, unit_key=key, source_name=SOURCE, unit_kind="financial",
            report_name="RPT_LICO_FN_CPD", secucode="000001.SZ",
            request_scope=json.dumps(scope_b),
        )
        assert first == second
        assert env.conn.execute("SELECT COUNT(*) FROM ingest_unit").fetchone()[0] == 1
        assert env.repo.count_units(env.run_id) == {"planned": 1}

    def test_new_run_reuses_scope_but_is_a_new_unit(self, evidence_env):
        """正常重跑新建 run：同范围在新 run 里是独立单元，不复用旧 run 结论。"""
        env = evidence_env
        first_run = env.new_run()
        unit_a = env.new_unit(run_id=first_run)
        second_run = env.repo.create_ingest_run(SOURCE)
        unit_b = env.new_unit(run_id=second_run)

        assert unit_a["unit_key"] == unit_b["unit_key"]
        assert unit_a["unit_id"] != unit_b["unit_id"]
        assert env.conn.execute("SELECT COUNT(*) FROM ingest_unit").fetchone()[0] == 2

    def test_stock_list_unit_shape_enforced(self, evidence_env):
        env = evidence_env
        env.new_run()
        env.new_unit(unit_kind="stock_list")
        row = env.repo.get_units_for_run(env.run_id)[0]
        assert row["unit_kind"] == "stock_list"
        assert row["report_name"] == "stock_list"
        assert row["secucode"] == ""

        unit_key = compute_unit_key(
            source_name=SOURCE, unit_kind="financial", report_name="RPT_LICO_FN_CPD",
            secucode="000001.SZ", request_scope={"page_size": 50},
        )
        with pytest.raises(ValueError):
            env.repo.create_ingest_unit(
                run_id=env.run_id, unit_key=unit_key, source_name=SOURCE,
                unit_kind="financial", report_name="stock_list", secucode="000001.SZ",
                request_scope="{}",
            )
        with pytest.raises(ValueError):
            env.repo.create_ingest_unit(
                run_id=env.run_id, unit_key=unit_key, source_name=SOURCE,
                unit_kind="financial", report_name="RPT_LICO_FN_CPD", secucode="",
                request_scope="{}",
            )

    def test_unit_lifecycle_guards(self, evidence_env):
        """planned → running → 终态；终态不可就地重跑。"""
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        unit_id = unit["unit_id"]

        env.repo.mark_unit_running(unit_id)
        with pytest.raises(ValueError, match="不能重新开始"):
            env.repo.mark_unit_running(unit_id)
        env.repo.finish_unit(unit_id, "success_empty")
        assert env.repo.get_unit(unit_id)["status"] == "success_empty"

        with pytest.raises(ValueError, match="不能把成功单元改为 failed"):
            env.repo.finish_unit(unit_id, "failed", failure_code="E")
        # 已完成单元在新计划中直接复用，不重复占位
        assert env.repo.create_ingest_unit(
            run_id=unit["run_id"], unit_key=unit["unit_key"], source_name=SOURCE,
            unit_kind="financial", report_name=unit["report_name"],
            secucode=unit["secucode"], request_scope=unit["request_scope"],
        ) == unit_id

    def test_failed_unit_reentry_in_same_run_rejected(self, evidence_env):
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        env.repo.mark_unit_running(unit["unit_id"])
        env.repo.finish_unit(unit["unit_id"], "failed", failure_code="http_error")

        with pytest.raises(ValueError, match="重入被拒绝"):
            env.repo.create_ingest_unit(
                run_id=unit["run_id"], unit_key=unit["unit_key"], source_name=SOURCE,
                unit_kind="financial", report_name=unit["report_name"],
                secucode=unit["secucode"], request_scope=unit["request_scope"],
            )

    def test_max_pages_per_unit_guard(self):
        class Cfg:
            def __init__(self, value):
                self._value = value

            def get(self, *keys, default=None):
                return self._value if keys[:2] == ("crawl", "max_pages_per_unit") else default

        assert resolve_max_pages_per_unit(None) == 1000
        assert resolve_max_pages_per_unit(Cfg(25)) == 25
        for bad in (0, -1, True, "10", None):
            with pytest.raises(ValueError):
                resolve_max_pages_per_unit(Cfg(bad))


class TestB14CrossEntityConsistency:
    """B14：unit/run/source、attempt/unit、snapshot 各字段错配逐项拒绝。"""

    def test_unknown_run_and_source_mismatch_rejected(self, evidence_env):
        env = evidence_env
        env.new_run()
        key = compute_unit_key(source_name=SOURCE, unit_kind="financial",
                               report_name="RPT_LICO_FN_CPD", secucode="000001.SZ",
                               request_scope={"page_size": 50})
        with pytest.raises(ValueError, match="不存在于 ingest_run"):
            env.repo.create_ingest_unit(
                run_id="run-does-not-exist", unit_key=key, source_name=SOURCE,
                unit_kind="financial", report_name="RPT_LICO_FN_CPD",
                secucode="000001.SZ", request_scope="{}",
            )
        with pytest.raises(ValueError, match="来源不一致"):
            env.repo.create_ingest_unit(
                run_id=env.run_id, unit_key=key, source_name="other_source",
                unit_kind="financial", report_name="RPT_LICO_FN_CPD",
                secucode="000001.SZ", request_scope="{}",
            )
        assert env.conn.execute("SELECT COUNT(*) FROM ingest_unit").fetchone()[0] == 0

    def test_attempt_requires_existing_unit(self, evidence_env):
        env = evidence_env
        env.new_run()
        with pytest.raises(ValueError, match="不存在于 ingest_unit"):
            env.repo.register_attempt("no-such-unit", 1, 1)
        for bad in (0, -1, True, "1"):
            unit = env.new_unit(secucode=f"00000{bad if isinstance(bad, int) else 9}.SZ")
            with pytest.raises(ValueError, match="page_number"):
                env.repo.register_attempt(unit["unit_id"], bad, 1)

    def test_snapshot_identity_mismatches_rejected(self, evidence_env):
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        attempt = env.repo.register_attempt(unit["unit_id"], 1, 1)
        body = b'{"a":1}'
        stored = env.store.publish(
            PublishContext(run_id=unit["run_id"], unit_key=unit["unit_key"],
                           unit_id=unit["unit_id"], page_number=1, http_attempt=1,
                           source_name=SOURCE, fetched_at="2026-10-16T08:00:00Z",
                           http_status=200),
            body,
        )
        base = dict(
            run_id=unit["run_id"], source_name=SOURCE, report_name=unit["report_name"],
            secucode=unit["secucode"], page_number=1, request_params="{}",
            raw_content_hash=stored.raw_hash, file_path=stored.file_path,
            record_count=1, attempt_id=attempt, http_status=200,
            raw_bytes=stored.raw_bytes, fetched_at="2026-10-16T08:00:00Z",
        )

        cases = {
            "报表不一致": {"report_name": "RPT_DMSK_FN_BALANCE"},
            "公司不一致": {"secucode": "600000.SH"},
            "页号不一致": {"page_number": 2},
            "来源不一致": {"source_name": "other_source"},
        }
        for message, override in cases.items():
            with pytest.raises(ValueError, match=message):
                env.repo.save_raw_snapshot(**{**base, **override})
        assert env.conn.execute("SELECT COUNT(*) FROM raw_snapshot").fetchone()[0] == 0

        # 缺少 attempt_id 的 legacy 路径不能声明为 v4 证据
        with pytest.raises(ValueError, match="必须给出 attempt_id"):
            env.repo.save_raw_snapshot(
                **{**base, "attempt_id": None, "raw_format": "http-body-v1"}
            )
        # 非 100..599 状态码拒绝
        for bad_status in (0, 99, 600, True, "200"):
            with pytest.raises(ValueError, match="http_status"):
                env.repo.save_raw_snapshot(**{**base, "http_status": bad_status})
        # 不存在的 attempt 拒绝
        with pytest.raises(ValueError, match="不存在于 ingest_attempt"):
            env.repo.save_raw_snapshot(**{**base, "attempt_id": "no-such-attempt"})

    def test_snapshot_belongs_to_unit_run(self, evidence_env):
        """attempt 的单元属于另一个 run 时，snapshot 不能借用本 run 身份。"""
        env = evidence_env
        run_a = env.new_run()
        unit_a = env.new_unit(run_id=run_a)
        run_b = env.repo.create_ingest_run(SOURCE)
        unit_b = env.new_unit(run_id=run_b, secucode="600000.SH")
        attempt_b = env.repo.register_attempt(unit_b["unit_id"], 1, 1)

        stored = env.store.publish(
            PublishContext(run_id=run_b, unit_key=unit_b["unit_key"],
                           unit_id=unit_b["unit_id"], page_number=1, http_attempt=1,
                           source_name=SOURCE, fetched_at="2026-10-16T08:00:00Z",
                           http_status=200),
            b'{"a":1}',
        )
        with pytest.raises(ValueError, match="采集批次不一致"):
            env.repo.save_raw_snapshot(
                run_a, SOURCE, unit_a["report_name"], unit_a["secucode"], 1,
                "{}", stored.raw_hash, stored.file_path, 1, attempt_id=attempt_b,
                http_status=200, raw_bytes=stored.raw_bytes,
                fetched_at="2026-10-16T08:00:00Z",
            )
        assert env.conn.execute("SELECT COUNT(*) FROM raw_snapshot").fetchone()[0] == 0

    def test_run_id_exported_from_snapshot(self, evidence_env):
        """未显式给 run_id 时导出快照所属批次，不保存未知批次。"""
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        captured = env.capture(unit, rows=[{"TOTAL_OPERATE_INCOME": 100}])

        version_id = env.repo.create_statement_version(
            secucode=unit["secucode"], report_date="2024-03-31",
            table_type="financial_indicator", source_name=SOURCE,
            snapshot_id=captured.snapshot_id,
            standardized_fields=json.dumps({"total_operate_income": 100}),
            raw_fields=json.dumps(captured.rows[0], sort_keys=True),
            row_index=0, unit_evidence=env.evidence(unit["unit_id"], [captured]),
        )
        row = env.conn.execute(
            "SELECT run_id FROM statement_version WHERE version_id = ?", (version_id,)
        ).fetchone()
        assert row["run_id"] == unit["run_id"]
        obs = env.repo.get_observations_for_version(version_id)[0]
        assert obs["run_id"] == unit["run_id"]

    def test_version_run_mismatch_rejected(self, evidence_env):
        env = evidence_env
        run_a = env.new_run()
        unit = env.new_unit(run_id=run_a)
        captured = env.capture(unit, rows=[{"TOTAL_OPERATE_INCOME": 100}])
        run_b = env.repo.create_ingest_run(SOURCE)

        with pytest.raises(ValueError, match="采集批次不一致"):
            env.repo.create_statement_version(
                secucode=unit["secucode"], report_date="2024-03-31",
                table_type="financial_indicator", source_name=SOURCE,
                snapshot_id=captured.snapshot_id, run_id=run_b,
                standardized_fields=json.dumps({"total_operate_income": 100}),
                raw_fields=json.dumps(captured.rows[0], sort_keys=True),
                row_index=0, unit_evidence=env.evidence(unit["unit_id"], [captured]),
            )
        assert env.conn.execute("SELECT COUNT(*) FROM statement_version").fetchone()[0] == 0

    def test_unit_evidence_identity_checked(self, evidence_env):
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        other = env.new_unit(secucode="600000.SH")
        captured = env.capture(unit, rows=[{"TOTAL_OPERATE_INCOME": 100}])

        with pytest.raises(ValueError, match="单元公司不一致"):
            env.repo.create_statement_version(
                secucode=unit["secucode"], report_date="2024-03-31",
                table_type="financial_indicator", source_name=SOURCE,
                snapshot_id=captured.snapshot_id,
                standardized_fields=json.dumps({"total_operate_income": 100}),
                raw_fields=json.dumps(captured.rows[0], sort_keys=True),
                row_index=0, unit_evidence=env.evidence(other["unit_id"], [captured]),
            )
        with pytest.raises(ValueError, match="不存在"):
            env.repo.create_statement_version(
                secucode=unit["secucode"], report_date="2024-03-31",
                table_type="financial_indicator", source_name=SOURCE,
                snapshot_id=captured.snapshot_id,
                standardized_fields=json.dumps({"total_operate_income": 100}),
                raw_fields=json.dumps(captured.rows[0], sort_keys=True),
                row_index=0, unit_evidence=UnitEvidence(unit_id="ghost", complete=True),
            )
        # 不完整证明不能声明证据充分
        with pytest.raises(ValueError, match="不完整"):
            env.repo.create_statement_version(
                secucode=unit["secucode"], report_date="2024-03-31",
                table_type="financial_indicator", source_name=SOURCE,
                snapshot_id=captured.snapshot_id,
                standardized_fields=json.dumps({"total_operate_income": 100}),
                raw_fields=json.dumps(captured.rows[0], sort_keys=True),
                row_index=0, unit_evidence=env.evidence(unit["unit_id"], [captured],
                                                        complete=False),
            )
        assert env.conn.execute("SELECT COUNT(*) FROM statement_version").fetchone()[0] == 0


class TestB15ReplayAndConflict:
    """B15：同 attempt 重登记、同 row locator 重处理/矛盾输入。"""

    def test_same_attempt_reregistration(self, evidence_env):
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        first = env.repo.register_attempt(unit["unit_id"], 1, 1)
        assert env.repo.register_attempt(unit["unit_id"], 1, 1) == first
        assert env.conn.execute("SELECT COUNT(*) FROM ingest_attempt").fetchone()[0] == 1

        env.repo.finish_attempt(first, "ok")
        with pytest.raises(ValueError, match="拒绝重跑同一尝试"):
            env.repo.register_attempt(unit["unit_id"], 1, 1)

    def test_attempt_outcome_finalized_once(self, evidence_env):
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        attempt = env.repo.register_attempt(unit["unit_id"], 1, 1)
        env.repo.finish_attempt(attempt, "transport_error", error_code="timeout")
        with pytest.raises(ValueError, match="不接受重复结算"):
            env.repo.finish_attempt(attempt, "ok")
        row = env.repo.get_attempt(attempt)
        assert row["outcome"] == "transport_error"
        assert row["error_code"] == "timeout"

    def test_snapshot_replay_idempotent_conflict_otherwise(self, evidence_env):
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        captured = env.capture(unit, rows=[{"TOTAL_OPERATE_INCOME": 100}])
        kwargs = dict(
            run_id=unit["run_id"], source_name=SOURCE, report_name=unit["report_name"],
            secucode=unit["secucode"], page_number=1, request_params="{}",
            http_status=200, attempt_id=captured.attempt_id,
            validation_status="pending", fetched_at="2026-10-16T08:00:00Z",
        )
        same = env.repo.save_raw_snapshot(
            raw_content_hash=captured.raw_hash, file_path=captured.file_path,
            record_count=1, raw_bytes=captured.raw_bytes, **kwargs,
        )
        assert same == captured.snapshot_id
        assert env.conn.execute("SELECT COUNT(*) FROM raw_snapshot").fetchone()[0] == 1

        # 同 attempt 不同内容：文件校验先失败（不接受调用方自述摘要）
        with pytest.raises(RawStoreVerifyError):
            env.repo.save_raw_snapshot(
                raw_content_hash="0" * 64, file_path=captured.file_path,
                record_count=1, raw_bytes=captured.raw_bytes, **kwargs,
            )
        # 文件/哈希一致但核心字段不同：冲突，不更新原证据、不另插一行
        with pytest.raises(CaptureConflict):
            env.repo.save_raw_snapshot(
                raw_content_hash=captured.raw_hash, file_path=captured.file_path,
                record_count=1, raw_bytes=captured.raw_bytes,
                **{**kwargs, "request_params": '{"page_size": 100}'},
            )
        row = env.repo.get_snapshot(captured.snapshot_id)
        assert row["raw_content_hash"] == captured.raw_hash
        assert row["file_path"] == captured.file_path
        assert row["request_params"] == "{}"
        assert env.conn.execute("SELECT COUNT(*) FROM raw_snapshot").fetchone()[0] == 1

    def test_row_locator_replay_is_idempotent(self, evidence_env):
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        captured = env.capture(
            unit, rows=[{"TOTAL_OPERATE_INCOME": 100}, {"TOTAL_OPERATE_INCOME": 200}]
        )
        first = env.create_version(unit, captured, row_index=0,
                                   standardized={"total_operate_income": 100})
        replay = env.create_version(unit, captured, row_index=0,
                                    standardized={"total_operate_income": 100})
        assert replay == first
        assert env.conn.execute("SELECT COUNT(*) FROM statement_version").fetchone()[0] == 1
        assert env.conn.execute("SELECT COUNT(*) FROM statement_observation").fetchone()[0] == 1

    def test_contradictory_row_input_conflicts_without_partial_change(self, evidence_env):
        """同 (snapshot, row_index) 不同内容 → 冲突；当前投影与旧观察不被部分修改。"""
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        captured = env.capture(
            unit, rows=[{"TOTAL_OPERATE_INCOME": 100}, {"TOTAL_OPERATE_INCOME": 200}]
        )
        version_a = env.create_version(unit, captured, row_index=0,
                                       standardized={"total_operate_income": 100})
        version_b = env.create_version(unit, captured, row_index=1,
                                       standardized={"total_operate_income": 200})
        assert version_a != version_b

        def pointer(version_id):
            return env.conn.execute(
                "SELECT is_superseded, superseded_by FROM statement_version "
                "WHERE version_id = ?", (version_id,)
            ).fetchone()

        before_a, before_b = pointer(version_a), pointer(version_b)
        obs_before = [dict(r) for r in env.repo.get_observations_for_version(version_a)]

        with pytest.raises(CaptureConflict):
            env.create_version(unit, captured, row_index=0,
                               standardized={"total_operate_income": 999})

        assert pointer(version_a)["is_superseded"] == before_a["is_superseded"]
        assert pointer(version_a)["superseded_by"] == before_a["superseded_by"]
        assert pointer(version_b)["is_superseded"] == before_b["is_superseded"]
        assert pointer(version_b)["superseded_by"] == before_b["superseded_by"]
        assert [dict(r) for r in env.repo.get_observations_for_version(version_a)] == obs_before
        assert env.conn.execute("SELECT COUNT(*) FROM statement_version").fetchone()[0] == 2
        assert env.conn.execute("SELECT COUNT(*) FROM statement_observation").fetchone()[0] == 2

    def test_contradictory_metadata_conflicts(self, evidence_env):
        """同一来源行、同内容但公告元信息不同 → 冲突（不静默改写既有观察）。"""
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        captured = env.capture(unit, rows=[{"TOTAL_OPERATE_INCOME": 100}])
        env.create_version(unit, captured, row_index=0, notice_date="2024-04-15")

        with pytest.raises(CaptureConflict):
            env.create_version(unit, captured, row_index=0, notice_date="2024-04-20")
        obs = env.conn.execute(
            "SELECT notice_date FROM statement_observation WHERE snapshot_id = ? AND row_index = 0",
            (captured.snapshot_id,),
        ).fetchone()
        assert obs["notice_date"] == "2024-04-15"

    def test_different_rows_same_content_share_version(self, evidence_env):
        """两行原始值相同 → 复用同一内容版本，但各自保留来源行定位。"""
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        captured = env.capture(
            unit, rows=[{"TOTAL_OPERATE_INCOME": 100}, {"TOTAL_OPERATE_INCOME": 100}]
        )
        first = env.create_version(unit, captured, row_index=0,
                                   standardized={"total_operate_income": 100})
        second = env.create_version(unit, captured, row_index=1,
                                    standardized={"total_operate_income": 100})
        assert first == second
        observations = env.repo.get_observations_for_version(first)
        assert sorted(o["row_index"] for o in observations) == [0, 1]
        assert {o["snapshot_id"] for o in observations} == {captured.snapshot_id}

    def test_row_verifier_mismatch_rejected(self, evidence_env):
        """row_index 无法重建传入值时拒绝声明证据充分。"""
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        captured = env.capture(unit, rows=[{"TOTAL_OPERATE_INCOME": 100}])

        def strict_verifier(snapshot_id, row_index, standardized, raw_fields):
            rows = env.rows_by_snapshot[snapshot_id]
            if raw_fields != rows[row_index]:
                return False
            return standardized == {
                "total_operate_income": rows[row_index]["TOTAL_OPERATE_INCOME"]
            }

        with pytest.raises(ValueError, match="无法重建"):
            env.create_version(
                unit, captured, row_index=0,
                standardized={"total_operate_income": 424242},
                evidence=env.evidence(unit["unit_id"], [captured], verifier=strict_verifier),
            )
        assert env.conn.execute("SELECT COUNT(*) FROM statement_version").fetchone()[0] == 0
        assert env.conn.execute("SELECT COUNT(*) FROM statement_observation").fetchone()[0] == 0

    def test_set_snapshot_validation_one_way(self, evidence_env):
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        captured = env.capture(unit, rows=[], validation="invalid")
        assert env.repo.get_snapshot(captured.snapshot_id)["validation_status"] == "invalid"
        with pytest.raises(ValueError, match="仅 pending"):
            env.repo.set_snapshot_validation(captured.snapshot_id, "valid", 0)


# ===========================================================================
# 复审修复 R4/R7（2026-10-17，响应独立审阅 b8b2b3f）
# ===========================================================================

class TestR4LocatorGroupAndDiagnostics:
    """R4：同 row locator 的幂等预检必须比较版本分组与诊断。"""

    def test_different_report_period_conflicts(self, evidence_env):
        """同 (snapshot, row_index) 改传另一报告期 → 冲突，不静默返回原期间版本。"""
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        captured = env.capture(unit, rows=[{"TOTAL_OPERATE_INCOME": 100}])

        first = env.create_version(unit, captured, row_index=0,
                                  report_date="2024-03-31")
        with pytest.raises(CaptureConflict, match="版本分组"):
            env.create_version(unit, captured, row_index=0,
                               report_date="2024-06-30")

        # 版本/观察/当前指针全表快照不变
        assert env.conn.execute("SELECT COUNT(*) FROM statement_version").fetchone()[0] == 1
        assert env.conn.execute("SELECT COUNT(*) FROM statement_observation").fetchone()[0] == 1
        row = env.conn.execute(
            "SELECT report_date FROM statement_version WHERE version_id = ?", (first,)
        ).fetchone()
        assert row["report_date"] == "2024-03-31"

    def test_different_diagnostics_conflicts(self, evidence_env):
        """同身份、同期间但诊断不同 → 冲突，旧诊断不被静默改写。"""
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        captured = env.capture(unit, rows=[{"TOTAL_OPERATE_INCOME": 100}])

        env.create_version(unit, captured, row_index=0,
                           report_date="2024-03-31",
                           normalization_diagnostics='["first"]')
        with pytest.raises(CaptureConflict, match="诊断"):
            env.create_version(unit, captured, row_index=0,
                               report_date="2024-03-31",
                               normalization_diagnostics='["different"]')

        obs = env.conn.execute(
            "SELECT normalization_diagnostics FROM statement_observation "
            "WHERE snapshot_id = ? AND row_index = 0",
            (captured.snapshot_id,),
        ).fetchone()
        assert obs["normalization_diagnostics"] == '["first"]'

    def test_same_everything_is_idempotent(self, evidence_env):
        """同身份、同分组、同内容、同诊断 → 幂等返回原版本。"""
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        captured = env.capture(unit, rows=[{"TOTAL_OPERATE_INCOME": 100}])

        first = env.create_version(unit, captured, row_index=0,
                                   report_date="2024-03-31",
                                   normalization_diagnostics='["first"]')
        replay = env.create_version(unit, captured, row_index=0,
                                    report_date="2024-03-31",
                                    normalization_diagnostics='["first"]')
        assert replay == first
        assert env.conn.execute("SELECT COUNT(*) FROM statement_version").fetchone()[0] == 1
        assert env.conn.execute("SELECT COUNT(*) FROM statement_observation").fetchone()[0] == 1


class TestR7AllFinalPagesVerified:
    """R7：完整单元证明中的全部最终页文件必须当前可核验。"""

    def test_other_final_page_missing_rejected(self, evidence_env):
        """声明 pages=2 的证明，第二页文件已删除 → 不能新增 evidence_sufficient=1。"""
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

        cap_a = env.capture(unit, page=1, rows=rows_a, body=body(rows_a))
        cap_b = env.capture(unit, page=2, rows=rows_b, body=body(rows_b))
        (env.store.root / cap_b.file_path).unlink()

        evidence = env.evidence(unit["unit_id"], [cap_a, cap_b])
        with pytest.raises(RawStoreVerifyError, match="缺失"):
            env.create_version(unit, cap_a, row_index=0, evidence=evidence)

        assert env.conn.execute("SELECT COUNT(*) FROM statement_version").fetchone()[0] == 0
        assert env.conn.execute("SELECT COUNT(*) FROM statement_observation").fetchone()[0] == 0

    def test_other_final_page_tampered_rejected(self, evidence_env):
        """第二页文件被篡改 → 不能新增充分证据版本。"""
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

        cap_a = env.capture(unit, page=1, rows=rows_a, body=body(rows_a))
        cap_b = env.capture(unit, page=2, rows=rows_b, body=body(rows_b))
        target_b = env.store.root / cap_b.file_path
        target_b.write_bytes(gzip.compress(b'{"tampered":1}', mtime=0))

        evidence = env.evidence(unit["unit_id"], [cap_a, cap_b])
        with pytest.raises(RawStoreVerifyError):
            env.create_version(unit, cap_a, row_index=0, evidence=evidence)

    def test_all_pages_present_accepted(self, evidence_env):
        """两页文件都在且可核验 → 可新增充分证据版本。"""
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

        cap_a = env.capture(unit, page=1, rows=rows_a, body=body(rows_a))
        cap_b = env.capture(unit, page=2, rows=rows_b, body=body(rows_b))
        evidence = env.evidence(unit["unit_id"], [cap_a, cap_b])

        version_id = env.create_version(unit, cap_a, row_index=0, evidence=evidence)
        flag = env.conn.execute(
            "SELECT evidence_sufficient FROM statement_version WHERE version_id = ?",
            (version_id,),
        ).fetchone()[0]
        assert flag == 1


class TestC2TerminalFailureNotSufficient:
    """C2：终态失败响应（http_error/非 2xx）不能凭 valid 标志冒充充分证据。"""

    def test_http_error_outcome_rejected(self, evidence_env):
        """最终尝试 outcome=http_error、快照标 valid → 不能声明 evidence_sufficient=1。"""
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        env.repo.mark_unit_running(unit["unit_id"])
        rows = [{"TOTAL_OPERATE_INCOME": 100}]

        cap = env.capture(
            unit, rows=rows, http_status=500, validation="valid", outcome="http_error",
        )
        evidence = env.evidence(unit["unit_id"], [cap])
        with pytest.raises(ValueError, match="outcome=http_error"):
            env.create_version(unit, cap, row_index=0, evidence=evidence)
        assert env.conn.execute("SELECT COUNT(*) FROM statement_version").fetchone()[0] == 0

    def test_non_success_http_status_rejected(self, evidence_env):
        """outcome=ok 但 HTTP 500 → 不能声明充分证据。"""
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        env.repo.mark_unit_running(unit["unit_id"])
        rows = [{"TOTAL_OPERATE_INCOME": 100}]

        cap = env.capture(
            unit, rows=rows, http_status=500, validation="valid", outcome="ok",
        )
        evidence = env.evidence(unit["unit_id"], [cap])
        with pytest.raises(ValueError, match="非成功"):
            env.create_version(unit, cap, row_index=0, evidence=evidence)
        assert env.conn.execute("SELECT COUNT(*) FROM statement_version").fetchone()[0] == 0

    def test_success_2xx_accepted(self, evidence_env):
        """outcome=ok 且 HTTP 200 → 可声明充分证据（不回归）。"""
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        env.repo.mark_unit_running(unit["unit_id"])
        rows = [{"TOTAL_OPERATE_INCOME": 100}]

        cap = env.capture(unit, rows=rows, http_status=200, validation="valid", outcome="ok")
        evidence = env.evidence(unit["unit_id"], [cap])
        version_id = env.create_version(unit, cap, row_index=0, evidence=evidence)
        flag = env.conn.execute(
            "SELECT evidence_sufficient FROM statement_version WHERE version_id = ?",
            (version_id,),
        ).fetchone()[0]
        assert flag == 1


class TestD2FinalAttemptFromDatabase:
    """D2：最终页身份由数据库事实决定，不能只信调用方给出的快照列表。"""

    def test_stale_ok_snapshot_after_later_failure_rejected(self, evidence_env):
        """attempt 1 成功、attempt 2 失败 → 传 attempt 1 旧快照不能声明充分证据。"""
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        env.repo.mark_unit_running(unit["unit_id"])
        rows = [{"TOTAL_OPERATE_INCOME": 100}]

        old = env.capture(
            unit, http_attempt=1, rows=rows, outcome="ok", http_status=200,
        )
        env.capture(
            unit, http_attempt=2, rows=[], body=b"HTTP 500",
            outcome="http_error", http_status=500, validation="invalid",
        )
        evidence = env.evidence(unit["unit_id"], [old])
        with pytest.raises(ValueError, match="最终尝试是 http_attempt=2"):
            env.create_version(unit, old, row_index=0, evidence=evidence)
        assert env.conn.execute("SELECT COUNT(*) FROM statement_version").fetchone()[0] == 0

    def test_stale_ok_snapshot_after_later_success_rejected(self, evidence_env):
        """attempt 1 成功、attempt 2 也成功 → 只能引用 attempt 2 的快照。"""
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        env.repo.mark_unit_running(unit["unit_id"])
        rows = [{"TOTAL_OPERATE_INCOME": 100}]

        old = env.capture(
            unit, http_attempt=1, rows=rows, outcome="ok", http_status=200,
        )
        later = env.capture(
            unit, http_attempt=2, rows=rows, outcome="ok", http_status=200,
        )
        # 传旧快照应被拒绝
        evidence_old = env.evidence(unit["unit_id"], [old])
        with pytest.raises(ValueError, match="最终尝试是 http_attempt=2"):
            env.create_version(unit, old, row_index=0, evidence=evidence_old)
        # 传最新快照应被接受
        evidence_new = env.evidence(unit["unit_id"], [later])
        version_id = env.create_version(unit, later, row_index=0, evidence=evidence_new)
        flag = env.conn.execute(
            "SELECT evidence_sufficient FROM statement_version WHERE version_id = ?",
            (version_id,),
        ).fetchone()[0]
        assert flag == 1


class TestE1ProofCoversAllFinalPages:
    """E1：单元完整性证明必须精确覆盖全部最终页，不能是任意子集。

    声明两页、第二页最终失败；证明只包含第一页，仍能写入充分证据版本——这是缺陷。
    修复后：证明页集合必须等于数据库最终页集合，遗漏/重复/范围外页均拒绝。
    """

    def test_subset_proof_hides_other_failed_page(self, evidence_env):
        """声明 2 页、页 2 最终 http_error；证明只给页 1 → 拒绝，无版本写入。"""
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        env.repo.mark_unit_running(unit["unit_id"])
        rows = [{"TOTAL_OPERATE_INCOME": 100}]

        def body(rows, pages, count):
            return json.dumps(
                {"success": True, "result": {"pages": pages, "count": count, "data": rows}}
            ).encode()

        page1 = env.capture(unit, page=1, rows=rows, body=body(rows, 2, 2))
        env.capture(
            unit, page=2, rows=[], body=b"HTTP 500",
            outcome="http_error", http_status=500, validation="invalid",
        )
        # 证明只引用页 1，遗漏页 2
        evidence = env.evidence(unit["unit_id"], [page1])
        with pytest.raises(ValueError, match="未精确覆盖全部最终页"):
            env.create_version(unit, page1, row_index=0, evidence=evidence)
        assert env.conn.execute("SELECT COUNT(*) FROM statement_version").fetchone()[0] == 0

    def test_full_proof_accepted(self, evidence_env):
        """声明 2 页、两页都成功；证明给全部最终页 → 接受，证据充分。"""
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

        page1 = env.capture(unit, page=1, rows=rows_a, body=body(rows_a, 2, 2))
        page2 = env.capture(unit, page=2, rows=rows_b, body=body(rows_b, 2, 2))
        evidence = env.evidence(unit["unit_id"], [page1, page2])
        version_id = env.create_version(unit, page1, row_index=0, evidence=evidence)
        flag = env.conn.execute(
            "SELECT evidence_sufficient FROM statement_version WHERE version_id = ?",
            (version_id,),
        ).fetchone()[0]
        assert flag == 1

    def test_duplicate_page_in_proof_rejected(self, evidence_env):
        """证明包含重复页 → 拒绝（proof_pages 集合小于 page_ids 长度）。"""
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        env.repo.mark_unit_running(unit["unit_id"])
        rows = [{"TOTAL_OPERATE_INCOME": 100}]

        page1 = env.capture(unit, page=1, rows=rows)
        # 证明重复引用同一页 1 两次
        evidence = env.evidence(unit["unit_id"], [page1, page1])
        with pytest.raises(ValueError, match="未精确覆盖全部最终页"):
            env.create_version(unit, page1, row_index=0, evidence=evidence)
        assert env.conn.execute("SELECT COUNT(*) FROM statement_version").fetchone()[0] == 0


class TestF1ProofCoversDeclaredRange:
    """F1：证明必须覆盖源端声明的全部页范围，不能只覆盖已登记页。

    源端声明两页，但只登记第一页，仍能写入充分证据版本——这是缺陷。
    修复后：从已核验原体取得源端声明 pages，proof 页必须等于声明范围。
    """

    def test_unregistered_declared_page_rejected(self, evidence_env):
        """页 1 原体声明 pages=2，但只登记页 1（无页 2 attempt）→ 拒绝。"""
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        env.repo.mark_unit_running(unit["unit_id"])
        rows = [{"SECUCODE": "000001.SZ", "REPORT_DATE": "2024-03-31",
                 "TOTAL_OPERATE_INCOME": 100}]

        def body(rows, pages, count):
            return json.dumps(
                {"success": True, "result": {"pages": pages, "count": count, "data": rows}}
            ).encode()

        page1 = env.capture(unit, page=1, rows=rows, body=body(rows, 2, 2))
        # 只登记页 1，未登记页 2——原体已声明整个请求范围未取完
        evidence = env.evidence(unit["unit_id"], [page1])
        with pytest.raises(ValueError, match="未覆盖源端声明的全部页范围"):
            env.create_version(unit, page1, row_index=0, evidence=evidence)
        assert env.conn.execute("SELECT COUNT(*) FROM statement_version").fetchone()[0] == 0

    def test_full_declared_range_accepted(self, evidence_env):
        """页 1 声明 pages=2，登记页 1 和页 2，证明覆盖全部 → 接受。"""
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

        page1 = env.capture(unit, page=1, rows=rows_a, body=body(rows_a, 2, 2))
        page2 = env.capture(unit, page=2, rows=rows_b, body=body(rows_b, 2, 2))
        evidence = env.evidence(unit["unit_id"], [page1, page2])
        version_id = env.create_version(unit, page1, row_index=0, evidence=evidence)
        flag = env.conn.execute(
            "SELECT evidence_sufficient FROM statement_version WHERE version_id = ?",
            (version_id,),
        ).fetchone()[0]
        assert flag == 1

    def test_legal_empty_single_page_accepted(self, evidence_env):
        """页 1 声明 pages=0（合法空），证明只含页 1 → 声明范围检查通过。

        合法空无数据行，不能写业务版本；验证 F1 声明范围检查不拒绝合法空单页证明
        （到达行定位核验器而非声明范围检查即表示 F1 通过）。
        """
        env = evidence_env
        env.new_run()
        unit = env.new_unit()
        env.repo.mark_unit_running(unit["unit_id"])
        body = json.dumps(
            {"success": True, "result": {"pages": 0, "count": 0, "data": []}}
        ).encode()
        page1 = env.capture(unit, rows=[], body=body)
        evidence = env.evidence(unit["unit_id"], [page1])
        # 合法空无数据行，行定位核验器拒绝 row_index=0（空列表无行可定位）；
        # 但这发生在 F1 声明范围检查之后——证明 F1 不拒绝合法空单页证明
        with pytest.raises(ValueError, match="行定位与传入值不一致"):
            env.create_version(unit, page1, row_index=0, evidence=evidence)


class TestF2F3WritePathUsesBody:
    """F2/F3：写入路径与 audit 共用原体解析结果，拒绝不完整/矛盾证据。"""

    def _env_unit(self, env):
        env.new_run()
        unit = env.new_unit()
        env.repo.mark_unit_running(unit["unit_id"])
        return unit

    def test_zero_pages_nonempty_body_rejected(self, evidence_env):
        """F2：pages=0 但 data 非空 → 写入拒绝。"""
        env = evidence_env
        unit = self._env_unit(env)
        rows = [{"TOTAL_OPERATE_INCOME": 100}]
        body = json.dumps(
            {"success": True, "result": {"pages": 0, "count": 1, "data": rows}}
        ).encode()
        page1 = env.capture(unit, rows=rows, body=body)
        evidence = env.evidence(unit["unit_id"], [page1])
        with pytest.raises(ValueError, match="不满足合法空条件"):
            env.create_version(unit, page1, row_index=0, evidence=evidence)
        assert env.conn.execute("SELECT COUNT(*) FROM statement_version").fetchone()[0] == 0

    def test_count_mismatch_body_rows_rejected(self, evidence_env):
        """F3：原体两行但 count=1 → 写入拒绝。"""
        env = evidence_env
        unit = self._env_unit(env)
        actual = [{"TOTAL_OPERATE_INCOME": 100}, {"TOTAL_OPERATE_INCOME": 200}]
        body = json.dumps(
            {"success": True, "result": {"pages": 1, "count": 1, "data": actual}}
        ).encode()
        page1 = env.capture(unit, rows=actual, body=body)
        evidence = env.evidence(unit["unit_id"], [page1])
        with pytest.raises(ValueError, match="原体行数合计"):
            env.create_version(unit, page1, row_index=0, evidence=evidence)
        assert env.conn.execute("SELECT COUNT(*) FROM statement_version").fetchone()[0] == 0

    def test_non_object_row_rejected(self, evidence_env):
        """F3：原体 data 含非对象元素 → 写入拒绝。"""
        env = evidence_env
        unit = self._env_unit(env)
        body = json.dumps(
            {"success": True, "result": {"pages": 1, "count": 1, "data": [7]}}
        ).encode()
        page1 = env.capture(unit, rows=[7], body=body)
        evidence = env.evidence(unit["unit_id"], [page1])
        with pytest.raises(ValueError, match="非对象元素"):
            env.create_version(
                unit, page1, row_index=0, evidence=evidence,
                standardized={"total_operate_income": 100},
            )
        assert env.conn.execute("SELECT COUNT(*) FROM statement_version").fetchone()[0] == 0

    def test_metadata_record_count_mismatch_rejected(self, evidence_env):
        """F3：原体行数与 snapshot.record_count 不一致 → 写入拒绝。"""
        env = evidence_env
        unit = self._env_unit(env)
        metadata = [{"TOTAL_OPERATE_INCOME": 100}]
        actual = metadata + [{"TOTAL_OPERATE_INCOME": 200}]
        body = json.dumps(
            {"success": True, "result": {"pages": 1, "count": 2, "data": actual}}
        ).encode()
        page1 = env.capture(unit, rows=metadata, body=body)
        evidence = env.evidence(unit["unit_id"], [page1])
        with pytest.raises(ValueError, match="record_count"):
            env.create_version(unit, page1, row_index=0, evidence=evidence)
        assert env.conn.execute("SELECT COUNT(*) FROM statement_version").fetchone()[0] == 0
