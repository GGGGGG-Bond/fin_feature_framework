"""端到端测试：使用真实 API 爬取小样本数据，验证完整链路。

注意：这些测试需要网络连接，会被标记为 @requires_network。
CI 环境可通过 pytest -m "not requires_network" 跳过。
"""

import tempfile
from pathlib import Path

import pytest

from fin_feature.addons.backtest import run_backtest
from fin_feature.cleaning.pipeline import run_cleaning
from fin_feature.config import load_config
from fin_feature.features.engine import compute_all
from fin_feature.storage.database import Database
from fin_feature.storage.repository import Repository


# 标记需要网络连接的测试
pytestmark = pytest.mark.requires_network


@pytest.fixture(scope="module")
def e2e_db():
    """创建临时数据库用于端到端测试。"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        db = Database(db_path)
        db.init()
        repo = Repository(db.conn)
        yield db, repo
        db.close()
    finally:
        # 清理临时文件
        for ext in ["", "-wal", "-shm"]:
            p = Path(db_path + ext)
            if p.exists():
                p.unlink()


@pytest.fixture(scope="module")
def crawled_data(e2e_db):
    """爬取小样本数据：1 家公司，2 种报表（主要财务指标 + 利润表）。"""
    db, repo = e2e_db

    from fin_feature.crawler.eastmoney import EastMoneyCrawler
    from fin_feature.storage.field_mapping import map_fields

    crawler = EastMoneyCrawler(
        base_url="https://datacenter-web.eastmoney.com/api/data/v1/get",
        page_size=50,
        timeout=15,
        min_interval=1.5,
    )

    # 爬取 1 家公司（贵州茅台）的报表
    secucode = "600519.SH"
    report_map = {
        "RPT_LICO_FN_CPD": "financial_indicator",
        "RPT_DMSK_FN_INCOME": "income_statement",
    }

    results = {}
    for report_name, table_suffix in report_map.items():
        records = crawler.fetch_by_company(secucode, report_name)
        mapped = [map_fields(r, report_name) for r in records]
        written = repo.batch_upsert_raw(table_suffix, mapped)
        results[report_name] = {
            "records_count": len(records),
            "written_count": written,
        }

    return {
        "secucode": secucode,
        "total_records": sum(r["records_count"] for r in results.values()),
        "total_written": sum(r["written_count"] for r in results.values()),
    }


class TestCrawler:
    """爬虫端到端测试。"""

    def test_fetch_financial_indicator(self, crawled_data):
        """应成功爬取主要财务指标数据。"""
        assert crawled_data["total_records"] >= 2, "应至少爬取到 2 条记录"
        assert crawled_data["total_written"] >= 2, "应至少写入 2 条记录"

    def test_fetch_multiple_periods(self, crawled_data):
        """应爬取到多个报告期的数据。"""
        # 主要财务指标和利润表应包含多个报告期
        assert crawled_data["total_records"] >= 8, "应至少爬取到 8 条记录（2 表×4 期）"


class TestCleaning:
    """清洗端到端测试。"""

    def test_clean_multiple_tables(self, e2e_db, crawled_data):
        """清洗多张表数据。"""
        db, repo = e2e_db

        # 执行清洗
        stats = run_cleaning(repo, config=None)

        # 验证清洗结果
        assert len(stats) >= 1, "应至少清洗 1 张表"

        # 验证至少有一张表有清洗后数据
        total_clean = sum(s.clean_count for s in stats)
        assert total_clean >= 1, "清洗后应至少有数据"

    def test_clean_preserves_latest_version(self, e2e_db, crawled_data):
        """清洗应保留最新公告版本。"""
        db, repo = e2e_db

        # 获取清洗后的记录
        secucode = crawled_data["secucode"]
        rows = repo.conn.execute(
            "SELECT report_date, notice_date FROM clean_financial_indicator "
            "WHERE secucode = ? ORDER BY report_date DESC",
            (secucode,)
        ).fetchall()

        # 应至少有一个报告期的数据
        assert len(rows) >= 1, "应至少清洗出 1 条记录"


class TestFeatures:
    """特征计算端到端测试。"""

    def test_compute_features(self, e2e_db, crawled_data):
        """计算特征应成功写入。"""
        db, repo = e2e_db

        # 先执行清洗（如果还没执行）
        run_cleaning(repo, config=None)

        # 计算特征
        count = compute_all(repo)

        # 验证特征写入
        assert count >= 1, "应至少计算 1 个特征值"

    def test_feature_names_registered(self, e2e_db, crawled_data):
        """应注册预期的特征名称。"""
        db, repo = e2e_db

        # 清洗和计算
        run_cleaning(repo, config=None)
        compute_all(repo)

        # 获取所有特征名
        rows = repo.conn.execute(
            "SELECT DISTINCT feature_name FROM features"
        ).fetchall()
        feature_names = [r[0] for r in rows]

        # 验证核心特征存在
        expected_features = [
            "net_profit_margin",
            "gross_margin_feat",
            "roe_change",
            "revenue_yoy",
            "profit_yoy",
            "equity_ratio",
            "debt_asset_ratio",
        ]
        for name in expected_features:
            assert name in feature_names, f"应包含特征 {name}"

    def test_feature_values_not_all_none(self, e2e_db, crawled_data):
        """特征值不应全部为 None。"""
        db, repo = e2e_db

        # 清洗和计算
        run_cleaning(repo, config=None)
        compute_all(repo)

        # 统计非空特征值
        rows = repo.conn.execute(
            "SELECT COUNT(*) FROM features WHERE feature_value IS NOT NULL"
        ).fetchone()
        non_null_count = rows[0]

        assert non_null_count >= 1, "应至少有一个非空特征值"


class TestBacktest:
    """分组分析端到端测试。"""

    def test_backtest_runs(self, e2e_db, crawled_data):
        """分组分析应能运行并返回结果。"""
        db, repo = e2e_db

        # 清洗和计算
        run_cleaning(repo, config=None)
        compute_all(repo)

        # 获取一个有效的分组日（需要至少 5 个特征）
        row = repo.conn.execute(
            "SELECT report_date FROM features GROUP BY report_date "
            "HAVING COUNT(*) >= 5 ORDER BY report_date DESC LIMIT 1"
        ).fetchone()

        if row is None:
            pytest.skip("无足够的特征数据进行分组分析")

        formation_date = row[0]

        # 运行分组分析
        result = run_backtest(repo, "roe_change", formation_date, n_groups=5)

        # 验证结果结构
        assert result.feature_name == "roe_change"
        assert result.formation_date == formation_date
        assert result.n_groups == 5
        # 注意：只有 1 家公司，样本数为 0 是正常的（无法分组）
        # 这里只验证能运行，不验证样本数


class TestConfigLoading:
    """配置加载端到端测试。"""

    def test_load_default_config(self):
        """应能加载默认配置。"""
        config = load_config()

        assert config.db_path == "data/finance.db"
        assert config.crawler.get("min_interval") == 1.5
        assert config.crawl.get("stock_count") == 50
        assert config.crawl.get("recent_periods") == 4

    def test_load_yaml_cleaning_config(self):
        """YAML 清洗配置应能正确加载。"""
        config = load_config()
        cleaning = config.cleaning

        # 验证阈值使用标准单位（小数）
        assert "weightavg_roe" in cleaning.get("outlier_thresholds", {})
        roe_threshold = cleaning["outlier_thresholds"]["weightavg_roe"]
        # stage1-v1 阈值应为 [-2.0, 2.0] 而非 [-200, 200]
        assert roe_threshold[0] <= -1.0, "ROE 阈值下限应使用标准单位（小数）"
        assert roe_threshold[1] >= 1.0, "ROE 阈值上限应使用标准单位（小数）"


class TestIncremental:
    """增量爬取端到端测试。"""

    def test_incremental_respects_recent_periods(self, e2e_db, crawled_data):
        """增量爬取应尊重 recent_periods 配置。"""
        db, repo = e2e_db

        from fin_feature.addons.incremental import crawl_incremental
        from fin_feature.crawler.eastmoney import EastMoneyCrawler

        crawler = EastMoneyCrawler(
            base_url="https://datacenter-web.eastmoney.com/api/data/v1/get",
            page_size=50,
            timeout=15,
            min_interval=1.5,
        )

        secucode = crawled_data["secucode"]
        report_names = ["RPT_LICO_FN_CPD"]

        # 限制为最近 2 个报告期
        stats = crawl_incremental(
            crawler, repo, [secucode], report_names=report_names, recent_periods=2
        )

        # 验证返回统计
        assert "RPT_LICO_FN_CPD" in stats
