"""清洗模块测试：去重、异常值检测、缺失值处理、时间对齐。

测试基于 stage1-v1 标准单位阈值。
"""

from fin_feature.cleaning.dedup import deduplicate
from fin_feature.cleaning.missing import handle_missing
from fin_feature.cleaning.outliers import detect_and_fix
from fin_feature.cleaning.pipeline import clean_table
from fin_feature.cleaning.time_align import align_time
from fin_feature.storage.repository import Repository


def _make_row(**kwargs):
    """构造 sqlite3.Row 的 mock（用 dict 模拟，deduplicate 接受 list[dict] 也可）。

    deduplicate 接受 sqlite3.Row 列表，但内部 dict(row) 转换。
    这里直接用 dict 列表测试逻辑。
    """
    return kwargs


class _FakeRow:
    """模拟 sqlite3.Row，支持 dict() 转换。"""

    def __init__(self, data: dict):
        self._data = data

    def keys(self):
        return self._data.keys()

    def __getitem__(self, key):
        return self._data[key]


def test_dedup_keeps_latest_notice_date(repo):
    """去重应保留 notice_date 最新的版本。"""
    raw_records = [
        _FakeRow({"secucode": "002501.SZ", "report_date": "2024-12-31",
                  "notice_date": "2025-04-29", "total_operate_income": 1000.0}),
        _FakeRow({"secucode": "002501.SZ", "report_date": "2024-12-31",
                  "notice_date": "2025-04-30", "total_operate_income": 1200.0}),  # 修订版
        _FakeRow({"secucode": "002501.SZ", "report_date": "2024-09-30",
                  "notice_date": "2024-10-30", "total_operate_income": 800.0}),
    ]
    result = deduplicate(repo, "financial_indicator", raw_records)
    assert len(result) == 2  # 两个报告期各一条
    # 2024-12-31 应保留 notice_date=2025-04-30 的版本
    dec = [r for r in result if r["report_date"] == "2024-12-31"][0]
    assert dec["total_operate_income"] == 1200.0


def test_outlier_threshold(repo):
    """超出阈值的字段应记日志但保留原值（stage1-v1）。

    stage1-v1: 标准化后判断阈值；超标只记 clean_log，不清空原值。
    阈值（标准单位：小数）：
    - weightavg_roe: [-2.0, 2.0]
    - gross_margin: [-1.0, 1.0]
    """
    # 原百分值 5.0（小数 0.05）在阈值内，应保留
    record = {
        "secucode": "002501.SZ", "report_date": "2024-12-31",
        "weightavg_roe": 5.0,    # 百分值 5%，标准化 0.05，在 [-2,2] 内
        "gross_margin": 1.5,     # 百分值 1.5%，标准化 0.015，在 [-1,1] 内
    }
    fixed = detect_and_fix(repo, "financial_indicator", record)
    # 原值保留，不清空
    assert fixed["weightavg_roe"] == 5.0
    assert fixed["gross_margin"] == 1.5

    # 真正超标的值：原百分值 500.0（标准化 5.0）超出 [-2,2]
    record_big = {
        "secucode": "002501.SZ", "report_date": "2024-12-31",
        "weightavg_roe": 500.0,  # 标准化 5.0，超出 [-2.0, 2.0]
        "gross_margin": 150.0,   # 标准化 1.5，超出 [-1.0, 1.0]
    }
    fixed_big = detect_and_fix(repo, "financial_indicator", record_big)
    # 超标仍保留原值（只记日志）
    assert fixed_big["weightavg_roe"] == 500.0
    assert fixed_big["gross_margin"] == 150.0
    # 验证 clean_log 有诊断记录
    logs = repo.get_clean_logs()
    outlier_logs = [l for l in logs if l["action"] == "outlier_threshold"]
    assert len(outlier_logs) >= 2  # 两个字段都记了日志

    # 正常值应保留
    record2 = {
        "secucode": "002501.SZ", "report_date": "2024-12-31",
        "weightavg_roe": 0.15,   # 15%，正常
        "gross_margin": 0.30,    # 30%，正常
    }
    fixed2 = detect_and_fix(repo, "financial_indicator", record2)
    assert fixed2["weightavg_roe"] == 0.15
    assert fixed2["gross_margin"] == 0.30


def test_outlier_negative_on_positive_field(repo):
    """应为正的字段出现负值应记日志但保留原值（stage1-v1）。"""
    record = {
        "secucode": "002501.SZ", "report_date": "2024-12-31",
        "total_assets": -1000.0,  # 总资产不应为负
    }
    fixed = detect_and_fix(repo, "balance_sheet", record)
    # 原值保留（只记日志）
    assert fixed["total_assets"] == -1000.0
    # 验证 clean_log 有诊断（阈值或负值检查均可触发）
    logs = repo.get_clean_logs()
    diag_logs = [l for l in logs if l["action"] in ("outlier_threshold", "outlier_negative")]
    assert len(diag_logs) >= 1


def test_missing_drop(repo):
    """核心字段缺失应丢弃记录。"""
    record = {
        "secucode": "002501.SZ", "report_date": "2024-12-31",
        "total_operate_income": None,  # 核心字段缺失
        "parent_netprofit": 200.0,
    }
    result = handle_missing(repo, "financial_indicator", record)
    assert result is None  # 被丢弃


def test_missing_keep(repo):
    """非核心字段缺失应保留 NULL。"""
    record = {
        "secucode": "002501.SZ", "report_date": "2024-12-31",
        "total_operate_income": 1000.0,
        "parent_netprofit": 200.0,
        "weightavg_roe": None,  # 非核心，保留
    }
    result = handle_missing(repo, "financial_indicator", record)
    assert result is not None
    assert result["weightavg_roe"] is None


def test_time_align_on_time(repo):
    """预期窗口内披露应标记 on_time。"""
    record = {
        "secucode": "002501.SZ", "report_date": "2024-12-31",
        "notice_date": "2025-04-29",  # 年报次年 4 月，在窗口内
    }
    result = align_time(repo, "financial_indicator", record)
    assert result["disclosure_status"] == "on_time"
    assert result["report_type"] == "annual"


def test_time_align_delayed(repo):
    """晚于窗口披露应标记 delayed。"""
    record = {
        "secucode": "002501.SZ", "report_date": "2024-12-31",
        "notice_date": "2025-06-15",  # 年报应在 1-4 月，6 月属延迟
    }
    result = align_time(repo, "financial_indicator", record)
    assert result["disclosure_status"] == "delayed"


def test_time_align_not_disclosed(repo):
    """无公告日期应标记 not_disclosed。"""
    record = {
        "secucode": "002501.SZ", "report_date": "2024-12-31",
        "notice_date": None,
    }
    result = align_time(repo, "financial_indicator", record)
    assert result["disclosure_status"] == "not_disclosed"


def test_clean_table_pipeline(repo):
    """完整清洗流水线应正确处理数据。"""
    raw_records = [
        _FakeRow({"secucode": "002501.SZ", "report_date": "2024-12-31",
                  "notice_date": "2025-04-29",
                  "total_operate_income": 1000.0, "parent_netprofit": 200.0,
                  "weightavg_roe": 0.15, "gross_margin": 0.30}),
        _FakeRow({"secucode": "002501.SZ", "report_date": "2024-12-31",
                  "notice_date": "2025-04-30",  # 修订版
                  "total_operate_income": 1100.0, "parent_netprofit": 220.0,
                  "weightavg_roe": 0.16, "gross_margin": 0.31}),
    ]
    stats = clean_table(repo, "financial_indicator", raw_records)
    assert stats.raw_count == 2
    assert stats.dedup_count == 1  # 去重后 1 条
    assert stats.clean_count == 1
    # 验证清洗表写入
    row = repo.get_clean_record("financial_indicator", "002501.SZ", "2024-12-31")
    assert row is not None
    assert row["total_operate_income"] == 1100.0  # 保留修订版


def test_drop_invalidates_old_clean_and_features(repo):
    """P1 #4: 最新输入被 drop 后旧 clean 和特征应被失效。

    场景：已有旧 clean 记录和特征，新原始数据核心字段缺失被 drop，
    旧 clean 和特征应被删除，避免陈旧值残留。
    """
    # 1. 先写入旧 clean 记录和特征
    repo.upsert_clean("financial_indicator", {
        "secucode": "002501.SZ", "report_date": "2024-12-31",
        "total_operate_income": 1000.0, "parent_netprofit": 200.0,
        "weightavg_roe": 0.15,
    })
    repo.upsert_feature("002501.SZ", "2024-12-31", "roe_change", 2.0, "profitability")

    # 确认旧数据存在
    assert repo.get_clean_record("financial_indicator", "002501.SZ", "2024-12-31") is not None
    assert len(repo.get_features("002501.SZ", "2024-12-31")) >= 1

    # 2. 新原始数据核心字段缺失 → 被 drop
    raw_records = [
        _FakeRow({"secucode": "002501.SZ", "report_date": "2024-12-31",
                  "notice_date": "2025-05-01",
                  "total_operate_income": None,  # 核心字段缺失
                  "parent_netprofit": 200.0}),
    ]
    stats = clean_table(repo, "financial_indicator", raw_records)
    assert stats.dropped_count == 1

    # 3. 旧 clean 和特征应被失效（删除）
    assert repo.get_clean_record("financial_indicator", "002501.SZ", "2024-12-31") is None
    assert len(repo.get_features("002501.SZ", "2024-12-31")) == 0

    # 4. clean_log 应有失效记录
    logs = repo.get_clean_logs()
    invalidation_logs = [l for l in logs if l["action"] == "invalidated_on_drop"]
    assert len(invalidation_logs) >= 1
