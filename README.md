# 金融数据处理与特征工程框架

0.2.0（2026-10-18）：建设计划阶段0–5的功能闭环与限定样本验收完成。干净checkout+隔离环境770项离线通过；真实接口3请求生成24条特征且24/24重放一致。八家公司96项中34项官方核查一致、50未查、12缺失。先读[交付指南](docs/delivery.md)和[验收记录](docs/acceptance.md)。

> 覆盖「数据爬取 → 结构化存储 → 清洗标准化 → 特征生成 → (质量监控/增量处理/特征回测)」完整链路的可扩展框架。

## 项目概述

本项目面向量化投研场景，从东方财富公开数据接口爬取 A 股上市公司财务数据（利润表、资产负债表、主要财务指标），经结构化存储、清洗标准化后，生成具有金融含义的特征/因子，并提供以后续净利润同比增速为代理的分组分析。现有 CLI 子命令仍名为 `backtest`。

- **数据源**：东方财富为计算主来源；新浪独立留证核验，官方年报抽查。不同接口不自动证明底层供应商独立。
- **存储**：SQLite（轻量、零配置、支持 SQL 按维度查询）
- **语言**：Python 3.10+

## 当前状态（2026-10-18）

版本留证、清洗血缘、12特征、离线重放、双源小样本、截至日查询与修订刷新已接通，并提供只读运行/质量/特征覆盖报告和CSV导出。本轮MR按完整目标组织：16官方核查、17截至日/修订、18交付验收。

只对实际保存且时间可信的观察提供历史选择；生产公开修订日期未知时拒选。真实小样本部分官方核查不等于96项全数财务验收，缺失历史和未知口径不补造。边界与证据见[当前进展](docs/progress.md)。

## 文档索引

详细文档位于 [`docs/`](docs/) 目录；先阅读 [文档入口与维护约定](docs/README.md)。

| 文档 | 内容 | 适合谁读 |
|------|------|----------|
| [交付指南](docs/delivery.md) | 安装、离线/在线运行、报告与退出码 | 使用与演示 |
| [验收记录](docs/acceptance.md) | 各阶段证据、实际计数及范围 | 审阅与交接 |
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
# 本轮测试平台 Linux Python 3.12.3
python3 -m venv .venv
.venv/bin/python -m pip install -c requirements-tested.txt -e '.[dev]'
.venv/bin/python scripts/verify_offline.py --output-dir reports/first-acceptance
```

### 2. 可选在线运行

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
# 本批：770 passed，11 个联网用例未执行
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
│   ├── cli.py                      # CLI 入口（16 个子命令）
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

## 离线闭环演示

```bash
# 人工构造样例，不访问网络；输出目录必须尚不存在
.venv/bin/python -m examples.offline_flow --output-dir data/offline-demo
```

完成采集留证 → 清洗 → 特征 → 重放 → 对账，输出 summary.json / replay.json。示例结果为 3 个财务单元、24 条特征、24 条重放一致、对账退出 0。重放入口与限制见 [2D 流程说明](docs/stage-2/2d-flow.md)。

阶段 3 批量采样：8 家×2 年、固定分母、失败保留和旧证据复用，见 [使用说明](docs/stage-3/batch-sampling.md)。早期采样快照74/96；当前84/96有数字、34官方参考一致，未查/缺失仍保留。

金融样本映射更新：复用原体后，8 家两年双方有效数字覆盖提升至 84/96，12 项定义不同继续缺失；见 [映射说明](docs/stage-3/financial-mapping.md)。
