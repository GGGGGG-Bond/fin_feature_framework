"""SQLite 表结构 DDL 定义。

设计要点：
- 原始表唯一键含 notice_date，保留同一报告期的多个披露版本（数据修订）。
- 清洗表唯一键仅 (secucode, report_date)，去重后每公司每报告期仅一条。
- 特征表用长表格式（feature_name + feature_value），新增特征无需改表结构。
- 所有表建索引于 secucode + report_date，加速按维度查询。
"""

# 股票列表
DDL_STOCK_LIST = """
CREATE TABLE IF NOT EXISTS stock_list (
    secucode        TEXT PRIMARY KEY,
    security_code   TEXT NOT NULL,
    security_name   TEXT,
    market          TEXT,
    crawled_at      TEXT DEFAULT (datetime('now', 'localtime'))
);
"""

# 原始主要财务指标
DDL_RAW_FINANCIAL_INDICATOR = """
CREATE TABLE IF NOT EXISTS raw_financial_indicator (
    secucode            TEXT NOT NULL,
    security_code       TEXT,
    security_name       TEXT,
    report_date         TEXT NOT NULL,
    notice_date         TEXT,
    entry_time          TEXT,
    qdate               TEXT,
    datatype            TEXT,
    data_year           TEXT,
    basic_eps           REAL,
    deduct_basic_eps    REAL,
    total_operate_income REAL,
    parent_netprofit    REAL,
    weightavg_roe       REAL,
    revenue_yoy         REAL,
    profit_yoy          REAL,
    bps                 REAL,
    ocf_per_share       REAL,
    gross_margin        REAL,
    revenue_qoq         REAL,
    profit_qoq          REAL,
    is_new              TEXT,
    update_date         TEXT,
    crawled_at          TEXT DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (secucode, report_date, notice_date)
);
"""

# 原始利润表
DDL_RAW_INCOME_STATEMENT = """
CREATE TABLE IF NOT EXISTS raw_income_statement (
    secucode                TEXT NOT NULL,
    security_code           TEXT,
    security_name           TEXT,
    report_date             TEXT NOT NULL,
    notice_date             TEXT,
    entry_time              TEXT,
    date_type_code          TEXT,
    report_type_code        TEXT,
    data_state              TEXT,
    industry_name           TEXT,
    total_operate_income    REAL,
    total_operate_cost      REAL,
    operate_cost             REAL,
    operate_expense         REAL,
    sale_expense            REAL,
    manage_expense          REAL,
    finance_expense         REAL,
    operate_profit          REAL,
    total_profit            REAL,
    income_tax              REAL,
    operate_income          REAL,
    invest_income           REAL,
    parent_netprofit        REAL,
    deduct_parent_netprofit REAL,
    toe_ratio               REAL,
    operate_expense_ratio   REAL,
    operate_profit_ratio    REAL,
    parent_netprofit_ratio  REAL,
    crawled_at              TEXT DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (secucode, report_date, notice_date)
);
"""

# 原始资产负债表
DDL_RAW_BALANCE_SHEET = """
CREATE TABLE IF NOT EXISTS raw_balance_sheet (
    secucode            TEXT NOT NULL,
    security_code       TEXT,
    security_name       TEXT,
    report_date         TEXT NOT NULL,
    notice_date         TEXT,
    entry_time          TEXT,
    date_type_code      TEXT,
    report_type_code    TEXT,
    data_state          TEXT,
    industry_name       TEXT,
    total_assets        REAL,
    fixed_asset         REAL,
    monetary_funds      REAL,
    accounts_receivable REAL,
    inventory           REAL,
    total_liabilities   REAL,
    accounts_payable    REAL,
    total_equity        REAL,
    current_ratio       REAL,
    debt_asset_ratio    REAL,
    total_liab_ratio    REAL,
    total_assets_ratio  REAL,
    total_equity_ratio  REAL,
    crawled_at          TEXT DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (secucode, report_date, notice_date)
);
"""

# 清洗后主要财务指标
DDL_CLEAN_FINANCIAL_INDICATOR = """
CREATE TABLE IF NOT EXISTS clean_financial_indicator (
    secucode            TEXT NOT NULL,
    security_code       TEXT,
    security_name       TEXT,
    report_date         TEXT NOT NULL,
    notice_date         TEXT,
    qdate               TEXT,
    datatype            TEXT,
    report_type         TEXT,
    basic_eps           REAL,
    total_operate_income REAL,
    parent_netprofit    REAL,
    weightavg_roe       REAL,
    revenue_yoy         REAL,
    profit_yoy          REAL,
    bps                 REAL,
    gross_margin        REAL,
    revenue_qoq         REAL,
    profit_qoq          REAL,
    disclosure_status   TEXT,
    cleaned_at          TEXT DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (secucode, report_date)
);
"""

# 清洗后利润表
DDL_CLEAN_INCOME_STATEMENT = """
CREATE TABLE IF NOT EXISTS clean_income_statement (
    secucode                TEXT NOT NULL,
    security_code           TEXT,
    security_name           TEXT,
    report_date             TEXT NOT NULL,
    notice_date             TEXT,
    report_type             TEXT,
    industry_name           TEXT,
    total_operate_income    REAL,
    total_operate_cost      REAL,
    operate_cost            REAL,
    sale_expense            REAL,
    manage_expense          REAL,
    finance_expense         REAL,
    operate_profit          REAL,
    total_profit            REAL,
    income_tax              REAL,
    parent_netprofit        REAL,
    deduct_parent_netprofit REAL,
    disclosure_status       TEXT,
    cleaned_at              TEXT DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (secucode, report_date)
);
"""

# 清洗后资产负债表
DDL_CLEAN_BALANCE_SHEET = """
CREATE TABLE IF NOT EXISTS clean_balance_sheet (
    secucode            TEXT NOT NULL,
    security_code       TEXT,
    security_name       TEXT,
    report_date         TEXT NOT NULL,
    notice_date         TEXT,
    report_type         TEXT,
    industry_name       TEXT,
    total_assets        REAL,
    monetary_funds      REAL,
    accounts_receivable REAL,
    inventory           REAL,
    total_liabilities   REAL,
    accounts_payable    REAL,
    total_equity        REAL,
    current_ratio       REAL,
    debt_asset_ratio    REAL,
    disclosure_status   TEXT,
    cleaned_at          TEXT DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (secucode, report_date)
);
"""

# 特征长表
DDL_FEATURES = """
CREATE TABLE IF NOT EXISTS features (
    secucode            TEXT NOT NULL,
    report_date         TEXT NOT NULL,
    feature_name        TEXT NOT NULL,
    feature_value       REAL,
    feature_group       TEXT,
    definition_version  TEXT,
    computed_at         TEXT DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (secucode, report_date, feature_name)
);
"""

# 清洗日志表
DDL_CLEAN_LOG = """
CREATE TABLE IF NOT EXISTS clean_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    secucode        TEXT,
    report_date     TEXT,
    source_table    TEXT,
    action          TEXT,
    detail          TEXT,
    logged_at       TEXT DEFAULT (datetime('now', 'localtime'))
);
"""

# 增量爬取水位表
DDL_CRAWL_WATERMARK = """
CREATE TABLE IF NOT EXISTS crawl_watermark (
    source_name     TEXT NOT NULL,
    secucode        TEXT NOT NULL,
    last_notice_date TEXT,
    last_crawled_at TEXT DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (source_name, secucode)
);
"""

# 数据质量报告表
DDL_QUALITY_REPORT = """
CREATE TABLE IF NOT EXISTS quality_report (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name      TEXT,
    metric_name     TEXT,
    metric_value    REAL,
    detail          TEXT,
    reported_at     TEXT DEFAULT (datetime('now', 'localtime'))
);
"""

# 所有 DDL（按依赖顺序）
ALL_DDL = [
    DDL_STOCK_LIST,
    DDL_RAW_FINANCIAL_INDICATOR,
    DDL_RAW_INCOME_STATEMENT,
    DDL_RAW_BALANCE_SHEET,
    DDL_CLEAN_FINANCIAL_INDICATOR,
    DDL_CLEAN_INCOME_STATEMENT,
    DDL_CLEAN_BALANCE_SHEET,
    DDL_FEATURES,
    DDL_CLEAN_LOG,
    DDL_CRAWL_WATERMARK,
    DDL_QUALITY_REPORT,
]

# 索引定义
ALL_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_raw_fi_code ON raw_financial_indicator(secucode);",
    "CREATE INDEX IF NOT EXISTS idx_raw_fi_date ON raw_financial_indicator(report_date);",
    "CREATE INDEX IF NOT EXISTS idx_raw_is_code ON raw_income_statement(secucode);",
    "CREATE INDEX IF NOT EXISTS idx_raw_is_date ON raw_income_statement(report_date);",
    "CREATE INDEX IF NOT EXISTS idx_raw_bs_code ON raw_balance_sheet(secucode);",
    "CREATE INDEX IF NOT EXISTS idx_raw_bs_date ON raw_balance_sheet(report_date);",
    "CREATE INDEX IF NOT EXISTS idx_clean_fi_code ON clean_financial_indicator(secucode);",
    "CREATE INDEX IF NOT EXISTS idx_clean_is_code ON clean_income_statement(secucode);",
    "CREATE INDEX IF NOT EXISTS idx_clean_bs_code ON clean_balance_sheet(secucode);",
    "CREATE INDEX IF NOT EXISTS idx_features_code ON features(secucode);",
    "CREATE INDEX IF NOT EXISTS idx_features_date ON features(report_date);",
    "CREATE INDEX IF NOT EXISTS idx_features_name ON features(feature_name);",
    "CREATE INDEX IF NOT EXISTS idx_clean_log_code ON clean_log(secucode);",
    "CREATE INDEX IF NOT EXISTS idx_watermark_source ON crawl_watermark(source_name);",
]
