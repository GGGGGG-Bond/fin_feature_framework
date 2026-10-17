"""2B-2 执行器与分页证据测试（B16-B22、B24 定位部分）。

真实临时库 + 真实临时 raw_root；HTTP 由 FakeSession 脚本化，不访问网络。
"""

from __future__ import annotations

import sqlite3

import pytest
import requests

from fin_feature.ingestion.capture import (
    BusinessFailure,
    DecodeFailure,
    HttpStatusFailure,
    IncompleteScope,
    SchemaFailure,
    StorageFailure,
    TransportFailure,
)
from fin_feature.ingestion.http_evidence import EvidenceRequestExecutor
from fin_feature.ingestion.pagination import fetch_financial_pages
from fin_feature.storage.raw_store import RawStoreError

from ingestion_helpers import (
    FIN,
    URL,
    FakeResponse,
    FakeSession,
    SleepRecorder,
    StepClock,
    body_for,
    fi_row,
)


def make_executor(env, session, *, max_retries=3, clock=None, sleep=None):
    return EvidenceRequestExecutor(
        repository=env.repo,
        raw_store=env.store,
        session=session,
        url=URL,
        timeout=15,
        max_retries=max_retries,
        sleep=sleep or SleepRecorder(),
        clock=clock or StepClock(),
    )


def row_params(page=1):
    return {
        "reportName": FIN,
        "columns": "ALL",
        "pageSize": "50",
        "pageNumber": str(page),
        "sortColumns": "REPORTDATE",
        "sortTypes": "-1",
    }


def snapshot_body(env, snapshot):
    stored = env.store.verify({
        "file_path": snapshot["file_path"],
        "raw_hash": snapshot["raw_content_hash"],
        "raw_bytes": snapshot["raw_bytes"],
    })
    return stored.body


def unit_with_run(env):
    env.new_run()
    return env.new_unit()


def snapshot_for_attempt(env, attempt_id):
    return env.repo.get_snapshot_for_attempt(attempt_id)


# ----------------------------------------------------------------------
# B16：网络无响应 → 500 body → 200 body
# ----------------------------------------------------------------------
def test_b16_retry_sequence_keeps_each_attempt_evidence(evidence_env):
    env = evidence_env
    unit = unit_with_run(env)
    error_body = b"<html>upstream error</html>"
    ok_body = body_for([fi_row()], pages=1, count=1)
    session = FakeSession({1: [
        requests.ConnectionError("no route"),
        FakeResponse(status_code=500, body=error_body),
        FakeResponse(body=ok_body, headers={"Content-Type": "application/json"}, encoding="utf-8"),
    ]})
    sleep = SleepRecorder()
    executor = make_executor(env, session, max_retries=3, sleep=sleep)

    page = executor.fetch_page(
        unit, page_number=1, params={**row_params(1), "Cookie": "secret"},
    )

    assert session.requested_pages == [1, 1, 1]
    assert sleep.calls == [2, 4]

    attempts = env.repo.get_attempts_for_unit(unit["unit_id"])
    assert [(a["http_attempt"], a["outcome"]) for a in attempts] == [
        (1, "transport_error"), (2, "http_error"), (3, "ok"),
    ]
    # 无响应：没有快照、没有文件；有响应：每次原体都留存
    assert snapshot_for_attempt(env, attempts[0]["attempt_id"]) is None
    failed_snapshot = snapshot_for_attempt(env, attempts[1]["attempt_id"])
    assert failed_snapshot["validation_status"] == "invalid"
    assert snapshot_body(env, failed_snapshot) == error_body
    ok_snapshot = snapshot_for_attempt(env, attempts[2]["attempt_id"])
    assert ok_snapshot["validation_status"] == "valid"
    assert snapshot_body(env, ok_snapshot) == ok_body

    # 只有最终成功页可作业务输入
    assert page.attempt_id == attempts[2]["attempt_id"]
    assert page.snapshot_id == ok_snapshot["snapshot_id"]
    assert page.fetched_at == ok_snapshot["fetched_at"]
    assert page.http_status == 200
    assert page.source_rows == 1
    # request_params 只保留业务白名单，不含 Cookie/Authorization
    assert "Cookie" not in ok_snapshot["request_params"]
    assert "Authorization" not in ok_snapshot["request_params"]
    assert '"pageNumber": "1"' in ok_snapshot["request_params"]


def test_b16_transport_failures_exhaust_attempts(evidence_env):
    env = evidence_env
    unit = unit_with_run(env)
    session = FakeSession({1: [requests.Timeout("slow")]})
    sleep = SleepRecorder()
    executor = make_executor(env, session, max_retries=2, sleep=sleep)

    with pytest.raises(TransportFailure) as excinfo:
        executor.fetch_page(unit, page_number=1, params=row_params(1))

    assert session.requested_pages == [1, 1]
    assert sleep.calls == [2]
    attempts = env.repo.get_attempts_for_unit(unit["unit_id"])
    assert [(a["http_attempt"], a["outcome"]) for a in attempts] == [
        (1, "transport_error"), (2, "transport_error"),
    ]
    assert all(snapshot_for_attempt(env, a["attempt_id"]) is None for a in attempts)
    assert excinfo.value.page_number == 1
    assert excinfo.value.http_attempt == 2


# ----------------------------------------------------------------------
# B17：业务失败 / 非法 JSON / 结构错误：先留原体再报错、不重试
# ----------------------------------------------------------------------
@pytest.mark.parametrize("case,body,error_type,outcome", [
    ("business", body_for([], success=False, message="无数据权限"),
     BusinessFailure, "business_error"),
    ("decode", b"<html>not json</html>", DecodeFailure, "decode_error"),
    ("schema", body_for(None, result={"pages": 1, "data": "oops"}),
     SchemaFailure, "schema_error"),
])
def test_b17_envelope_failures_store_body_first(
    evidence_env, case, body, error_type, outcome,
):
    env = evidence_env
    unit = unit_with_run(env)
    session = FakeSession({1: [FakeResponse(body=body)]})
    sleep = SleepRecorder()
    executor = make_executor(env, session, max_retries=3, sleep=sleep)

    with pytest.raises(error_type) as excinfo:
        executor.fetch_page(unit, page_number=1, params=row_params(1))

    assert session.requested_pages == [1], "业务/解码/结构失败不应重试"
    assert sleep.calls == []
    attempts = env.repo.get_attempts_for_unit(unit["unit_id"])
    assert len(attempts) == 1 and attempts[0]["outcome"] == outcome
    snapshot = snapshot_for_attempt(env, attempts[0]["attempt_id"])
    assert snapshot is not None, "失败响应也必须先留原体"
    assert snapshot["validation_status"] == "invalid"
    assert snapshot_body(env, snapshot) == body
    assert excinfo.value.snapshot_id == snapshot["snapshot_id"]
    assert body.decode("utf-8", "replace") not in str(excinfo.value)


# ----------------------------------------------------------------------
# B18：RawStore / 审计写入失败：不重试、不继续
# ----------------------------------------------------------------------
def test_b18_audit_write_failure_stops_without_retry(evidence_env, monkeypatch):
    env = evidence_env
    unit = unit_with_run(env)
    session = FakeSession({1: [FakeResponse(body=body_for([fi_row()]))]})
    sleep = SleepRecorder()
    executor = make_executor(env, session, max_retries=3, sleep=sleep)

    def broken_save(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(env.repo, "save_raw_snapshot", broken_save)

    with pytest.raises(StorageFailure):
        executor.fetch_page(unit, page_number=1, params=row_params(1))

    assert session.requested_pages == [1]
    assert sleep.calls == []
    attempts = env.repo.get_attempts_for_unit(unit["unit_id"])
    assert [(a["http_attempt"], a["outcome"]) for a in attempts] == [
        (1, "storage_error"),
    ]


def test_b18_raw_store_publish_failure_stops_without_retry(
    evidence_env, monkeypatch,
):
    env = evidence_env
    unit = unit_with_run(env)
    session = FakeSession({1: [FakeResponse(body=body_for([fi_row()]))]})
    executor = make_executor(env, session, max_retries=3)

    def broken_publish(context, body):
        raise RawStoreError("无法创建证据文件")

    monkeypatch.setattr(env.store, "publish", broken_publish)

    with pytest.raises(StorageFailure):
        executor.fetch_page(unit, page_number=1, params=row_params(1))

    assert session.requested_pages == [1]
    attempts = env.repo.get_attempts_for_unit(unit["unit_id"])
    assert [(a["http_attempt"], a["outcome"]) for a in attempts] == [
        (1, "storage_error"),
    ]
    assert all(snapshot_for_attempt(env, a["attempt_id"]) is None for a in attempts)


# ----------------------------------------------------------------------
# B19：多页正常成功
# ----------------------------------------------------------------------
def test_b19_multi_page_locators_and_count(evidence_env):
    env = evidence_env
    unit = unit_with_run(env)
    first_rows = [
        fi_row(report_date="2024-03-31", income=1.0),
        fi_row(report_date="2023-12-31", income=2.0),
    ]
    second_rows = [fi_row(report_date="2023-09-30", income=3.0)]
    first_body = body_for(first_rows, pages=2, count=3)
    second_body = body_for(second_rows, pages=2, count=3)
    session = FakeSession({
        1: [FakeResponse(body=first_body)],
        2: [FakeResponse(body=second_body)],
    })
    executor = make_executor(env, session)

    result = fetch_financial_pages(
        executor, unit=unit, params_builder=row_params, max_pages_per_unit=10,
    )

    assert result.complete is True
    assert result.declared_pages == 2
    assert result.source_row_count == 3
    assert len(result.pages) == 2
    assert [r.page_number for r in result.records] == [1, 1, 2]
    assert [r.row_index for r in result.records] == [0, 1, 0]
    # 每行的发生身份可回原页重建
    for record in result.records:
        assert record.snapshot_id == result.pages[record.page_number - 1].snapshot_id
        assert record.value is result.pages[record.page_number - 1].rows[record.row_index]
    # 所有页文件均真实存在且与响应字节一致
    expected_bodies = {1: first_body, 2: second_body}
    for page in result.pages:
        snapshot = env.repo.get_snapshot(page.snapshot_id)
        assert snapshot_body(env, snapshot) == expected_bodies[page.page_number]


# ----------------------------------------------------------------------
# B20：后续页失败 / 页数变化 / count 变化 / 早空页
# ----------------------------------------------------------------------
@pytest.mark.parametrize("case,page2_response,error_type", [
    ("http_500", FakeResponse(status_code=500, body=b"{}"), HttpStatusFailure),
    ("pages_changed",
     FakeResponse(body=body_for([fi_row(income=3.0)], pages=3, count=3)),
     SchemaFailure),
    ("count_changed",
     FakeResponse(body=body_for([fi_row(income=3.0)], pages=2, count=99)),
     SchemaFailure),
    ("empty_second_page",
     FakeResponse(body=body_for([], pages=2, count=2)),
     SchemaFailure),
])
def test_b20_second_page_failure_keeps_prior_evidence(
    evidence_env, case, page2_response, error_type,
):
    env = evidence_env
    unit = unit_with_run(env)
    first_rows = [
        fi_row(report_date="2024-03-31", income=1.0),
        fi_row(report_date="2023-12-31", income=2.0),
    ]
    first_body = body_for(first_rows, pages=2, count=3)
    session = FakeSession({
        1: [FakeResponse(body=first_body)],
        2: [page2_response],
    })
    executor = make_executor(env, session, max_retries=2)

    with pytest.raises(error_type):
        fetch_financial_pages(
            executor, unit=unit, params_builder=row_params, max_pages_per_unit=10,
        )

    assert session.requested_pages[0] == 1
    # 第一页文件/审计仍保留（失败不删除证据）
    first_snapshot = snapshot_for_attempt(
        env, env.repo.get_attempts_for_unit(unit["unit_id"])[0]["attempt_id"]
    )
    assert first_snapshot["validation_status"] == "valid"
    assert snapshot_body(env, first_snapshot) == first_body
    # 第二页每次收到响应都留原体；无论页校验是否通过都不能让单元成功
    second_attempts = [
        a for a in env.repo.get_attempts_for_unit(unit["unit_id"])
        if a["page_number"] == 2
    ]
    assert second_attempts, "第二页必须留下真实尝试"
    assert any(
        snapshot_for_attempt(env, a["attempt_id"]) is not None
        for a in second_attempts
    )


def test_b20_pagination_failure_returns_no_partial_result(evidence_env):
    env = evidence_env
    unit = unit_with_run(env)
    session = FakeSession({
        1: [FakeResponse(body=body_for([fi_row()], pages=2, count=2))],
        2: [FakeResponse(status_code=500, body=b"{}")],
    })
    executor = make_executor(env, session, max_retries=1)

    with pytest.raises(HttpStatusFailure):
        fetch_financial_pages(
            executor, unit=unit, params_builder=row_params, max_pages_per_unit=10,
        )
    # 异常路径没有可被误当成功的 FetchResult；第一页记录未返回


# ----------------------------------------------------------------------
# B21：非法类型 / pages=0 非空 / 非对象行
# ----------------------------------------------------------------------
@pytest.mark.parametrize("case,response", [
    ("pages_bool", FakeResponse(body=body_for(
        None, result={"pages": True, "data": [fi_row()], "count": 1}))),
    ("missing_pages", FakeResponse(body=body_for(
        None, result={"data": [fi_row()], "count": 1}))),
    ("data_not_list", FakeResponse(body=body_for(
        None, result={"pages": 1, "data": {"x": 1}, "count": 1}))),
    ("success_string", FakeResponse(body=body_for(
        [fi_row()], success="true"))),
    ("pages_zero_nonempty", FakeResponse(body=body_for(
        None, result={"pages": 0, "data": [fi_row()], "count": 1}))),
    ("non_object_row", FakeResponse(body=body_for(
        None, result={"pages": 1, "data": [fi_row(), "x"], "count": 2}))),
])
def test_b21_invalid_envelopes_fail_explicitly(evidence_env, case, response):
    env = evidence_env
    unit = unit_with_run(env)
    session = FakeSession({1: [response]})
    executor = make_executor(env, session)

    with pytest.raises((SchemaFailure, BusinessFailure)):
        executor.fetch_page(unit, page_number=1, params=row_params(1))

    assert session.requested_pages == [1]
    attempts = env.repo.get_attempts_for_unit(unit["unit_id"])
    assert len(attempts) == 1
    snapshot = snapshot_for_attempt(env, attempts[0]["attempt_id"])
    assert snapshot is not None and snapshot["validation_status"] == "invalid"


def test_b21_empty_result_is_not_treated_as_missing(evidence_env):
    """缺 data 字段不能被当作合法空列表。"""
    env = evidence_env
    unit = unit_with_run(env)
    session = FakeSession({1: [FakeResponse(body=body_for(None, result={"pages": 1}))]})
    executor = make_executor(env, session)

    with pytest.raises(SchemaFailure):
        executor.fetch_page(unit, page_number=1, params=row_params(1))


# ----------------------------------------------------------------------
# B22：安全上限截断
# ----------------------------------------------------------------------
def test_b22_declared_pages_over_limit_is_incomplete_scope(evidence_env):
    env = evidence_env
    unit = unit_with_run(env)
    session = FakeSession({
        1: [FakeResponse(body=body_for([fi_row()], pages=5, count=5))],
    })
    executor = make_executor(env, session)

    with pytest.raises(IncompleteScope) as excinfo:
        fetch_financial_pages(
            executor, unit=unit, params_builder=row_params, max_pages_per_unit=2,
        )

    assert session.requested_pages == [1], "预知超限后不继续请求"
    assert excinfo.value.snapshot_id is not None
    snapshot = env.repo.get_snapshot(excinfo.value.snapshot_id)
    assert snapshot["validation_status"] == "valid", "首响应证据保留"
    # 没有完整结果，调用方无法从异常得到可当成功的 records
    assert excinfo.value.code == "incomplete_scope"


# ----------------------------------------------------------------------
# B24：重复行的发生身份不靠值相等推断
# ----------------------------------------------------------------------
def test_b24_duplicate_rows_keep_their_own_locators(evidence_env):
    env = evidence_env
    unit = unit_with_run(env)
    duplicated = fi_row(report_date="2024-03-31", income=1.0)
    session = FakeSession({
        1: [FakeResponse(body=body_for(
            [duplicated, dict(duplicated)], pages=2, count=3))],
        2: [FakeResponse(body=body_for([duplicated], pages=2, count=3))],
    })
    executor = make_executor(env, session)

    result = fetch_financial_pages(
        executor, unit=unit, params_builder=row_params, max_pages_per_unit=10,
    )

    assert len(result.records) == 3
    locators = {(r.snapshot_id, r.row_index) for r in result.records}
    assert len(locators) == 3, "每行出现都有独立 locator"
    assert [r.row_index for r in result.records] == [0, 1, 0]
    assert result.records[0].value == result.records[1].value
    assert result.records[0].snapshot_id != result.records[2].snapshot_id
