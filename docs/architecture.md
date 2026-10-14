# 架构设计：从 0 到 1 怎么做的

> 本文档记录项目从需求到落地的完整设计过程：技术调研、关键决策、模块划分、数据模型、可扩展性设计。供后续维护者理解「为什么这样设计」。

---

## 1. 起点：需求拆解

原始需求（见 [requirements.md](requirements.md)）要求一周内交付一套覆盖完整链路的金融数据处理框架：

```
数据获取 → 结构化存储 → 清洗标准化 → 特征生成 → (附加项)
```

核心考察点：爬取与反爬、结构化存储、清洗流程设计、特征工程、框架可扩展性。

**第一步不是写代码，而是确认四个影响架构的关键决策**（通过 AskUserQuestion 与用户确认）：

| 决策 | 选项 | 最终选择 | 影响 |
|------|------|----------|------|
| 数据源 | 东方财富 / 巨潮 / 同花顺 / akshare | 东方财富 | 决定爬虫模块实现策略 |
| 存储方案 | SQLite / Parquet / 两者结合 | SQLite | 决定存储层设计 |
| 起步范围 | 纵向切片 / 完整框架先铺开 | 完整框架先铺开 | 决定开发节奏 |
| 项目位置 | 全新独立 / 复用 validator | 全新独立 | 决定代码组织 |

## 2. 技术调研：先验证再设计

在写任何代码前，先验证数据源是否可用、环境是否就绪。**这一步避免了基于假设设计架构的风险。**

### 2.1 数据源验证

通过实际 API 调用确认东方财富 datacenter-web 接口可用（HTTP 200）：

| 报表 | reportName | 验证结果 |
|------|-----------|----------|
| 主要财务指标 | `RPT_LICO_FN_CPD` | ✅ 可用，含营收/净利润/ROE/毛利率/同比环比增速 |
| 利润表 | `RPT_DMSK_FN_INCOME` | ✅ 可用，含营收/成本/三费/利润总额/所得税 |
| 资产负债表 | `RPT_DMSK_FN_BALANCE` | ✅ 可用，含总资产/总负债/权益/流动比率/资产负债率 |

**关键发现**（影响清洗与特征设计）：

1. **filter 参数必须 URL 编码**：`(SECUCODE="002501.SZ")` 中的双引号直接拼在 URL 会 400，必须用 `requests` 的 params 或 `urllib.parse.urlencode`。
2. **按公司查询返回多期历史**：如 002501.SZ 利润表有 15 页，可用于计算同比/环比特征。
3. **同一报告期可有多条记录**：不同 `notice_date`（公告日期）代表数据修订版本 → 这正是题目要求的「去重，保留最新版本」场景。
4. **去重字段**：财务指标用 `ISNEW`（"1"=最新），利润表/资产负债表用 `DATA_STATE`。
5. **报告期类型**：`DATE_TYPE_CODE` 001=年报、002=半年报、003=一季报、004=三季报。
6. **字段名全大写**：API 返回 `TOTAL_OPERATE_INCOME` 等，需映射为规范 snake_case。

### 2.2 环境验证

- Python 3.12.3，但系统 python 的 pandas 因缺 numpy 损坏，pip 未安装
- 现有 venv 都不完整（validator/dagster 等缺包）
- 解决：用 `get-pip.py` 引导 pip，创建独立 venv，安装 pandas/numpy/requests/lxml/openpyxl/pyyaml/pytest
- 网络慢（~20KB/s），影响爬取规模决策（最终定 50 家公司）

## 3. 架构决策

### 3.1 分层架构

```
┌─────────────────────────────────────────────────────┐
│                    CLI 入口层                         │
│        crawl / clean / features / quality /          │
│        incremental / backtest                        │
├─────────────────────────────────────────────────────┤
│  爬虫层        存储层         清洗层        特征层    │
│  crawler  →  storage  →  cleaning  →  features       │
│  (反爬)      (SQLite)    (流水线)    (注册表)        │
├─────────────────────────────────────────────────────┤
│              附加项层  addons                         │
│        quality / incremental / backtest              │
├─────────────────────────────────────────────────────┤
│              工具层  utils                            │
│           logger / date_utils                        │
└─────────────────────────────────────────────────────┘
```

每层职责单一、依赖单向（上层依赖下层），便于独立测试和替换。

### 3.2 数据流

```
东方财富 API
    │  爬虫(反爬: 频率控制+UA伪装+重试)
    ▼
原始表 raw_*  (唯一键含 notice_date，保留多版本)
    │  清洗流水线: 去重→异常值→缺失值→时间对齐
    ▼
清洗表 clean_* (唯一键仅 secucode+report_date，去重后单条)
    │  特征引擎: 三时点上下文 → 已注册特征
    ▼
特征表 features (长表: secucode+report_date+feature_name+feature_value)
    │  回测: 分组 → 收益代理
    ▼
回测报告

旁路: clean_log(清洗日志) / quality_report(质量报告) / crawl_watermark(增量水位)
```

### 3.3 关键设计决策

| 决策 | 方案 | 理由 |
|------|------|------|
| 原始表唯一键含 notice_date | (secucode, report_date, notice_date) | 保留同一报告期的多个披露版本，清洗时去重取最新 |
| 清洗表唯一键仅 (secucode, report_date) | 去重后每公司每报告期仅一条 | 满足「保留最新版本」要求 |
| 特征表用长表格式 | feature_name + feature_value | 新增特征无需改表结构，满足可扩展性 |
| 字段映射集中管理 | field_mapping.py 一处维护 | 大写→snake_case 统一，便于新增数据源 |
| 特征注册用装饰器 | @FeatureRegistry.register | 零侵入，新特征只需定义类+装饰器+import |
| FeatureContext 三时点 | current/previous/year_ago | 覆盖同比、环比、水平值三类特征 |
| 增量水位按 (source, secucode) | 精确到每公司每数据源 | 支持细粒度增量 |
| 写入前过滤表列 | _get_table_columns 缓存 | 防止映射后多余字段导致入库失败 |

## 4. 模块设计

### 4.1 爬虫模块 `crawler/`

**反爬策略**（题目要求）：
- `rate_limiter.py`：令牌桶模式，`min_interval`（默认 1.5s）控制请求间隔，线程安全
- `base.py`：`BaseCrawler` 封装 `requests.Session`，UA 伪装 + Referer；`_build_filter_string` 构建 filter；`_request_with_retry` 指数退避重试（2/4/8s），业务错误不重试
- `eastmoney.py`：`fetch_page` / `fetch_all`（自动分页）/ `fetch_by_company`（按公司+日期范围）
- `stock_list.py`：从 push2 API 获取 A 股列表，`f13` 区分市场（0=SZ, 1=SH）

**设计要点**：`BaseCrawler` 是抽象基类，新增数据源只需继承并实现 `fetch_page`/`fetch_all`，反爬与重试逻辑复用。

### 4.2 存储模块 `storage/`

- `field_mapping.py`：三个 dict 映射大写→snake_case，`map_fields(raw_record, report_name)`
- `schema.py`：全部表 DDL + 索引定义
- `database.py`：SQLite 连接管理，WAL 模式，外键开启，`init_schema()` 幂等
- `repository.py`：数据访问层，`batch_upsert_raw` / `get_raw_by_company` / `upsert_clean` / `upsert_feature` / `get_watermark` / `log_clean_action` 等

**设计要点**：`batch_upsert_raw` 写入前用 `_get_table_columns` 过滤掉映射后多余的列（如 `trade_market`、`org_code`），避免「table has no column」错误。这是实测中发现的问题。

### 4.3 清洗模块 `cleaning/`

**清洗流水线**（题目要求覆盖异常值/缺失值/去重/时间对齐/日志）：

```
去重(dedup) → 异常值检测(outliers) → 缺失值处理(missing) → 时间对齐(time_align)
```

- `rules.py`：`OUTLIER_THRESHOLDS`（字段→(min,max)）、`MISSING_STRATEGY`（drop/zero/keep）、`AMOUNT_MUST_POSITIVE`（应为正的字段）
- `dedup.py`：按 (secucode, report_date) 分组，取 notice_date 最新，记日志
- `outliers.py`：超阈值置 NULL + 记日志；应为正的字段出现负值置 NULL
- `missing.py`：核心字段缺失 drop（无法计算特征），非核心 keep
- `time_align.py`：按报告期推算发布窗口（年报次年1-4月等），标记 on_time/delayed/not_disclosed
- `pipeline.py`：编排四步，每步记 clean_log，返回 CleanStats 统计

**阈值设定依据**：基于 A 股财务数据合理范围，如 ROE ∈ [-200, 200]、资产负债率 ∈ [0, 100]、营收 ∈ [-1e13, 1e13]。

### 4.4 特征模块 `features/`

**可扩展性设计**（题目要求）：

```python
@FeatureRegistry.register
class MyFeature(Feature):
    name = "my_feature"
    group = "profitability"
    def calculate(self, ctx: FeatureContext) -> float | None: ...
```

新增特征只需：定义类 + 装饰器 + 在 engine.py import（触发注册），无需改引擎或其他代码。

**FeatureContext 三时点**：
- `current`：当期三表合并数据
- `previous`：上一报告期（环比基准）
- `year_ago`：去年同期（同比基准）

**12 个特征**：

| 组 | 特征 | 计算 |
|----|------|------|
| 盈利能力 | net_profit_margin | 归母净利润/营收 |
| 盈利能力 | gross_margin_feat | API 毛利率 |
| 盈利能力 | roe_change / roe_change_rate | ROE 变化/变化率 |
| 成长性 | revenue_yoy / revenue_qoq | 营收同比/环比 |
| 成长性 | profit_yoy / profit_qoq | 净利润同比/环比 |
| 偿债能力 | debt_asset_ratio_change | 资产负债率变化 |
| 偿债能力 | current_ratio_change | 流动比率变化 |
| 偿债能力 | equity_ratio / debt_asset_ratio | 权益比率/资产负债率 |

**设计要点**：成长性特征优先用 API 直接提供的增速字段（YSTZ/SJLTZ 等），缺失时用原始金额自行计算，保证数据完整性。`safe_divide`/`pct_change` 提供除零保护。

### 4.5 附加项 `addons/`

- `quality.py`：各表记录数、关键字段缺失率、清洗保留率、特征值覆盖率 → quality_report 表
- `incremental.py`：读水位 → 仅爬水位之后新数据 → 更新水位
- `backtest.py`：按特征值排序分 N 组，计算各组下期收益代理（净利润同比增速），输出多空收益

**回测收益代理决策**：本项目仅爬财务数据无行情数据，采用「下一报告期净利润同比增速」作为收益代理，纯财务数据驱动。

## 5. 数据模型

### 5.1 表结构

| 表名 | 用途 | 唯一键 |
|------|------|--------|
| stock_list | 股票列表 | secucode |
| raw_financial_indicator | 原始财务指标 | (secucode, report_date, notice_date) |
| raw_income_statement | 原始利润表 | (secucode, report_date, notice_date) |
| raw_balance_sheet | 原始资产负债表 | (secucode, report_date, notice_date) |
| clean_financial_indicator | 清洗后财务指标 | (secucode, report_date) |
| clean_income_statement | 清洗后利润表 | (secucode, report_date) |
| clean_balance_sheet | 清洗后资产负债表 | (secucode, report_date) |
| features | 特征长表 | (secucode, report_date, feature_name) |
| clean_log | 清洗日志 | id 自增 |
| crawl_watermark | 增量水位 | (source_name, secucode) |
| quality_report | 质量报告 | id 自增 |

### 5.2 设计要点

- **原始 vs 清洗分离**：原始表保留全部版本（含 notice_date），清洗表去重后单条，可追溯
- **特征长表**：feature_name + feature_value，新增特征无需 ALTER TABLE
- **索引**：所有表建 secucode + report_date 索引，加速按维度查询
- **时间戳**：各表保留 crawled_at / cleaned_at / computed_at

## 6. 可扩展性

### 6.1 新增数据源

1. `crawler/` 下继承 `BaseCrawler`，实现 `fetch_page`/`fetch_all`
2. `field_mapping.py` 加映射 dict
3. `schema.py` 加 raw/clean 表 DDL
4. `config.yaml` 加配置
5. `cli.py` 注册爬取逻辑

### 6.2 新增特征

1. `features/` 下继承 `Feature`，设置 name/group/description
2. 实现 `calculate(ctx)` 方法
3. `@FeatureRegistry.register` 装饰
4. `engine.py` import 触发注册
5. 无需改其他代码

## 7. 实现顺序

| 步骤 | 模块 | 产出 |
|------|------|------|
| 1 | 骨架 + 配置 + 存储层 | 可建库、可映射字段 |
| 2 | 爬虫模块 | 可爬取入库 |
| 3 | 清洗模块 | 可清洗 |
| 4 | 特征模块 | 可计算特征 |
| 5 | 附加项 | 质量/增量/回测 |
| 6 | CLI + 测试 | 完整可运行 + 测试通过 |
| 7 | 端到端验证 + 文档 | 最终提交 |

## 8. 验证结果

3 家公司实测端到端：
- 爬取：301 条/表入库 ✓
- 清洗：301→299（丢弃 2 条核心字段缺失），保留率 99.3% ✓
- 特征：12 特征 × 301 组合 = 3612 条，值覆盖率 77.2% ✓
- 回测：分组逻辑、多空收益计算正确 ✓
- 测试：44 个 pytest 全部通过 ✓
