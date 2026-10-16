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

# ========== 阶段 2 新增表（2A）==========

# 1. 迁移版本管理
DDL_SCHEMA_VERSION = """
CREATE TABLE IF NOT EXISTS schema_version (
    version         INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    applied_at      TEXT DEFAULT (datetime('now', 'localtime'))
);
"""

# 2. 采集批次
DDL_INGEST_RUN = """
CREATE TABLE IF NOT EXISTS ingest_run (
    run_id          TEXT PRIMARY KEY,          -- UUID
    source_name     TEXT NOT NULL,             -- eastmoney
    request_scope   TEXT,                      -- JSON: {report_names, secucodes, recent_periods}
    started_at      TEXT DEFAULT (datetime('now', 'localtime')),
    ended_at        TEXT,
    status          TEXT NOT NULL DEFAULT 'running',  -- running/success/partial_failure/failed
    failed_units    INTEGER DEFAULT 0,
    crawler_version TEXT,                      -- 采集器版本标识
    detail          TEXT                       -- 失败原因摘要
);
"""

# 3. 原始响应留存
DDL_RAW_SNAPSHOT = """
CREATE TABLE IF NOT EXISTS raw_snapshot (
    snapshot_id      TEXT PRIMARY KEY,          -- UUID
    run_id           TEXT NOT NULL,             -- 关联采集批次
    source_name      TEXT NOT NULL,
    report_name      TEXT NOT NULL,             -- RPT_LICO_FN_CPD 等
    secucode         TEXT,                      -- 可空（股票列表请求无公司）
    page_number      INTEGER,
    request_params   TEXT,                      -- JSON（去除凭据）
    fetched_at       TEXT DEFAULT (datetime('now', 'localtime')),
    raw_content_hash TEXT NOT NULL,             -- SHA-256（原始响应 JSON，仅用于去重）
    file_path        TEXT NOT NULL,             -- 不可变内容文件路径
    record_count     INTEGER,
    FOREIGN KEY (run_id) REFERENCES ingest_run(run_id)
);
"""

# 4. 标准化版本与血缘（核心表）
DDL_STATEMENT_VERSION = """
CREATE TABLE IF NOT EXISTS statement_version (
    version_id           TEXT PRIMARY KEY,          -- UUID
    snapshot_id          TEXT,                       -- 关联原始响应（旧数据迁移为 NULL）
    run_id               TEXT,                       -- 关联采集批次（旧数据迁移为 NULL）
    secucode             TEXT NOT NULL,
    report_date          TEXT NOT NULL,
    table_type           TEXT NOT NULL,             -- financial_indicator/income_statement/balance_sheet
    source_name          TEXT NOT NULL DEFAULT 'eastmoney',  -- 来源（observation_seq 分组维度之一）
    notice_date          TEXT,                      -- 公告时间
    revision_notice_date TEXT,                      -- 修订公告时间（未知为 NULL）
    revision_type        TEXT DEFAULT 'unknown',    -- initial/revision/error_correction/unknown
    observed_at          TEXT DEFAULT (datetime('now', 'localtime')),  -- 系统观察时间
    metric_hash          TEXT NOT NULL,             -- SHA-256（仅标准化字段值，检测数值变化）
    semantic_hash        TEXT NOT NULL,             -- SHA-256（版本身份：字段值 + 来源 + 币种/计量+scope+period_basis 等）
    observation_seq      INTEGER NOT NULL,          -- 同一(secucode,report_date,table_type,source_name)内自增
    consolidate_scope    TEXT DEFAULT 'unknown',    -- consolidated/parent_only/unknown
    period_basis         TEXT DEFAULT 'unknown',    -- flow_ytd/flow_single_quarter/stock_end/unknown
    currency             TEXT DEFAULT 'unknown',
    amount_unit          TEXT DEFAULT 'unknown',
    industry_type        TEXT DEFAULT 'unknown',    -- industrial/financial/unknown
    standardized_fields  TEXT,                      -- JSON: 标准化字段快照
    raw_fields           TEXT,                      -- JSON: 原始字段快照（映射后）
    evidence_sufficient  INTEGER DEFAULT 1,         -- 1=有原始响应；0=旧数据迁移（无原始响应）
    is_superseded        INTEGER DEFAULT 0,         -- 0=当前有效；1=已被新版本替代
    superseded_by        TEXT,                      -- 指向替代版本 version_id
    FOREIGN KEY (snapshot_id) REFERENCES raw_snapshot(snapshot_id),
    FOREIGN KEY (run_id) REFERENCES ingest_run(run_id),
    UNIQUE (secucode, report_date, table_type, source_name, observation_seq)
);
"""

# 5. 双源核验结果（阶段 3 用，阶段 2 仅预留表结构）
DDL_RECONCILIATION_RESULT = """
CREATE TABLE IF NOT EXISTS reconciliation_result (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    left_version_id     TEXT NOT NULL,
    right_version_id    TEXT NOT NULL,
    field_name          TEXT NOT NULL,
    comparable_status   TEXT,                      -- comparable/incomparable/unknown
    left_value          REAL,
    right_value         REAL,
    converted_value     REAL,
    tolerance           REAL,
    difference          REAL,
    conclusion          TEXT,                      -- match/conflict/unknown
    evidence            TEXT,                      -- JSON: 证据引用
    reconciled_at       TEXT DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (left_version_id) REFERENCES statement_version(version_id),
    FOREIGN KEY (right_version_id) REFERENCES statement_version(version_id)
);
"""

# 6. 不可变计算记录（A3 修复：重放绑定确切计算实例）
DDL_FEATURE_COMPUTE_LOG = """
CREATE TABLE IF NOT EXISTS feature_compute_log (
    result_id              TEXT PRIMARY KEY,          -- UUID，唯一标识一次计算
    compute_run_id         TEXT NOT NULL,             -- 同次批量计算共享
    secucode               TEXT NOT NULL,
    report_date            TEXT NOT NULL,
    feature_name           TEXT NOT NULL,
    definition_version     TEXT NOT NULL,             -- stage1-v1 等
    input_version_ids      TEXT NOT NULL,             -- JSON: 当期/同比期/相邻期/差分期各表输入的 version_id
    feature_value          REAL,                      -- 计算结果（None 时为 NULL）
    not_computable_reason  TEXT,                      -- missing_input/non_positive_base/not_applicable 等
    code_commit            TEXT,                      -- 计算时 git commit
    config_snapshot        TEXT,                      -- JSON: 规范化的实际生效配置（含默认值与运行覆盖）
    as_of_date             TEXT,                      -- 计算时点
    computed_at            TEXT DEFAULT (datetime('now', 'localtime'))
    -- 注意：compute_run_id 不引用 ingest_run（计算批次≠采集批次，P1 #4 修复）
    -- 计算可能消费多个采集批次或旧迁移版本，需独立标识
);
"""

# features 表加列（6 列，幂等 ALTER TABLE，由迁移执行）
DDL_FEATURES_ADD_COLUMNS = """
ALTER TABLE features ADD COLUMN compute_run_id TEXT;
ALTER TABLE features ADD COLUMN input_version_ids TEXT;
ALTER TABLE features ADD COLUMN code_commit TEXT;
ALTER TABLE features ADD COLUMN config_hash TEXT;
ALTER TABLE features ADD COLUMN as_of_date TEXT;
ALTER TABLE features ADD COLUMN not_computable_reason TEXT;
"""

# 阶段 2 新增索引
ALL_INDEXES_STAGE2 = [
    "CREATE INDEX IF NOT EXISTS idx_sv_secucode ON statement_version(secucode);",
    "CREATE INDEX IF NOT EXISTS idx_sv_report_date ON statement_version(report_date);",
    "CREATE INDEX IF NOT EXISTS idx_sv_table_type ON statement_version(table_type);",
    "CREATE INDEX IF NOT EXISTS idx_sv_current ON statement_version(secucode, report_date, table_type, is_superseded);",
    "CREATE INDEX IF NOT EXISTS idx_raw_snap_run ON raw_snapshot(run_id);",
    "CREATE INDEX IF NOT EXISTS idx_ingest_run_status ON ingest_run(status);",
    "CREATE INDEX IF NOT EXISTS idx_fcl_lookup ON feature_compute_log(secucode, report_date, feature_name);",
    "CREATE INDEX IF NOT EXISTS idx_fcl_run ON feature_compute_log(compute_run_id);",
]

# 更新 ALL_DDL（按依赖顺序，供 migrate_v1_baseline 使用）
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
    # 阶段 2 新增
    DDL_SCHEMA_VERSION,
    DDL_INGEST_RUN,
    DDL_RAW_SNAPSHOT,
    DDL_STATEMENT_VERSION,
    DDL_RECONCILIATION_RESULT,
    DDL_FEATURE_COMPUTE_LOG,
]
