"""2B-2 统一采集单元协调器测试（B23-B33）。

真实临时库 + 真实临时 raw_root；HTTP 脚本化；时钟/睡眠注入。
"""

from __future__ import annotations

import json
import math
import sqlite3

import pytest

import fin_feature.ingestion.service as service_module
import fin_feature.storage.repository as repository_module
from fin_feature.ingestion.capture import ActiveTransactionError
from fin_feature.ingestion.http_evidence import EvidenceRequestExecutor
from fin_feature.ingestion.service import (
    CRAWLER_VERSION,
    SOURCE_NAME,
    FinancialIngestionCoordinator,
    FinancialUnitSpec,
)
from fin_feature.storage.repository import Repository

from ingestion_helpers import (
    FIN,
    URL,
    FakeResponse,
    FakeSession,
    SleepRecorder,
    StepClock,
    body_for,
    count_rows,
    current_versions,
    fi_row,
    observation_rows,
    raw_rows,
    version_of,
    version_rows,
)

SECUCODE = "000001.SZ"


def make_coordinator(env, session, *, max_retries=3, recent_periods=None,
                     max_pages=10, clock=None, page_size=50):
    clock = clock or StepClock()
    executor = EvidenceRequestExecutor(
        repository=env.repo,
        raw_store=env.store,
        session=session,
        url=URL,
        timeout=15,
        max_retries=max_retries,
        sleep=SleepRecorder(),
        clock=clock,
    )
    coordinator = FinancialIngestionCoordinator(
        env.repo,
        env.store,
        executor,
        page_size=page_size,
        max_pages_per_unit=max_pages,
        recent_periods=recent_periods,
        clock=clock,
    )
    return coordinator, executor


def spec(secucode=SECUCODE, report_name=FIN):
    return FinancialUnitSpec(secucode=secucode, report_name=report_name)


def assert_observed_matches_received(conn):
    """H1：每条观察的 observed_at 与它引用的响应真实接收时间一致。"""
    rows = conn.execute(
        "SELECT o.observed_at, s.fetched_at FROM statement_observation o "
        "JOIN raw_snapshot s ON s.snapshot_id = o.snapshot_id"
    ).fetchall()
    assert rows
    for observed_at, fetched_at in rows:
        assert observed_at == fetched_at
        assert observed_at.endswith("Z") and "T" in observed_at


# ----------------------------------------------------------------------
# B23：完整分页后选最近 N 个规范化报告期
# ----------------------------------------------------------------------
def test_b23_recent_periods_selects_all_revisions_of_latest_periods(evidence_env):
    env = evidence_env
    rows = [
        fi_row(report_date="2023-03-31", notice_date="2023-04-20", income=1.0),
        fi_row(report_date="2024-06-30", notice_date="2024-08-20", income=6.0),
        fi_row(report_date="2024-03-31", notice_date="2024-04-20", income=2.0),
        fi_row(report_date="2024-03-31", notice_date="2024-04-25", income=3.0),
    ]
    session = FakeSession({
        1: [FakeResponse(body=body_for(rows[:2], pages=3, count=4))],
        2: [FakeResponse(body=body_for(rows[2:3], pages=3, count=4))],
        3: [FakeResponse(body=body_for(rows[3:], pages=3, count=4))],
    })
    coordinator, _ = make_coordinator(env, session, recent_periods=2)

    result = coordinator.run([spec()])

    assert result.status == "success"
    unit = result.units[0]
    assert unit.status == "success"
    assert unit.source_rows == 4
    assert unit.selected_rows == 3, "选中最近两个报告期的全部修订，不是截前 N 行"
    # 2023-03-31 不在最近两期，不写版本；原页仍然全部留存
    assert len(result.units) == 1
    assert len(session.calls) == 3
    versions = version_rows(env.conn)
    assert sorted(v["report_date"] for v in versions) == [
        "2024-03-31", "2024-03-31", "2024-06-30",
    ]
    assert all(v["evidence_sufficient"] == 1 for v in versions)
    snapshots = env.conn.execute(
        "SELECT COUNT(*) FROM raw_snapshot WHERE run_id = ?", (result.run_id,)
    ).fetchone()[0]
    assert snapshots == 3, "未入选报告期的原始页也留存"


# ----------------------------------------------------------------------
# B25：同日期不同内容、同内容不同公告、A→B→A
# ----------------------------------------------------------------------
def test_b25_versions_observations_and_a_b_a(evidence_env):
    env = evidence_env
    clock = StepClock(start="2026-10-17T08:00:00Z", step_seconds=5)
    row_a = fi_row(report_date="2024-03-31", notice_date="2024-01-01", income=100.0)
    row_b = fi_row(report_date="2024-03-31", notice_date="2024-01-02", income=200.0)

    run1_session = FakeSession({1: [FakeResponse(body=body_for([row_a, row_b], count=2))]})
    coordinator, _ = make_coordinator(env, run1_session, clock=clock)
    result1 = coordinator.run([spec()])
    assert result1.status == "success"

    versions = version_rows(env.conn)
    assert len(versions) == 2
    by_income = {
        json.loads(v["standardized_fields"])["total_operate_income"]: v
        for v in versions
    }
    current = current_versions(env.conn)
    assert len(current) == 1
    assert json.loads(current[0]["standardized_fields"])["total_operate_income"] == 200
    assert by_income[100]["is_superseded"] == 1
    assert by_income[100]["superseded_by"] == by_income[200]["version_id"]
    assert_observed_matches_received(env.conn)

    # A→B→A：新批次重新返回 A 的内容，公告较新（2024-01-03）
    row_a2 = fi_row(report_date="2024-03-31", notice_date="2024-01-03", income=100.0)
    run2_session = FakeSession({1: [FakeResponse(body=body_for([row_a2], count=1))]})
    coordinator2, _ = make_coordinator(env, run2_session, clock=clock)
    result2 = coordinator2.run([spec()])
    assert result2.status == "success"
    assert result2.units[0].new_versions == 0, "同内容复用版本，不制造新版本"
    assert result2.units[0].new_observations == 1

    versions_after = version_rows(env.conn)
    assert len(versions_after) == 2
    restored = version_of(env.conn, by_income[100]["version_id"])
    assert restored["is_superseded"] == 0
    assert restored["notice_date"] == "2024-01-01", "版本行保留首次观察元信息"
    b_version = version_of(env.conn, by_income[200]["version_id"])
    assert b_version["is_superseded"] == 1
    assert b_version["superseded_by"] == restored["version_id"]

    observations = [
        o for o in observation_rows(env.conn)
        if o["version_id"] == restored["version_id"]
    ]
    assert [o["notice_date"] for o in observations] == ["2024-01-01", "2024-01-03"]
    assert len(observation_rows(env.conn)) == 3
    assert_observed_matches_received(env.conn)


def test_b25_same_content_different_notice_is_one_version_two_observations(
    evidence_env,
):
    env = evidence_env
    rows = [
        fi_row(report_date="2024-03-31", notice_date="2024-04-20", income=100.0),
        fi_row(report_date="2024-03-31", notice_date="2024-04-25", income=100.0),
    ]
    session = FakeSession({1: [FakeResponse(body=body_for(rows, count=2))]})
    coordinator, _ = make_coordinator(env, session)

    result = coordinator.run([spec()])

    assert result.status == "success"
    assert len(version_rows(env.conn)) == 1
    observations = observation_rows(env.conn)
    assert sorted(o["notice_date"] for o in observations) == ["2024-04-20", "2024-04-25"]
    assert observations[0]["version_id"] == observations[1]["version_id"]
    assert len(current_versions(env.conn)) == 1
    assert_observed_matches_received(env.conn)


def test_b25_insufficient_old_version_gains_new_verified_observation(evidence_env):
    """旧版本（迁移来的不足证据）再次被观察时：版本首次标志不变，观察新增。"""
    env = evidence_env
    legacy_version = env.repo.create_statement_version(
        secucode=SECUCODE,
        report_date="2024-03-31",
        table_type="financial_indicator",
        source_name=SOURCE_NAME,
        standardized_fields=json.dumps({
            "security_code": 1, "total_operate_income": 100.0,
            "weightavg_roe": 0.1675,
        }),
        raw_fields=json.dumps({
            "total_operate_income": 100.0, "weightavg_roe": 16.75,
        }),
        evidence_sufficient=0,
    )
    env.conn.commit()  # 旧数据由历史批次提交；协调器要求无外层活动事务
    row = fi_row(report_date="2024-03-31", notice_date="2024-04-20", income=100.0)
    session = FakeSession({1: [FakeResponse(body=body_for([row], count=1))]})
    coordinator, _ = make_coordinator(env, session)

    result = coordinator.run([spec()])

    assert result.status == "success"
    version = version_of(env.conn, legacy_version)
    assert version["evidence_sufficient"] == 0, "复用的旧版本不冒改首次证据标志"
    observations = env.repo.get_observations_for_version(legacy_version)
    assert len(observations) == 1
    assert observations[0]["snapshot_id"] is not None
    assert_observed_matches_received(env.conn)


# ----------------------------------------------------------------------
# B26：阶段一选值反例
# ----------------------------------------------------------------------
def test_b26_ambiguous_notice_and_entry_uses_digest_and_logs(evidence_env):
    env = evidence_env
    rows = [
        fi_row(report_date="2024-03-31", notice_date="2024-04-20",
               entry_time="2024-04-20 18:00:00", income=1.0),
        fi_row(report_date="2024-03-31", notice_date="2024-04-20",
               entry_time="2024-04-20 18:00:00", income=2.0),
    ]
    session = FakeSession({1: [FakeResponse(body=body_for(rows, count=2))]})
    coordinator, _ = make_coordinator(env, session)

    result = coordinator.run([spec()])

    assert result.status == "success"
    assert len(version_rows(env.conn)) == 2
    current = current_versions(env.conn)
    assert len(current) == 1
    logs = env.repo.get_clean_logs()
    ambiguous = [log for log in logs if log["action"] == "dedup_ambiguous"]
    assert ambiguous, "同日同 entry 的不同内容必须留下歧义记录"

    # 重复运行同样输入：结果确定（同一版本成为当前），不依赖到达顺序
    session2 = FakeSession({1: [FakeResponse(body=body_for(list(reversed(rows)), count=2))]})
    coordinator2, _ = make_coordinator(env, session2)
    coordinator2.run([spec()])
    assert [v["version_id"] for v in current_versions(env.conn)] == [
        current[0]["version_id"]
    ]


def test_b26_null_notice_does_not_borrow_fetch_time(evidence_env):
    env = evidence_env
    rows = [
        fi_row(report_date="2024-03-31", notice_date="2024-01-01", income=100.0),
        fi_row(report_date="2024-03-31", notice_date=None, income=200.0),
    ]
    session = FakeSession({1: [FakeResponse(body=body_for(rows, count=2))]})
    coordinator, _ = make_coordinator(env, session)

    result = coordinator.run([spec()])

    assert result.status == "success"
    versions = version_rows(env.conn)
    null_notice = [v for v in versions if v["notice_date"] is None]
    assert len(null_notice) == 1, "NULL 公告不借 fetched_at 补齐"
    assert null_notice[0]["observed_at"].endswith("Z")
    current = current_versions(env.conn)
    assert current[0]["notice_date"] == "2024-01-01", "有效公告日优先于 NULL"


def test_b26_later_batch_with_older_notice_does_not_downgrade(evidence_env):
    env = evidence_env
    clock = StepClock()
    newer = fi_row(report_date="2024-03-31", notice_date="2024-06-01", income=200.0)
    older = fi_row(report_date="2024-03-31", notice_date="2024-01-01", income=100.0)

    coordinator1, _ = make_coordinator(
        env, FakeSession({1: [FakeResponse(body=body_for([newer], count=1))]}),
        clock=clock,
    )
    assert coordinator1.run([spec()]).status == "success"
    newer_version = current_versions(env.conn)[0]["version_id"]

    coordinator2, _ = make_coordinator(
        env, FakeSession({1: [FakeResponse(body=body_for([older], count=1))]}),
        clock=clock,
    )
    assert coordinator2.run([spec()]).status == "success"

    current = current_versions(env.conn)
    assert [v["version_id"] for v in current] == [newer_version], (
        "后续批次只返回旧公告时不得降级当前版本"
    )


def test_b26_date_format_order_uses_normalized_notice(evidence_env):
    env = evidence_env
    # 输入顺序与规范化后的公告日相反：2024/2/1 实际更晚
    rows = [
        fi_row(report_date="2024-03-31", notice_date="2024/2/1", income=200.0),
        fi_row(report_date="2024-03-31", notice_date="2024-01-15", income=100.0),
    ]
    session = FakeSession({1: [FakeResponse(body=body_for(rows, count=2))]})
    coordinator, _ = make_coordinator(env, session)

    result = coordinator.run([spec()])

    assert result.status == "success"
    current = current_versions(env.conn)
    assert json.loads(current[0]["standardized_fields"])["total_operate_income"] == 200
    watermark = env.repo.get_watermark(FIN, SECUCODE)
    assert watermark == "2024-02-01", "水位使用规范化公告日"


# ----------------------------------------------------------------------
# B27：单位、原计量、非有限值、fixture 单位污染
# ----------------------------------------------------------------------
def test_b27_standardization_keeps_original_measure_and_diagnostics(evidence_env):
    env = evidence_env
    from fin_feature.features.standardize import clear_unit_overrides, set_unit_override

    set_unit_override("total_operate_income", "percent")
    try:
        row = fi_row(
            report_date="2024-03-31", notice_date="2024-04-20",
            income=123456789.0, roe=16.75, BPS=float("inf"),
        )
        session = FakeSession({1: [FakeResponse(body=body_for([row], count=1))]})
        coordinator, _ = make_coordinator(env, session)

        result = coordinator.run([spec()])
    finally:
        clear_unit_overrides()

    assert result.status == "success"
    raw = raw_rows(env.conn)[0]
    assert raw["total_operate_income"] == 123456789.0, "raw 保持原计量"
    assert raw["weightavg_roe"] == 16.75

    version = version_rows(env.conn)[0]
    standardized = json.loads(version["standardized_fields"])
    assert standardized["weightavg_roe"] == 0.1675
    assert standardized["total_operate_income"] == 123456789, (
        "固定 FIELD_UNITS 不受运行时 fixture 覆盖影响（金额不除以 100）"
    )
    assert version["evidence_sufficient"] == 1
    # 证据充分不等于可比性维度已知
    assert version["consolidate_scope"] == "unknown"
    assert version["period_basis"] == "unknown"
    assert version["currency"] == "unknown"
    assert version["amount_unit"] == "unknown"
    # 非有限值以可逆字符串进入 raw JSON（序列化安全），raw 表保留原值
    raw_fields = json.loads(version["raw_fields"])
    assert raw_fields.get("bps") == "inf"
    assert raw["bps"] == float("inf")


def test_b27_non_finite_value_is_diagnosed_not_selected(evidence_env):
    env = evidence_env
    row = fi_row(
        report_date="2024-03-31", notice_date="2024-04-20",
        income=float("inf"), roe=12.0,
    )
    session = FakeSession({1: [FakeResponse(body=body_for([row], count=1))]})
    coordinator, _ = make_coordinator(env, session)

    result = coordinator.run([spec()])

    assert result.status == "success"
    version = version_rows(env.conn)[0]
    standardized = json.loads(version["standardized_fields"])
    assert "total_operate_income" not in standardized
    observations = env.repo.get_observations_for_version(version["version_id"])
    diagnostics = json.loads(observations[0]["normalization_diagnostics"] or "[]")
    assert any(
        d["field"] == "total_operate_income" and d["reason_code"] == "invalid_value"
        for d in diagnostics
    )
    raw = raw_rows(env.conn)[0]
    assert math.isinf(raw["total_operate_income"])


def test_b27_unknown_unit_is_not_guessed(evidence_env):
    """资产负债表 current_ratio 单位未经验证：保留原值，标准值不猜测。"""
    env = evidence_env
    from fin_feature.constants import REPORT_BALANCE_SHEET

    row = {
        "SECUCODE": SECUCODE,
        "SECURITY_CODE": "000001",
        "SECURITY_NAME_ABBR": "测试公司",
        "REPORT_DATE": "2024-03-31",
        "NOTICE_DATE": "2024-04-20",
        "TOTAL_ASSETS": 1000.0,
        "CURRENT_RATIO": 1.5,
    }
    session = FakeSession({1: [FakeResponse(body=body_for([row], count=1))]})
    coordinator, _ = make_coordinator(env, session)

    result = coordinator.run([spec(report_name=REPORT_BALANCE_SHEET)])

    assert result.status == "success"
    raw = raw_rows(env.conn, table="raw_balance_sheet")[0]
    assert raw["current_ratio"] == 1.5, "未知单位仍保留原始计量"
    version = version_rows(env.conn, table_type="balance_sheet")[0]
    standardized = json.loads(version["standardized_fields"])
    assert "current_ratio" not in standardized, "未知单位字段不猜标准值"
    assert standardized["total_assets"] == 1000
    observations = env.repo.get_observations_for_version(version["version_id"])
    diagnostics = json.loads(observations[0]["normalization_diagnostics"] or "[]")
    assert any(
        d["field"] == "current_ratio" and d["reason_code"] == "unknown_unit"
        for d in diagnostics
    )


# ----------------------------------------------------------------------
# B28：公司不符 / 报告期非法 / 映射失败：整单元回滚
# ----------------------------------------------------------------------
@pytest.mark.parametrize("case,rows", [
    ("company_mismatch", [
        fi_row(report_date="2024-03-31", notice_date="2024-04-20", income=1.0),
        fi_row(secucode="000002.SZ", report_date="2024-03-31",
               notice_date="2024-04-20", income=2.0),
    ]),
    ("invalid_report_date", [
        fi_row(report_date="not-a-date", notice_date="2024-04-20", income=1.0),
    ]),
])
def test_b28_invalid_row_fails_whole_unit_without_partial_writes(
    evidence_env, case, rows,
):
    env = evidence_env
    session = FakeSession({1: [FakeResponse(body=body_for(rows, count=len(rows)))]})
    coordinator, _ = make_coordinator(env, session)

    result = coordinator.run([spec()])

    assert result.status == "failed"
    assert result.units[0].failure_code == "schema_error"
    assert count_rows(env.conn, "raw_financial_indicator") == 0
    assert count_rows(env.conn, "statement_version") == 0
    assert count_rows(env.conn, "statement_observation") == 0
    assert env.repo.get_watermark(FIN, SECUCODE) is None
    unit = env.repo.get_units_for_run(result.run_id)[0]
    assert unit["status"] == "failed"
    # 失败响应审计仍在（页本身解析有效且已校验）
    snapshots = env.conn.execute(
        "SELECT validation_status FROM raw_snapshot WHERE run_id = ?", (result.run_id,)
    ).fetchall()
    assert [s[0] for s in snapshots] == ["valid"]


def test_b28_mapping_failure_rolls_back_unit(evidence_env, monkeypatch):
    env = evidence_env
    row = fi_row(report_date="2024-03-31", notice_date="2024-04-20", income=1.0)
    session = FakeSession({1: [FakeResponse(body=body_for([row], count=1))]})
    coordinator, _ = make_coordinator(env, session)

    def broken_map(raw_record, report_name):
        raise ValueError("未知字段映射")

    monkeypatch.setattr(service_module, "map_fields", broken_map)

    result = coordinator.run([spec()])

    assert result.status == "failed"
    assert result.units[0].failure_code == "schema_error"
    assert count_rows(env.conn, "raw_financial_indicator") == 0
    assert count_rows(env.conn, "statement_version") == 0
    assert count_rows(env.conn, "statement_observation") == 0


def test_b18_storage_failure_stops_before_business_writes(evidence_env, monkeypatch):
    """RawStore 失败：单元失败，不进入兼容 raw/版本/水位写入。"""
    env = evidence_env
    from fin_feature.storage.raw_store import RawStoreError

    session = FakeSession({1: [FakeResponse(body=body_for([_row_for_run()], count=1))]})
    coordinator, _ = make_coordinator(env, session, max_retries=3)

    def broken_publish(context, body):
        raise RawStoreError("无法创建证据文件")

    monkeypatch.setattr(env.store, "publish", broken_publish)

    result = coordinator.run([spec()])

    assert result.status == "failed"
    assert result.units[0].failure_code == "storage_error"
    assert len(session.calls) == 1, "存储故障不重试"
    assert count_rows(env.conn, "raw_financial_indicator") == 0
    assert count_rows(env.conn, "statement_version") == 0
    assert count_rows(env.conn, "statement_observation") == 0
    assert env.repo.get_watermark(FIN, SECUCODE) is None


# ----------------------------------------------------------------------
# B29：空公告重复抓取的兼容 raw 幂等
# ----------------------------------------------------------------------
def test_b29_null_notice_compat_raw_is_idempotent(evidence_env):
    env = evidence_env
    clock = StepClock()
    row = fi_row(report_date="2024-03-31", notice_date=None, income=100.0)
    results = []
    for _ in range(2):
        coordinator, _ = make_coordinator(
            env, FakeSession({1: [FakeResponse(body=body_for([row], count=1))]}),
            clock=clock,
        )
        results.append(coordinator.run([spec()]))

    assert all(r.status == "success" for r in results)
    assert results[1].units[0].compat_writes == 0, "同一映射内容复用既有 raw 行"
    assert len(raw_rows(env.conn)) == 1
    assert len(version_rows(env.conn)) == 1
    assert len(observation_rows(env.conn)) == 2

    changed = fi_row(report_date="2024-03-31", notice_date=None, income=300.0)
    coordinator3, _ = make_coordinator(
        env, FakeSession({1: [FakeResponse(body=body_for([changed], count=1))]}),
        clock=clock,
    )
    result3 = coordinator3.run([spec()])
    assert result3.units[0].compat_writes == 1
    rows = raw_rows(env.conn)
    assert len(rows) == 2, "内容变化保留历史 raw，不删除旧行"
    assert sorted(r["total_operate_income"] for r in rows) == [100.0, 300.0]


# ----------------------------------------------------------------------
# B30：各写入点失败 → 单元所有业务表一致回滚
# ----------------------------------------------------------------------
def _row_for_run():
    return fi_row(report_date="2024-03-31", notice_date="2024-04-20", income=100.0)


@pytest.mark.parametrize("target", [
    "upsert_compat_raw", "create_statement_version",
    "apply_current_version", "update_watermark", "finish_unit_success",
])
def test_b30_failure_at_each_write_point_rolls_back_all_business_tables(
    evidence_env, monkeypatch, target,
):
    env = evidence_env
    session = FakeSession({1: [FakeResponse(body=body_for([_row_for_run()], count=1))]})
    coordinator, _ = make_coordinator(env, session)

    if target == "finish_unit_success":
        original = env.repo.finish_unit

        def flaky(unit_id, status, **kwargs):
            if status == "success":
                raise RuntimeError("注入：成功结算写入失败")
            return original(unit_id, status, **kwargs)

        monkeypatch.setattr(env.repo, "finish_unit", flaky)
    else:
        def boom(*args, **kwargs):
            raise RuntimeError(f"注入：{target} 写入失败")

        monkeypatch.setattr(env.repo, target, boom)

    result = coordinator.run([spec()])

    assert result.status == "failed"
    assert result.exit_code == 1
    assert result.units[0].status == "failed"
    assert result.units[0].failure_code == "internal_error"
    assert result.units[0].new_versions == 0
    assert result.units[0].new_observations == 0
    assert count_rows(env.conn, "raw_financial_indicator") == 0
    assert count_rows(env.conn, "statement_version") == 0
    assert count_rows(env.conn, "statement_observation") == 0
    assert env.repo.get_watermark(FIN, SECUCODE) is None
    # 失败结算不丢弃证据
    assert count_rows(env.conn, "raw_snapshot") == 1
    unit = env.repo.get_units_for_run(result.run_id)[0]
    if target == "finish_unit_success":
        assert unit["status"] == "failed", "失败结算走独立短事务"
    else:
        assert unit["status"] == "failed"


def test_b30_successful_unit_unaffected_by_other_unit_failure(evidence_env, monkeypatch):
    env = evidence_env
    good = fi_row(secucode="000001.SZ", report_date="2024-03-31",
                  notice_date="2024-04-20", income=100.0)
    bad = fi_row(secucode="000002.SZ", report_date="2024-03-31",
                 notice_date="2024-04-20", income=200.0)
    session = FakeSession({
        1: [FakeResponse(body=body_for([good], count=1)),
            FakeResponse(body=body_for([bad], count=1))],
    })
    coordinator, _ = make_coordinator(env, session)

    original = env.repo.upsert_compat_raw

    def flaky(table_suffix, records):
        records = list(records)
        if any(r.get("secucode") == "000002.SZ" for r in records):
            raise RuntimeError("注入：第二个单元写入失败")
        return original(table_suffix, records)

    monkeypatch.setattr(env.repo, "upsert_compat_raw", flaky)

    result = coordinator.run([spec("000001.SZ"), spec("000002.SZ")])

    assert result.status == "partial_failure"
    assert result.success == 1 and result.failed == 1
    by_secucode = {u.secucode: u for u in result.units}
    assert by_secucode["000001.SZ"].status == "success"
    assert by_secucode["000002.SZ"].status == "failed"
    assert len(raw_rows(env.conn, secucode="000001.SZ")) == 1
    assert raw_rows(env.conn, secucode="000002.SZ") == []
    assert len(version_rows(env.conn, secucode="000001.SZ")) == 1
    assert version_rows(env.conn, secucode="000002.SZ") == []


# ----------------------------------------------------------------------
# B31：提交失败 → 统计以已提交结果为准
# ----------------------------------------------------------------------
def test_b31_commit_failure_rolls_back_and_reports_failed(evidence_env, monkeypatch):
    env = evidence_env
    session = FakeSession({1: [FakeResponse(body=body_for([_row_for_run()], count=1))]})
    coordinator, _ = make_coordinator(env, session)

    armed = {"pending": False}
    original_finish = Repository.finish_unit

    def finish_then_arm(self, unit_id, status, **kwargs):
        original_finish(self, unit_id, status, **kwargs)
        if status == "success":
            armed["pending"] = True

    monkeypatch.setattr(Repository, "finish_unit", finish_then_arm)

    original_exit = repository_module._UnitOfWork.__exit__

    def failing_exit(self, exc_type, exc_val, exc_tb):
        if armed["pending"] and exc_type is None:
            armed["pending"] = False
            # 模拟提交失败：先按异常路径回滚，再向调用方报错
            original_exit(self, sqlite3.OperationalError, None, None)
            raise sqlite3.OperationalError("disk I/O error")
        return original_exit(self, exc_type, exc_val, exc_tb)

    monkeypatch.setattr(repository_module._UnitOfWork, "__exit__", failing_exit)

    result = coordinator.run([spec()])

    assert result.status == "failed"
    assert result.exit_code == 1
    assert result.units[0].status == "failed"
    assert count_rows(env.conn, "raw_financial_indicator") == 0
    assert count_rows(env.conn, "statement_version") == 0
    assert count_rows(env.conn, "statement_observation") == 0
    assert env.repo.get_watermark(FIN, SECUCODE) is None
    unit = env.repo.get_units_for_run(result.run_id)[0]
    assert unit["status"] == "failed"


# ----------------------------------------------------------------------
# B32：已有外层事务：请求前拒绝
# ----------------------------------------------------------------------
def test_b32_active_outer_transaction_rejected_before_request(evidence_env):
    env = evidence_env
    session = FakeSession({1: [FakeResponse(body=body_for([_row_for_run()]))]})
    coordinator, _ = make_coordinator(env, session)

    env.conn.execute("BEGIN")
    try:
        with pytest.raises(ActiveTransactionError):
            coordinator.run([spec()])
        assert session.calls == [], "拒绝发生在任何 GET 之前"
        assert count_rows(env.conn, "ingest_run") == 0
        assert count_rows(env.conn, "ingest_attempt") == 0
    finally:
        env.conn.rollback()

    # 外层事务结束后可正常运行
    result = coordinator.run([spec()])
    assert result.status == "success"


# ----------------------------------------------------------------------
# B33：真实接收 UTC 时间用于 observed/fetched，不借其填公告
# ----------------------------------------------------------------------
def test_b33_observed_at_is_real_receive_utc_for_first_and_repeat(
    evidence_env,
):
    env = evidence_env
    clock = StepClock(start="2026-10-17T08:00:00Z", step_seconds=7)
    row = fi_row(report_date="2024-03-31", notice_date="2024-04-20", income=100.0)

    coordinator1, _ = make_coordinator(
        env, FakeSession({1: [FakeResponse(body=body_for([row], count=1))]}),
        clock=clock,
    )
    assert coordinator1.run([spec()]).status == "success"
    version = version_rows(env.conn)[0]
    first_observed = version["observed_at"]
    first_snapshot = env.repo.get_snapshot(version["snapshot_id"])
    assert first_observed == first_snapshot["fetched_at"]
    assert first_observed.endswith("Z")

    coordinator2, _ = make_coordinator(
        env, FakeSession({1: [FakeResponse(body=body_for([row], count=1))]}),
        clock=clock,
    )
    assert coordinator2.run([spec()]).status == "success"

    observations = env.repo.get_observations_for_version(version["version_id"])
    assert len(observations) == 2
    second_snapshot = env.repo.get_snapshot(observations[1]["snapshot_id"])
    assert observations[0]["observed_at"] == first_observed
    assert observations[1]["observed_at"] == second_snapshot["fetched_at"]
    assert observations[1]["observed_at"] != observations[0]["observed_at"]
    assert version_of(env.conn, version["version_id"])["observed_at"] == first_observed
    assert_observed_matches_received(env.conn)


def test_b33_invalid_notice_is_unknown_and_never_backfilled(evidence_env):
    env = evidence_env
    clock = StepClock(start="2026-10-17T09:30:00Z")
    row = fi_row(
        report_date="2024-03-31", notice_date="not-a-real-date", income=100.0,
    )
    coordinator, _ = make_coordinator(
        env, FakeSession({1: [FakeResponse(body=body_for([row], count=1))]}),
        clock=clock,
    )

    result = coordinator.run([spec()])

    assert result.status == "success"
    version = version_rows(env.conn)[0]
    assert version["notice_date"] is None
    assert version["observed_at"].endswith("Z")
    observations = env.repo.get_observations_for_version(version["version_id"])
    diagnostics = json.loads(observations[0]["normalization_diagnostics"] or "[]")
    assert any(
        d["field"] == "notice_date" and d["original_value"] == "not-a-real-date"
        for d in diagnostics
    )
    assert env.repo.get_watermark(FIN, SECUCODE) is None, "无有效公告不推进水位"
    observation = observation_rows(env.conn)[0]
    assert observation["notice_date"] is None
    snapshot = env.repo.get_snapshot(observation["snapshot_id"])
    assert observation["observed_at"] == snapshot["fetched_at"]
    assert observation["observed_at"] != snapshot["fetched_at"].replace("T", " ")


# ----------------------------------------------------------------------
# 运行/单元身份与 request_scope（契约第 2 节）
# ----------------------------------------------------------------------
def test_run_and_unit_identity_and_request_scope(evidence_env):
    env = evidence_env
    session = FakeSession({1: [FakeResponse(body=body_for([_row_for_run()], count=1))]})
    coordinator, _ = make_coordinator(env, session)

    coordinator.run([spec(), spec()])  # 同一 spec 重复计划只请求一次

    assert len(session.calls) == 1
    run = env.conn.execute("SELECT * FROM ingest_run").fetchone()
    assert run["crawler_version"] == CRAWLER_VERSION
    assert run["started_at"].endswith("Z")
    units = env.repo.get_units_for_run(run["run_id"])
    assert len(units) == 1
    scope = json.loads(units[0]["request_scope"])
    assert scope["endpoint"] == "/api/data/v1/get"
    assert scope["report_name"] == FIN
    assert scope["page_size"] == 50
    assert scope["sort_columns"] == "REPORTDATE"
    assert scope["filters"] == {"SECUCODE": SECUCODE}
    assert scope["mapping_version"]
    assert "pageNumber" not in scope and "http_attempt" not in scope
    call = session.calls[0]
    assert call["params"]["pageNumber"] == "1"
    assert call["params"]["pageSize"] == "50"
    assert SECUCODE in call["params"]["filter"]
    assert "SECUCODE" in call["params"]["filter"]
