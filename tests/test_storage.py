"""存储模块测试：DDL 建表、upsert 去重、按维度查询、水位读写。"""

from fin_feature.storage.repository import Repository


def test_schema_initialized(db):
    """init_schema 应创建所有表。"""
    tables = db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    table_names = [t[0] for t in tables]
    assert "stock_list" in table_names
    assert "raw_financial_indicator" in table_names
    assert "clean_income_statement" in table_names
    assert "features" in table_names
    assert "clean_log" in table_names
    assert "crawl_watermark" in table_names


def test_upsert_stock_list(repo):
    """股票列表应正确写入。"""
    records = [
        {"secucode": "002501.SZ", "security_code": "002501", "security_name": "利源", "market": "SZ"},
        {"secucode": "600000.SH", "security_code": "600000", "security_name": "浦发", "market": "SH"},
    ]
    count = repo.upsert_stock_list(records)
    assert count == 2
    assert repo.get_table_count("stock_list") == 2


def test_batch_upsert_raw(repo):
    """原始数据应正确批量写入。"""
    records = [
        {"secucode": "002501.SZ", "report_date": "2024-12-31", "notice_date": "2025-04-29",
         "total_operate_income": 1000.0, "parent_netprofit": 200.0},
        {"secucode": "002501.SZ", "report_date": "2024-09-30", "notice_date": "2024-10-30",
         "total_operate_income": 800.0, "parent_netprofit": 150.0},
    ]
    count = repo.batch_upsert_raw("financial_indicator", records)
    assert count == 2
    assert repo.get_table_count("raw_financial_indicator") == 2


def test_upsert_raw_replaces_on_duplicate(repo):
    """相同主键应替换（upsert 幂等）。"""
    record1 = {"secucode": "002501.SZ", "report_date": "2024-12-31", "notice_date": "2025-04-29",
               "total_operate_income": 1000.0}
    record2 = {"secucode": "002501.SZ", "report_date": "2024-12-31", "notice_date": "2025-04-29",
               "total_operate_income": 1200.0}  # 修订值
    repo.batch_upsert_raw("financial_indicator", [record1])
    repo.batch_upsert_raw("financial_indicator", [record2])
    assert repo.get_table_count("raw_financial_indicator") == 1
    rows = repo.get_raw_by_company("financial_indicator", "002501.SZ")
    assert rows[0]["total_operate_income"] == 1200.0


def test_get_raw_by_date_range(repo):
    """按日期范围查询应正确过滤。"""
    records = [
        {"secucode": "002501.SZ", "report_date": "2024-12-31", "notice_date": "2025-04-29"},
        {"secucode": "002501.SZ", "report_date": "2024-09-30", "notice_date": "2024-10-30"},
        {"secucode": "002501.SZ", "report_date": "2024-06-30", "notice_date": "2024-08-29"},
    ]
    repo.batch_upsert_raw("financial_indicator", records)
    rows = repo.get_raw_by_date_range("financial_indicator", "2024-07-01", "2024-12-31")
    assert len(rows) == 2  # 2024-09-30 和 2024-12-31


def test_upsert_clean(repo):
    """清洗数据应正确写入并按主键去重。"""
    repo.upsert_clean("income_statement", {
        "secucode": "002501.SZ", "report_date": "2024-12-31",
        "total_operate_income": 1000.0, "parent_netprofit": 200.0,
    })
    repo.upsert_clean("income_statement", {
        "secucode": "002501.SZ", "report_date": "2024-12-31",
        "total_operate_income": 1100.0, "parent_netprofit": 220.0,
    })
    assert repo.get_table_count("clean_income_statement") == 1
    row = repo.get_clean_record("income_statement", "002501.SZ", "2024-12-31")
    assert row["total_operate_income"] == 1100.0


def test_feature_upsert_and_query(repo):
    """特征应正确写入和查询。"""
    repo.upsert_feature("002501.SZ", "2024-12-31", "roe_change", 2.5, "profitability")
    repo.upsert_feature("002501.SZ", "2024-12-31", "revenue_yoy", 0.15, "growth")
    features = repo.get_features("002501.SZ", "2024-12-31")
    assert len(features) == 2
    by_group = repo.get_features("002501.SZ", feature_group="profitability")
    assert len(by_group) == 1
    assert by_group[0]["feature_name"] == "roe_change"


def test_watermark(repo):
    """增量水位应正确读写和更新。"""
    assert repo.get_watermark("RPT_LICO_FN_CPD", "002501.SZ") is None
    repo.update_watermark("RPT_LICO_FN_CPD", "002501.SZ", "2025-04-29")
    assert repo.get_watermark("RPT_LICO_FN_CPD", "002501.SZ") == "2025-04-29"
    repo.update_watermark("RPT_LICO_FN_CPD", "002501.SZ", "2025-10-30")
    assert repo.get_watermark("RPT_LICO_FN_CPD", "002501.SZ") == "2025-10-30"


def test_clean_log(repo):
    """清洗日志应正确写入。"""
    repo.log_clean_action("002501.SZ", "2024-12-31", "financial_indicator",
                          "dedup_replace", "旧版本被替换")
    logs = repo.get_clean_logs(limit=10)
    assert len(logs) == 1
    assert logs[0]["action"] == "dedup_replace"
