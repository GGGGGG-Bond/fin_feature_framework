# 金融数据处理与特征工程框架

> 覆盖「数据爬取 → 结构化存储 → 清洗标准化 → 特征生成 → (质量监控/增量处理/特征回测)」完整链路的可扩展框架。

## 项目概述

本项目面向量化投研场景，从东方财富公开数据接口爬取 A 股上市公司财务数据（利润表、资产负债表、主要财务指标），经结构化存储、清洗标准化后，生成具有金融含义的特征/因子，并支持特征回测。

- **数据源**：东方财富 datacenter-web API（公开 JSON 接口，无需登录）
- **存储**：SQLite（轻量、零配置、支持 SQL 按维度查询）
- **语言**：Python 3.10+

## 文档索引

详细文档位于 [`docs/`](docs/) 目录：

| 文档 | 内容 | 适合谁读 |
|------|------|----------|
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

```bash
# 1. 爬取数据（默认 50 家公司，可在 config.yaml 调整 stock_count）
.venv/bin/python -m fin_feature.cli crawl

# 2. 清洗数据
.venv/bin/python -m fin_feature.cli clean

# 3. 计算特征
.venv/bin/python -m fin_feature.cli features

# 4. 生成质量报告
.venv/bin/python -m fin_feature.cli quality

# 5. 增量爬取（仅获取上次之后的新数据）
.venv/bin/python -m fin_feature.cli incremental

# 6. 特征回测（按特征值分5组，计算各组下期收益代理）
.venv/bin/python -m fin_feature.cli backtest --feature roe_change --date 2024-12-31
```

### 3. 验证数据

```bash
sqlite3 data/finance.db "SELECT 'raw_fi', COUNT(*) FROM raw_financial_indicator
  UNION ALL SELECT 'clean_fi', COUNT(*) FROM clean_financial_indicator
  UNION ALL SELECT 'features', COUNT(*) FROM features;"

sqlite3 data/finance.db "SELECT * FROM features WHERE secucode='002501.SZ' AND report_date='2024-12-31';"
sqlite3 data/finance.db "SELECT * FROM clean_log LIMIT 10;"
```

### 4. 运行测试

```bash
.venv/bin/python -m pytest tests/ -v   # 44 个用例全部通过
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
│   ├── cli.py                      # CLI 入口（6 个子命令）
│   ├── config.py / constants.py
│   ├── crawler/                    # 爬虫（反爬：频率控制+UA伪装+重试）
│   ├── storage/                    # SQLite 存储层
│   ├── cleaning/                   # 清洗流水线
│   ├── features/                   # 特征引擎（装饰器注册，可扩展）
│   ├── addons/                     # 附加项（质量/增量/回测）
│   └── utils/                      # 日志/日期工具
├── tests/                          # pytest 测试（44 个用例）
├── data/  reports/  logs/          # 运行时生成
└── README.md                       # 本文件（文档索引）
```

## 核心特性

- **完整链路**：爬取 → 存储 → 清洗 → 特征 → 回测，一条命令一步
- **反爬策略**：令牌桶频率控制 + UA 伪装 + 指数退避重试
- **规范清洗**：去重（保留最新披露版本）→ 异常值检测 → 缺失值处理 → 时间对齐，全程记日志
- **可扩展特征**：`@FeatureRegistry.register` 装饰器，新特征零侵入接入
- **三个附加项**：数据质量监控、增量处理（水位机制）、特征回测（分组回测）
- **测试覆盖**：44 个 pytest 用例，覆盖全部模块

## 配置

主配置在 [config.yaml](config.yaml)，关键项：

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| database.path | SQLite 路径 | data/finance.db |
| crawler.min_interval | 请求间隔（秒） | 1.5 |
| crawl.stock_count | 爬取公司数 | 50 |
| backtest.n_groups | 回测分组数 | 5 |

更多细节见 [docs/development.md](docs/development.md) 的「维护指南」。
