"""CLI 入口：爬取/清洗/特征/质量/增量/回测 及原始证据对账、离线重放八个子命令。

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
from fin_feature.storage.repository import Repository
from fin_feature.utils.logger import setup_logger


def _get_db_and_repo(config):
    """初始化数据库并返回 (Database, Repository)。"""
    db = Database(config.db_path)
    db.init()
    return db, Repository(db.conn)


def _new_ingestion_session(settings):
    """One anonymous session; credentials and arbitrary headers never enter snapshots."""
    import requests
    session = requests.Session()
    session.headers.update({"User-Agent": settings.user_agent, "Referer": settings.referer})
    return session


def _cmd_ingest(args, mode):
    import json
    from pathlib import Path
    import yaml
    from fin_feature.ingestion.settings import validate_ingestion_settings
    from fin_feature.ingestion.http_evidence import EvidenceRequestExecutor
    from fin_feature.ingestion.workflow import IngestionWorkflow
    from fin_feature.storage.raw_store import RawStore
    from fin_feature.crawler.rate_limiter import RateLimiter

    # Validate the complete request/config scope before DB initialization, run creation or GET.
    try:
        if args.config is not None and not Path(args.config).is_file():
            raise ValueError("configuration file does not exist")
        config = load_config(args.config)
        settings = validate_ingestion_settings(config)
    except (ValueError, TypeError, OSError, yaml.YAMLError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    db, session = None, None
    logger = setup_logger()
    code = 1
    try:
        logger = setup_logger(level=config.logging.get("level", "INFO"), log_file=config.logging.get("file"))
        db, repo = _get_db_and_repo(config)
        store = RawStore(settings.raw_root, settings.max_body_bytes)
        repo.raw_store = store
        session = _new_ingestion_session(settings)
        limiter = RateLimiter(settings.min_interval)
        def executor(url):
            return EvidenceRequestExecutor(repository=repo, raw_store=store, session=session,
                                           url=url, timeout=settings.timeout,
                                           max_retries=settings.max_retries, rate_limiter=limiter)
        workflow = IngestionWorkflow(repo, store, executor(settings.financial_url), settings,
                                     stock_executor=executor(settings.stock_url) if mode == "crawl" else None)
        result = workflow.run(mode)
        print(json.dumps({"run_id": result.run_id, "status": result.status,
                          "failed_units": result.failed_units, "total": result.total,
                          "success": result.success, "failed": result.failed,
                          "incomplete": result.incomplete}, sort_keys=True))
        code = result.exit_code
    except KeyboardInterrupt:
        logger.error("Ingestion interrupted; unfinished evidence is retained for audit-raw")
    except Exception as exc:
        # Keep the console bounded: raw response bodies never belong in diagnostic output.
        logger.error("Ingestion failed: %s", type(exc).__name__)
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                code = 1
        if db is not None:
            try:
                db.close()
            except Exception:
                code = 1
    return code


def cmd_crawl(args) -> int:
    """Discover stocks with raw evidence, then run the shared financial coordinator."""
    return _cmd_ingest(args, "crawl")


def cmd_clean(args) -> None:
    """清洗数据。"""
    config = load_config(args.config)
    logger = setup_logger(level=config.logging.get("level", "INFO"),
                          log_file=config.logging.get("file"))
    from fin_feature.cleaning.pipeline import run_cleaning
    from fin_feature.cleaning.versioned import effective_rules
    effective_rules(config.cleaning)

    db, repo = _get_db_and_repo(config)
    logger.info("开始数据清洗...")
    # 传入 YAML 清洗配置
    cleaning_config = config.cleaning
    try:
        stats = run_cleaning(repo, config=cleaning_config, input_mode="versions")
    except BaseException:
        db.close()
        raise
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
    from fin_feature.cleaning.versioned import effective_rules
    effective_rules(config.cleaning)

    db, repo = _get_db_and_repo(config)
    logger.info("开始特征计算...")
    groups = config.features.get("groups")
    try:
        count = compute_all(repo, groups=groups, input_mode="versions", config=config.cleaning)
    except BaseException:
        db.close()
        raise
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


def cmd_incremental(args) -> int:
    """Run the same financial coordinator using the persisted stock universe."""
    return _cmd_ingest(args, "incremental")


def cmd_audit_raw(args) -> int:
    """只读对账采集证据（audit-raw）：不写数据库、不改证据文件。"""
    import sqlite3
    from pathlib import Path

    config = load_config(args.config)
    logger = setup_logger(level=config.logging.get("level", "INFO"),
                          log_file=config.logging.get("file"))

    if bool(args.run_id) == bool(args.all_runs):
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

    import json
    report_dir = Path(args.report_dir or "data/audit")
    try:
        if report_dir.resolve() == raw_store.root or raw_store.root in report_dir.resolve().parents:
            logger.error("audit reports must be outside the evidence root")
            return 2
        report_dir.mkdir(parents=True, exist_ok=True)
        stem = f"audit-raw_{report.scope}_{datetime.now():%Y%m%d%H%M%S%f}"
        report_path = report_dir / (stem + ".txt")
        report_path.write_text(text + "\n", encoding="utf-8")
        (report_dir / (stem + ".json")).write_text(
            json.dumps(report.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        logger.info("audit report saved to %s", report_path)
    except OSError:
        logger.error("audit report output could not be written")
        return 1
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


def cmd_replay(args) -> int:
    """Read-only replay; no migration, network requests or feature/log writes."""
    import json
    import sqlite3
    from fin_feature.replay import replay_result
    from fin_feature.storage.database import get_readonly_connection
    conn = None
    try:
        config = load_config(args.config)
        conn = get_readonly_connection(config.db_path)
        conn.execute('BEGIN')
        repo = Repository(conn)
        if args.result_id:
            ids = [args.result_id]
        else:
            ids = [row[0] for row in conn.execute(
                'SELECT result_id FROM feature_compute_log WHERE compute_run_id=? ORDER BY result_id',
                (args.compute_run_id,))]
        if not ids:
            raise ValueError('no calculation results in the requested scope')
        results = [replay_result(repo, result_id) for result_id in ids]
        print(json.dumps({'total':len(results),'matched':sum(r['match'] for r in results),
                          'replay_scope':'saved-inputs/current-code','results':results},ensure_ascii=False))
        return 0 if all(r['match'] for r in results) else 1
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as exc:
        print(f'Replay failed: {type(exc).__name__}: {exc}',file=sys.stderr)
        return 2
    finally:
        if conn is not None:
            conn.rollback()
            conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="fin_feature",
        description="金融数据处理与特征工程框架",
    )
    parser.add_argument("--config", default=None, help="配置文件路径")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("crawl", help="发现前 N 家公司并留证采集财务报表",
                   description="先完整核验股票发现范围，再采集配置报表；0 成功，1 失败，2 非法配置。")
    sub.add_parser("clean", help="清洗当前版本并保存标准值与血缘")
    sub.add_parser("features", help="从版本清洗结果计算特征并追加不可变日志")
    sub.add_parser("quality", help="生成数据质量报告")
    sub.add_parser("incremental", help="对本地股票列表按报告期回扫并留证采集",
                   description="使用全部本地 stock_list；recent_periods 限制报告期，水位不作请求过滤。0 成功，1 失败，2 非法配置。")
    bt = sub.add_parser("backtest", help="特征回测（分组分析）")
    bt.add_argument("--feature", required=True, help="用于分组的特征名")
    bt.add_argument("--date", required=True, help="分组日（报告期，如 2024-12-31）")

    ar = sub.add_parser("audit-raw", help="只读对账采集证据（不改数据库与证据文件）")
    ar.add_argument("--run-id", default=None, help="只检查指定采集批次")
    ar.add_argument("--all", dest="all_runs", action="store_true",
                    help="检查全部批次（与 --run-id 二选一）")
    ar.add_argument("--report-dir", default=None,
                    help="TXT/JSON 报告输出目录，须在证据根外（默认 data/audit）")

    rp = sub.add_parser("replay", help="按已保存输入离线重放（只读、当前代码）")
    scope = rp.add_mutually_exclusive_group(required=True)
    scope.add_argument("--result-id", help="重放一条不可变计算结果")
    scope.add_argument("--compute-run-id", help="重放一个计算批次")

    rc = sub.add_parser("reconcile", help="离线核验两份来源数据，JSON 报告输出到 stdout")
    rc.add_argument("--left", required=True, help="主来源 reconcile-v1 JSON")
    rc.add_argument("--right", required=True, help="第二来源 reconcile-v1 JSON")
    rc.add_argument("--abs-tol", default="1", help="金额绝对容差，标准元，默认 1")
    rc.add_argument("--rel-tol", default="0.000001", help="相对容差，默认 0.000001")
    from fin_feature.reconcile import cmd_reconcile

    args = parser.parse_args()

    commands = {
        "crawl": cmd_crawl,
        "clean": cmd_clean,
        "features": cmd_features,
        "quality": cmd_quality,
        "incremental": cmd_incremental,
        "backtest": cmd_backtest,
        "audit-raw": cmd_audit_raw,
        "replay": cmd_replay,
        "reconcile": cmd_reconcile,
    }
    result = commands[args.command](args)
    # 传播退出码（None 视为成功）
    if result is not None and result != 0:
        sys.exit(result)


if __name__ == "__main__":
    main()
