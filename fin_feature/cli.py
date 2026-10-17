"""CLI 入口：爬取/清洗/特征/质量/增量/回测 六个子命令。

用法：
    python -m fin_feature.cli crawl
    python -m fin_feature.cli clean
    python -m fin_feature.cli features
    python -m fin_feature.cli quality
    python -m fin_feature.cli incremental
    python -m fin_feature.cli backtest --feature roe_change --date 2024-12-31
"""

import argparse
import sys
from datetime import datetime

from fin_feature.config import load_config
from fin_feature.storage.database import Database
from fin_feature.storage.field_mapping import map_fields
from fin_feature.storage.repository import Repository
from fin_feature.utils.logger import setup_logger


def _get_db_and_repo(config):
    """初始化数据库并返回 (Database, Repository)。"""
    db = Database(config.db_path)
    db.init()
    return db, Repository(db.conn)


def cmd_crawl(args) -> None:
    """爬取数据：股票列表 + 三张报表。"""
    config = load_config(args.config)
    logger = setup_logger(level=config.logging.get("level", "INFO"),
                          log_file=config.logging.get("file"))

    from fin_feature.constants import (
        REPORT_BALANCE_SHEET,
        REPORT_FINANCIAL_INDICATOR,
        REPORT_INCOME_STATEMENT,
    )
    from fin_feature.crawler.eastmoney import EastMoneyCrawler
    from fin_feature.crawler.stock_list import StockListCrawler

    db, repo = _get_db_and_repo(config)

    # 1. 爬取股票列表
    stock_crawler = StockListCrawler(
        stock_list_url=config.crawler.get("stock_list_url"),
        timeout=config.crawler.get("timeout", 15),
        min_interval=config.crawler.get("min_interval", 1.5),
        user_agent=config.crawler.get("user_agent"),
    )
    stock_count = config.crawl.get("stock_count", 50)
    logger.info("开始爬取 %d 只股票列表...", stock_count)
    stocks = stock_crawler.fetch_stock_list(stock_count)
    with repo.unit_of_work():
        repo.upsert_stock_list(stocks)
    logger.info("股票列表爬取完成：%d 只", len(stocks))

    # 2. 爬取三张报表
    em_crawler = EastMoneyCrawler(
        base_url=config.crawler.get("base_url"),
        page_size=config.crawler.get("page_size", 50),
        timeout=config.crawler.get("timeout", 15),
        min_interval=config.crawler.get("min_interval", 1.5),
        max_retries=config.crawler.get("max_retries", 3),
        user_agent=config.crawler.get("user_agent"),
        referer=config.crawler.get("referer", ""),
    )

    report_map = {
        REPORT_FINANCIAL_INDICATOR: "financial_indicator",
        REPORT_INCOME_STATEMENT: "income_statement",
        REPORT_BALANCE_SHEET: "balance_sheet",
    }

    secucodes = [s["secucode"] for s in stocks]
    # 读取 recent_periods 配置
    recent_periods = config.crawl.get("recent_periods")
    from fin_feature.addons.incremental import select_recent_periods, validate_recent_periods
    # 请求前校验 N
    recent_periods = validate_recent_periods(recent_periods)

    failed_units: list[str] = []
    for report_name, table_suffix in report_map.items():
        logger.info("开始爬取 %s (%d 家公司，recent_periods=%s)...", report_name, len(secucodes), recent_periods)
        total = 0
        for i, secucode in enumerate(secucodes, 1):
            try:
                records = em_crawler.fetch_by_company(secucode, report_name)
                # N 期选择：保留目标期内全部记录（含修订）
                if recent_periods is not None:
                    records = select_recent_periods(records, recent_periods)
                mapped = [map_fields(r, report_name) for r in records]
                with repo.unit_of_work():
                    written = repo.batch_upsert_raw(table_suffix, mapped)
                total += written
                logger.info("[%d/%d] %s %s: %d 条", i, len(secucodes), secucode, table_suffix, written)
            except Exception as exc:
                logger.error("爬取 %s %s 失败（单元失败，不提交）：%s", secucode, report_name, exc)
                failed_units.append(f"{secucode}/{report_name}")
        logger.info("%s 爬取完成：共 %d 条", report_name, total)

    db.close()

    # 汇总失败单元，非零退出
    if failed_units:
        logger.error("爬取完成，但 %d 个单元失败: %s", len(failed_units), failed_units)
        return 1
    return 0


def cmd_clean(args) -> None:
    """清洗数据。"""
    config = load_config(args.config)
    logger = setup_logger(level=config.logging.get("level", "INFO"),
                          log_file=config.logging.get("file"))
    from fin_feature.cleaning.pipeline import run_cleaning

    db, repo = _get_db_and_repo(config)
    logger.info("开始数据清洗...")
    # 传入 YAML 清洗配置
    cleaning_config = config.cleaning
    stats = run_cleaning(repo, config=cleaning_config)
    for s in stats:
        logger.info(
            "表 %s: 原始%d -> 去重%d -> 丢弃%d -> 清洗%d",
            s.table_suffix, s.raw_count, s.dedup_count, s.dropped_count, s.clean_count,
        )
    db.close()


def cmd_features(args) -> None:
    """计算特征。"""
    config = load_config(args.config)
    logger = setup_logger(level=config.logging.get("level", "INFO"),
                          log_file=config.logging.get("file"))
    from fin_feature.features.engine import compute_all

    db, repo = _get_db_and_repo(config)
    logger.info("开始特征计算...")
    groups = config.features.get("groups")
    count = compute_all(repo, groups=groups)
    logger.info("特征计算完成：%d 条", count)
    db.close()


def cmd_quality(args) -> None:
    """生成数据质量报告。"""
    config = load_config(args.config)
    logger = setup_logger(level=config.logging.get("level", "INFO"),
                          log_file=config.logging.get("file"))
    from fin_feature.addons.quality import generate_report

    db, repo = _get_db_and_repo(config)
    logger.info("生成数据质量报告...")
    report = generate_report(repo)
    # 打印摘要
    for table, metrics in report.items():
        print(f"\n[{table}]")
        for metric, value in metrics.items():
            if isinstance(value, float):
                print(f"  {metric}: {value:.4f}")
            else:
                print(f"  {metric}: {value}")
    db.close()


def cmd_incremental(args) -> None:
    """增量爬取。"""
    config = load_config(args.config)
    logger = setup_logger(level=config.logging.get("level", "INFO"),
                          log_file=config.logging.get("file"))
    from fin_feature.addons.incremental import crawl_incremental
    from fin_feature.crawler.eastmoney import EastMoneyCrawler

    db, repo = _get_db_and_repo(config)
    em_crawler = EastMoneyCrawler(
        base_url=config.crawler.get("base_url"),
        page_size=config.crawler.get("page_size", 50),
        timeout=config.crawler.get("timeout", 15),
        min_interval=config.crawler.get("min_interval", 1.5),
        max_retries=config.crawler.get("max_retries", 3),
        user_agent=config.crawler.get("user_agent"),
        referer=config.crawler.get("referer", ""),
    )
    # 获取已爬取的公司列表
    secucodes = [r[0] for r in repo.conn.execute("SELECT secucode FROM stock_list").fetchall()]
    if not secucodes:
        logger.error("无股票列表，请先执行 crawl")
        db.close()
        return 1
    # 读取 recent_periods 配置
    recent_periods = config.crawl.get("recent_periods")
    logger.info("增量爬取 %d 家公司，recent_periods=%s...", len(secucodes), recent_periods)
    try:
        stats = crawl_incremental(em_crawler, repo, secucodes, recent_periods=recent_periods)
    except ValueError as exc:
        logger.error("配置错误: %s", exc)
        db.close()
        return 1
    failed_units = stats.pop("__failed_units__", 0)
    for report_name, count in stats.items():
        logger.info("%s: 新增 %d 条", report_name, count)
    db.close()
    # stage1-v1: 发生任一采集失败时非零退出
    if failed_units:
        logger.error("增量爬取完成，但 %d 个单元失败", failed_units)
        return 1
    return 0


def cmd_audit_raw(args) -> int:
    """只读对账采集证据（audit-raw）：不写数据库、不改证据文件。"""
    import sqlite3
    from pathlib import Path

    config = load_config(args.config)
    logger = setup_logger(level=config.logging.get("level", "INFO"),
                          log_file=config.logging.get("file"))

    if not args.run_id and not args.all_runs:
        logger.error("audit-raw 必须指定 --run-id 或 --all")
        return 2

    from fin_feature.storage.database import get_readonly_connection
    from fin_feature.storage.raw_audit import audit_raw
    from fin_feature.storage.raw_store import build_readonly_raw_store, RawStoreError

    # 只读：mode=ro 连接既有库，不创建数据库/目录、不改 journal_mode、不执行迁移
    try:
        conn = get_readonly_connection(config.db_path)
    except sqlite3.OperationalError as exc:
        logger.error("只读连接失败（库不存在或不可读）: %s", exc)
        return 1
    try:
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "ingest_unit" not in tables:
            logger.error("数据库缺少 v4 采集证据结构（ingest_unit），请先完成迁移")
            return 1
        try:
            raw_store = build_readonly_raw_store(config, config.db_path)
        except (ValueError, RawStoreError) as exc:
            logger.error("证据根配置错误或不存在: %s", exc)
            return 1
        report = audit_raw(conn, raw_store, run_id=args.run_id)
    except sqlite3.Error as exc:
        logger.error("读取数据库失败: %s", exc)
        return 1
    finally:
        conn.close()

    text = report.format_text()
    print(text)

    report_dir = Path(args.report_dir or "data/audit")
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"audit-raw_{report.scope}_{datetime.now():%Y%m%d%H%M%S}.txt"
    report_path.write_text(text + "\n", encoding="utf-8")
    logger.info("对账报告已保存至 %s", report_path)
    return report.exit_code()


def cmd_backtest(args) -> None:
    """特征回测。"""
    config = load_config(args.config)
    logger = setup_logger(level=config.logging.get("level", "INFO"),
                          log_file=config.logging.get("file"))
    from fin_feature.addons.backtest import format_report, run_backtest

    db, repo = _get_db_and_repo(config)
    n_groups = config.backtest.get("n_groups", 5)
    return_proxy = config.backtest.get("return_proxy", "next_profit_yoy")

    result = run_backtest(
        repo, args.feature, args.date, n_groups=n_groups, return_proxy=return_proxy,
    )
    report_text = format_report(result)
    print(report_text)

    # 保存报告到文件
    from pathlib import Path
    report_path = Path("reports") / f"backtest_{args.feature}_{args.date}.txt"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report_text, encoding="utf-8")
    logger.info("回测报告已保存至 %s", report_path)
    db.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="fin_feature",
        description="金融数据处理与特征工程框架",
    )
    parser.add_argument("--config", default=None, help="配置文件路径")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("crawl", help="爬取财务数据")
    sub.add_parser("clean", help="清洗数据")
    sub.add_parser("features", help="计算特征")
    sub.add_parser("quality", help="生成数据质量报告")
    sub.add_parser("incremental", help="增量爬取")
    bt = sub.add_parser("backtest", help="特征回测（分组分析）")
    bt.add_argument("--feature", required=True, help="用于分组的特征名")
    bt.add_argument("--date", required=True, help="分组日（报告期，如 2024-12-31）")

    ar = sub.add_parser("audit-raw", help="只读对账采集证据（不改数据库与证据文件）")
    ar.add_argument("--run-id", default=None, help="只检查指定采集批次")
    ar.add_argument("--all", dest="all_runs", action="store_true",
                    help="检查全部批次（与 --run-id 二选一）")
    ar.add_argument("--report-dir", default=None,
                    help="对账报告输出目录（默认 data/audit）")

    args = parser.parse_args()

    commands = {
        "crawl": cmd_crawl,
        "clean": cmd_clean,
        "features": cmd_features,
        "quality": cmd_quality,
        "incremental": cmd_incremental,
        "backtest": cmd_backtest,
        "audit-raw": cmd_audit_raw,
    }
    result = commands[args.command](args)
    # 传播退出码（None 视为成功）
    if result is not None and result != 0:
        sys.exit(result)


if __name__ == "__main__":
    main()
