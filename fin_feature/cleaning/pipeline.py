"""清洗流水线：编排去重 → 异常值检测 → 缺失值处理 → 时间对齐。

对三张原始表分别执行清洗，结果写入对应的清洗表。
每步记录清洗日志，最终返回统计信息。
"""

from dataclasses import dataclass, field

from fin_feature.cleaning.dedup import deduplicate
from fin_feature.cleaning.missing import handle_missing
from fin_feature.cleaning.outliers import detect_and_fix
from fin_feature.cleaning.time_align import align_time
from fin_feature.storage.repository import Repository
from fin_feature.utils.logger import setup_logger

logger = setup_logger("fin_feature.cleaning.pipeline")

# 清洗表保留的字段（从原始记录中筛选）
# 字段必须与 schema.py 中清洗表的列名一致
CLEAN_FIELDS: dict[str, list[str]] = {
    "financial_indicator": [
        "secucode", "security_code", "security_name", "report_date",
        "notice_date", "qdate", "datatype", "report_type",
        "basic_eps", "total_operate_income", "parent_netprofit",
        "weightavg_roe", "revenue_yoy", "profit_yoy", "bps",
        "gross_margin", "revenue_qoq", "profit_qoq", "disclosure_status",
    ],
    "income_statement": [
        "secucode", "security_code", "security_name", "report_date",
        "notice_date", "report_type", "industry_name",
        "total_operate_income", "total_operate_cost", "operate_cost",
        "sale_expense", "manage_expense", "finance_expense",
        "operate_profit", "total_profit", "income_tax",
        "parent_netprofit", "deduct_parent_netprofit", "disclosure_status",
    ],
    "balance_sheet": [
        "secucode", "security_code", "security_name", "report_date",
        "notice_date", "report_type", "industry_name",
        "total_assets", "monetary_funds", "accounts_receivable", "inventory",
        "total_liabilities", "accounts_payable", "total_equity",
        "current_ratio", "debt_asset_ratio", "disclosure_status",
    ],
}


@dataclass
class CleanStats:
    """单张表的清洗统计。"""

    table_suffix: str
    raw_count: int = 0
    dedup_count: int = 0
    dropped_count: int = 0
    clean_count: int = 0
    actions: dict[str, int] = field(default_factory=dict)


def _project_fields(record: dict, table_suffix: str) -> dict:
    """从记录中筛选清洗表需要的字段，丢弃多余字段。"""
    fields = CLEAN_FIELDS[table_suffix]
    return {f: record.get(f) for f in fields}


def clean_table(
    repo: Repository,
    table_suffix: str,
    raw_records: list,
) -> CleanStats:
    """清洗单张表的全部原始记录。

    流程：去重 → 异常值检测 → 缺失值处理 → 时间对齐 → 写入清洗表

    R1 修复：全部清洗写入（日志、失效、upsert）在同一 unit_of_work 中提交，
    失败一起回滚，不残留部分清洗结果。
    """
    stats = CleanStats(table_suffix=table_suffix, raw_count=len(raw_records))

    with repo.unit_of_work():
        # 1. 去重
        deduped = deduplicate(repo, table_suffix, raw_records)
        stats.dedup_count = len(deduped)
        stats.actions["dedup"] = stats.raw_count - stats.dedup_count

        # 2-4. 逐条清洗
        clean_records: list[dict] = []
        for record in deduped:
            # 保存键用于 drop 时失效旧数据
            secucode = record.get("secucode", "")
            report_date = record.get("report_date", "")
            # 异常值检测与修正
            record = detect_and_fix(repo, table_suffix, record)
            # 缺失值处理（可能丢弃记录）
            record = handle_missing(repo, table_suffix, record)
            if record is None:
                stats.dropped_count += 1
                # stage1-v1: 最新输入被 drop，同步失效旧 clean 及依赖特征
                if secucode and report_date:
                    repo.invalidate_clean(table_suffix, secucode, report_date)
                    repo.invalidate_features(secucode, report_date)
                    repo.log_clean_action(
                        secucode, report_date, table_suffix, "invalidated_on_drop",
                        "核心字段缺失，记录被 drop，失效旧 clean 及依赖特征",
                    )
                continue
            # 时间对齐
            record = align_time(repo, table_suffix, record)
            # 筛选清洗表字段
            clean_record = _project_fields(record, table_suffix)
            clean_records.append(clean_record)

        stats.clean_count = len(clean_records)

        # 写入清洗表
        for record in clean_records:
            repo.upsert_clean(table_suffix, record)

    logger.info(
        "表 %s 清洗完成: 原始 %d -> 去重 %d -> 丢弃 %d -> 清洗后 %d",
        table_suffix, stats.raw_count, stats.dedup_count,
        stats.dropped_count, stats.clean_count,
    )
    return stats


def run_cleaning(repo: Repository, config: dict | None = None) -> list[CleanStats]:
    """执行全部三张表的清洗。

    从原始表读取数据，清洗后写入清洗表。
    config: 清洗配置（cleaning 段），用于覆盖默认规则
    """
    from fin_feature.cleaning.rules import load_rules_config
    from fin_feature.constants import RAW_TABLES

    # 加载配置覆盖默认规则
    if config:
        load_rules_config(config)
        logger.info("已加载清洗配置覆盖默认规则")

    all_stats: list[CleanStats] = []
    for table_suffix in ["financial_indicator", "income_statement", "balance_sheet"]:
        raw_table = RAW_TABLES[table_suffix]
        raw_records = repo.conn.execute(
            f"SELECT * FROM {raw_table} ORDER BY secucode, report_date, notice_date"
        ).fetchall()
        if not raw_records:
            logger.warning("表 %s 无原始数据，跳过清洗", table_suffix)
            continue
        stats = clean_table(repo, table_suffix, raw_records)
        all_stats.append(stats)

    return all_stats
