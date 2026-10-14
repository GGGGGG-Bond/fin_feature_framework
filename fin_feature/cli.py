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
    repo.upsert_stock_list(stocks)
    logger.info("股票列表爬取完成: %d 只", len(stocks))

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
    for report_name, table_suffix in report_map.items():
        logger.info("开始爬取 %s (%d 家公司)...", report_name, len(secucodes))
        total = 0
        for i, secucode in enumerate(secucodes, 1):
            try:
                records = em_crawler.fetch_by_company(secucode, report_name)
                mapped = [map_fields(r, report_name) for r in records]
                written = repo.batch_upsert_raw(table_suffix, mapped)
                total += written
                logger.info("[%d/%d] %s %s: %d 条", i, len(secucodes), secucode, table_suffix, written)
            except Exception as exc:
                logger.error("爬取 %s %s 失败: %s", secucode, report_name, exc)
        logger.info("%s 爬取完成: 共 %d 条", report_name, total)

    db.close()


def cmd_clean(args) -> None:
    """清洗数据。"""
    config = load_config(args.config)
    logger = setup_logger(level=config.logging.get("level", "INFO"),
                          log_file=config.logging.get("file"))
    from fin_feature.cleaning.pipeline import run_cleaning

    db, repo = _get_db_and_repo(config)
    logger.info("开始数据清洗...")
    stats = run_cleaning(repo)
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
    logger.info("特征计算完成: %d 条", count)
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
        return
    logger.info("增量爬取 %d 家公司...", len(secucodes))
    stats = crawl_incremental(em_crawler, repo, secucodes)
    for report_name, count in stats.items():
        logger.info("%s: 新增 %d 条", report_name, count)
    db.close()


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
    sub.add_parser("quality", help="生成质量报告")
    sub.add_parser("incremental", help="增量爬取")
    bt = sub.add_parser("backtest", help="特征回测")
    bt.add_argument("--feature", required=True, help="用于分组的特征名")
    bt.add_argument("--date", required=True, help="分组日（报告期，如 2024-12-31）")

    args = parser.parse_args()

    commands = {
        "crawl": cmd_crawl,
        "clean": cmd_clean,
        "features": cmd_features,
        "quality": cmd_quality,
        "incremental": cmd_incremental,
        "backtest": cmd_backtest,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
