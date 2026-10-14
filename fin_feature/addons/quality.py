"""数据质量监控：对数据处理各环节输出质量报告。

指标：各表记录数、关键字段缺失率、数据量变化（与上次报告对比）。
结果写入 quality_report 表。
"""

from fin_feature.storage.repository import Repository
from fin_feature.utils.logger import setup_logger

logger = setup_logger("fin_feature.addons.quality")

# 各表需要监控缺失率的关键字段
KEY_FIELDS: dict[str, list[str]] = {
    "clean_financial_indicator": [
        "total_operate_income", "parent_netprofit", "weightavg_roe", "gross_margin",
    ],
    "clean_income_statement": [
        "total_operate_income", "parent_netprofit", "operate_profit",
    ],
    "clean_balance_sheet": [
        "total_assets", "total_liabilities", "total_equity", "debt_asset_ratio",
    ],
}


def _missing_rate(repo: Repository, table: str, field: str) -> float:
    """计算某表某字段的缺失率（NULL 占比）。"""
    total = repo.get_table_count(table)
    if total == 0:
        return 0.0
    sql = f"SELECT COUNT(*) FROM {table} WHERE {field} IS NULL"
    null_count = repo.conn.execute(sql).fetchone()[0]
    return null_count / total


def generate_report(repo: Repository) -> dict:
    """生成数据质量报告，写入 quality_report 表，返回报告摘要。"""

    report: dict[str, dict] = {}

    # 1. 各表记录数
    tables = [
        "raw_financial_indicator", "raw_income_statement", "raw_balance_sheet",
        "clean_financial_indicator", "clean_income_statement", "clean_balance_sheet",
        "features",
    ]
    for table in tables:
        count = repo.get_table_count(table)
        repo.save_quality_metric(table, "record_count", count)
        report.setdefault(table, {})["record_count"] = count

    # 2. 关键字段缺失率
    for table, fields in KEY_FIELDS.items():
        for field in fields:
            rate = _missing_rate(repo, table, field)
            repo.save_quality_metric(
                table, f"missing_rate:{field}", rate,
                detail=f"{field} 缺失率 {rate:.2%}",
            )
            report.setdefault(table, {})[f"missing_rate:{field}"] = rate

    # 3. 数据量变化（原始 vs 清洗）
    for suffix in ["financial_indicator", "income_statement", "balance_sheet"]:
        raw_table = f"raw_{suffix}"
        clean_table = f"clean_{suffix}"
        raw_count = repo.get_table_count(raw_table)
        clean_count = repo.get_table_count(clean_table)
        if raw_count > 0:
            retention = clean_count / raw_count
            repo.save_quality_metric(
                clean_table, "retention_rate", retention,
                detail=f"清洗保留率 {retention:.2%} (原始{raw_count}->清洗{clean_count})",
            )
            report.setdefault(clean_table, {})["retention_rate"] = retention

    # 4. 特征覆盖率（有值的特征占比）
    feature_total = repo.get_table_count("features")
    if feature_total > 0:
        sql = "SELECT COUNT(*) FROM features WHERE feature_value IS NOT NULL"
        non_null = repo.conn.execute(sql).fetchone()[0]
        coverage = non_null / feature_total
        repo.save_quality_metric(
            "features", "value_coverage", coverage,
            detail=f"特征值覆盖率 {coverage:.2%}",
        )
        report.setdefault("features", {})["value_coverage"] = coverage

    logger.info("数据质量报告已生成: %s", report)
    return report
