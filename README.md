# 金融数据处理与特征工程框架

> 覆盖「数据爬取 → 结构化存储 → 清洗标准化 → 特征生成 → (质量监控/增量处理/特征回测)」完整链路的可扩展框架。

## 项目概述

本项目面向量化投研场景，从东方财富公开数据接口爬取 A 股上市公司财务数据（利润表、资产负债表、主要财务指标），经结构化存储、清洗标准化后，生成具有金融含义的特征/因子，并提供以后续净利润同比增速为代理的分组分析。现有 CLI 子命令仍名为 `backtest`。

- **数据源**：东方财富 datacenter-web API（公开 JSON 接口，无需登录）
- **存储**：SQLite（轻量、零配置、支持 SQL 按维度查询）
- **语言**：Python 3.10+

## 当前状态（2026-10-17）

阶段 1、2A、2B 已合入；MR!8 合入 main `194610b`，与送审头 `d41673d` 文件树一致。本批从该 main 实施 2C，代码 `3b3c5d8` + 季度报告期门禁 `9f9a26d`（分支 `feat/stage2c`），完成实现与自验证，待 MR 审阅。全量离线 597 passed、11 deselected，新增 45 项；未调用真实接口、未升级真实业务库。2D 尚未开始。

采集已接入原始响应文件、逐次 HTTP 尝试、版本与观察血缘、单元事务和只读对账。特征输入已接入版本清洗结果；第二来源、离线重放和历史时点查询仍属后续工作，详见 [当前进展](docs/progress.md)。采集配置与退出码见 [2B 操作指南](docs/stage-2/2b-operations.md)，清洗、血缘与 v5 升级说明见 [2C 操作指南](docs/stage-2/2c-operations.md)。

## 文档索引

详细文档位于 [`docs/`](docs/) 目录；先阅读 [文档入口与维护约定](docs/README.md)。

| 文档 | 内容 | 适合谁读 |
|------|------|----------|
| [阶段 1 契约](docs/stage-1/README.md) | 数据字典、12 个特征、字段权威与验收矩阵 | 实现 agent 开工与架构审阅 |
| [docs/progress.md](docs/progress.md) | 当前状态、开发批次、验证与待办 | 协作交接与查看进展 |
| [docs/decisions.md](docs/decisions.md) | 设计选择、备选方案、理由与边界 | 理解思考与权衡 |
| [docs/implementation-plan.md](docs/implementation-plan.md) | 后续建设阶段与验收契约 | 规划和审阅实现 |
| [docs/requirements.md](docs/requirements.md) | 原始需求（考核题目原文 + 落地决策） | 了解项目要做什么、验收基准 |
| [docs/architecture.md](docs/architecture.md) | 架构设计：从 0 到 1 怎么做的（调研、决策、模块、数据模型） | 理解为什么这样设计 |
| [docs/development.md](docs/development.md) | 开发文档：环境配置、踩坑记录、维护指南、变更记录 | 修改和维护项目 |
| [docs/gaps-and-weaknesses.md](docs/gaps-and-weaknesses.md) | 项目缺口与弱点（14 项，含优先级与改进建议） | 迭代完善、技术债管理 |

## 快速开始

### 1. 环境准备

```bash
# Python 3.10+，venv 已创建于 .venv/
.venv/bin/pip install pandas numpy requests lxml openpyxl pyyaml pytest
```

### 2. 端到端运行

以下采集命令会访问公开接口并写入配置数据库，首次使用先检查 [操作指南](docs/stage-2/2b-operations.md) 的范围、备份和退出码。自定义配置写在子命令前：`python -m fin_feature.cli --config /path/config.yaml crawl`。本批仅做临时库与脚本化 HTTP 验证。

```bash
# 1. 爬取数据（默认 50 家公司，可在 config.yaml 调整 stock_count）
.venv/bin/python -m fin_feature.cli crawl

# 2. 清洗数据
.venv/bin/python -m fin_feature.cli clean

# 3. 计算特征
.venv/bin/python -m fin_feature.cli features

# 4. 生成质量报告
.venv/bin/python -m fin_feature.cli quality

# 5. 对本地 stock_list 全部公司按 recent_periods 回扫（不按水位过滤）
.venv/bin/python -m fin_feature.cli incremental

# 6. 特征回测（按特征值分5组，计算各组下期收益代理）
.venv/bin/python -m fin_feature.cli backtest --feature roe_change --date 2024-12-31
```

### 3. 验证数据

先确认 `data/finance.db` 来自实际采集且存在，再执行以下查询；当前项目没有本次验收数据库。

```bash
sqlite3 data/finance.db "SELECT 'raw_fi', COUNT(*) FROM raw_financial_indicator
  UNION ALL SELECT 'clean_fi', COUNT(*) FROM clean_financial_indicator
  UNION ALL SELECT 'features', COUNT(*) FROM features;"

sqlite3 data/finance.db "SELECT * FROM features WHERE secucode='002501.SZ' AND report_date='2024-12-31';"
sqlite3 data/finance.db "SELECT * FROM clean_log LIMIT 10;"
```

### 4. 运行测试

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/ -q -p no:cacheprovider -m 'not requires_network'
# 本批：597 passed，11 个联网用例未执行
```

## 目录结构

```
fin_feature_framework/
├── docs/                           # 文档
│   ├── requirements.md             # 原始需求
│   ├── architecture.md             # 架构设计
│   └── development.md              # 开发文档
├── config.yaml                     # 主配置
├── pyproject.toml                  # 项目元数据与依赖
├── fin_feature/                    # 主包
│   ├── cli.py                      # CLI 入口（7 个子命令）
│   ├── config.py / constants.py
│   ├── crawler/                    # 爬虫（反爬：频率控制+UA伪装+重试）
│   ├── ingestion/                  # 留证请求、分页、发现与财务协调器
│   ├── storage/                    # SQLite、证据文件与只读对账
│   ├── cleaning/                   # 清洗流水线
│   ├── features/                   # 特征引擎（装饰器注册，可扩展）
│   ├── addons/                     # 附加项（质量/增量/回测）
│   └── utils/                      # 日志/日期工具
├── tests/                          # pytest（默认离线验证，联网用例显式选择）
├── data/  reports/  logs/          # 运行时生成
└── README.md                       # 本文件（文档索引）
```

## 核心特性

- **完整链路**：爬取 → 存储 → 清洗 → 特征 → 回测，一条命令一步
- **反爬策略**：令牌桶频率控制 + UA 伪装 + 指数退避重试
- **规范清洗**：去重（保留最新披露版本）→ 异常值检测 → 缺失值处理 → 时间对齐，全程记日志
- **可扩展特征**：`@FeatureRegistry.register` 装饰器，新特征零侵入接入
- **附加项**：数据质量监控、按报告期回扫（水位仅作记录）、盈利增长分组分析、原始证据只读对账
- **证据与失败行为**：逐次保留原始响应；采集单元失败回滚业务写入，失败证据保留供只读对账；真实来源验收另行记录

## 配置

主配置在 [config.yaml](config.yaml)，关键项：

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| database.path | SQLite 路径 | data/finance.db |
| crawler.min_interval | 请求间隔（秒） | 1.5 |
| crawler.max_retries | 每页最多尝试数，含首次 | 3 |
| crawl.recent_periods | 最近不同报告期及其中全部修订；null 为全部历史 | 4 |
| crawl.max_pages_per_unit | 分页安全上限，截断即失败 | 1000 |
| raw_store.root | 相对数据库目录的证据根；null 使用同目录 raw/ | null |
| raw_store.max_body_bytes | 单响应原字节/解压上限 | 33554432 |
| crawl.stock_count | 爬取公司数 | 50 |
| backtest.n_groups | 回测分组数 | 5 |

采集上限、证据路径、退出码和对账命令见 [2B 操作指南](docs/stage-2/2b-operations.md)，开发说明见 [docs/development.md](docs/development.md)。
