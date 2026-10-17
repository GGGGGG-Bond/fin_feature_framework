# 项目缺口与弱点

2026-10-18：此文为初始分析档案，不是当前未完成清单。已交付版本/观察留证、双源小样本、只读as-of特征、修订传播和重放；当前分组仍是现存版本的描述性盈利增长分析。当前验收/未验证数据和可选增强统一见[验收记录](acceptance.md)，不把下方历史提议视作本轮新增需求。

本文保留初始缺口分析，不作为实时完成状态。2026-10-14 实机审阅新增的正确性问题、当前优先级及修复状态统一见 [开发记录](progress.md)；后续建设范围与验收见 [建设计划](implementation-plan.md)。此处的改进建议尚未因文档维护而实现。

> 本文档记录当前框架的已知缺口、弱点与待改进项，用于迭代更新完善。每项标注**严重程度**和**改进建议**，已完成的打 ✅。
>
> 维护方式：本文补充问题分析与改进建议；实时状态和验收记录统一维护在 progress.md，新增问题使用对应任务编号关联。

---

## 缺口总览

| 领域 | 高优先级 | 中优先级 | 低优先级 |
|------|----------|----------|----------|
| 特征工程 | point-in-time 对齐缺失 | 无标准化/中性化 | — |
| 回测框架 | — | 无 IC/换手率、仅单期 | 无多空组合权重 |
| 爬虫 | 无断点续爬 | 串行无并发 | 无代理池 |
| 数据质量 | 无跨表一致性校验 | — | — |
| 工程化 | — | 无日志轮转、config 不支持多源 | 无 CI |

---

## 一、特征工程

### 1.1 【高】point-in-time 对齐未真正用于特征计算

**现状**：`time_align.py` 计算了 `disclosure_status`（on_time/delayed/not_disclosed），但特征引擎 `engine.py` 构建 `FeatureContext` 时**未使用 notice_date 做 point-in-time 对齐**。查找上期/去年同期数据时，仅按 `report_date` 匹配，未校验该数据在分组日是否已实际披露。

**影响**：回测时可能引入未来函数（look-ahead bias）——分组日当天，某公司的年报可能尚未公告，但特征计算却用到了该报告期数据。

**改进建议**：
- `FeatureContext` 增加一个 `as_of_date`（分组日/评估日）参数
- `_build_context` 查找上期/去年同期时，加 `notice_date <= as_of_date` 过滤
- 回测 `run_backtest` 传入 formation_date 作为 as_of_date

**涉及文件**：[fin_feature/features/engine.py](../fin_feature/features/engine.py)、[fin_feature/features/base.py](../fin_feature/features/base.py)、[fin_feature/addons/backtest.py](../fin_feature/addons/backtest.py)

### 1.2 【中】无特征标准化/中性化

**现状**：特征值为原始计算值，无 z-score 标准化、无行业/市值中性化、无极值缩尾（winsorize）。

**影响**：不同特征量纲差异大（如净利率 0.2 vs 资产负债率 60），回测分组时极端值影响分组均衡性。

**改进建议**：
- `features/` 增加 `postprocess.py`，提供 `standardize()`（z-score）、`winsorize()`（缩尾）、`neutralize()`（行业中性化）
- 在 `engine.compute_all` 后增加可选的后处理步骤
- config 增加 `features.postprocess` 配置开关

**涉及文件**：新增 `fin_feature/features/postprocess.py`，修改 [fin_feature/features/engine.py](../fin_feature/features/engine.py)

---

## 二、回测框架

### 2.1 【中】缺少 IC / 换手率等评价指标

**现状**：回测仅输出各组平均收益和多空收益，无信息系数（IC）、IC 衰减、分组单调性、换手率等量化常用评价指标。

**改进建议**：
- `backtest.py` 增加 `compute_ic()`（特征值与下期收益的 Rank IC）
- 增加 `compute_monotonicity()`（分组收益单调性检验）
- 报告中增加 IC 均值、ICIR、分组单调性指标

**涉及文件**：[fin_feature/addons/backtest.py](../fin_feature/addons/backtest.py)

### 2.2 【中】仅支持单期回测，无多期累计

**现状**：回测仅在单个 formation_date 分组，计算下一期收益，不支持多期调仓、累计收益曲线。

**改进建议**：
- `run_backtest` 支持 `formation_dates: list`（多期）
- 增加累计净值曲线、年化收益、夏普比率
- 输出多期 IC 时序

**涉及文件**：[fin_feature/addons/backtest.py](../fin_feature/addons/backtest.py)

### 2.3 【低】无多空组合权重优化

**现状**：多空收益为简单的 Q5 - Q1 等权差。无市值加权、波动率加权等组合构建方式。

**改进建议**：增加加权方式选项（等权/市值加权），config 配置。

---

## 三、爬虫模块

### 3.1 【高】无断点续爬

**现状**：`crawl` 命令若中途失败（网络中断、进程被杀），已爬数据虽已入库（upsert 幂等），但**无法从断点继续**，重跑会从头开始，浪费已完成的请求。

**影响**：50 家公司爬取约 5-10 分钟，中途失败需全量重跑。网络不稳定时体验差。

**改进建议**：
- `crawl_watermark` 表已有水位机制，`crawl` 命令可复用：爬取前检查水位，跳过已爬公司
- 或增加 `--resume` 参数，读取已完成的公司列表，仅爬未完成的
- 爬取每家公司后立即更新水位（而非全部完成后）

**涉及文件**：[fin_feature/cli.py](../fin_feature/cli.py) `cmd_crawl`、[fin_feature/addons/incremental.py](../fin_feature/addons/incremental.py)

### 3.2 【中】串行爬取，无并发

**现状**：爬虫串行执行，每家公司每报表串行请求，受 `min_interval=1.5s` 限制。50 家 × 3 报表 × 多页，耗时较长。

**改进建议**：
- 用 `concurrent.futures.ThreadPoolExecutor` 并发爬取不同公司
- 注意：并发度需控制（建议 3-5），避免触发反爬封禁
- `RateLimiter` 已线程安全，可支持并发

**涉及文件**：[fin_feature/cli.py](../fin_feature/cli.py) `cmd_crawl`、[fin_feature/crawler/rate_limiter.py](../fin_feature/crawler/rate_limiter.py)

### 3.3 【低】无代理池 / 验证码处理

**现状**：仅 UA 伪装 + 频率控制，无代理 IP 轮换、无验证码识别。

**说明**：东方财富 datacenter-web 接口反爬较弱，当前策略已够用。若未来接口加强反爬或需大规模爬取，再考虑。

---

## 四、数据质量

### 4.1 【高】无跨表一致性校验

**现状**：三张表（财务指标/利润表/资产负债表）独立清洗，未校验跨表一致性。例如：
- 财务指标的 `parent_netprofit` 与利润表的 `parent_netprofit` 应一致
- 财务指标的 `total_operate_income` 与利润表应一致
- 资产负债表的 `total_liabilities / total_assets` 应约等于 `debt_asset_ratio`

**影响**：同一指标在不同表可能数值不一致（数据源差异或更新不同步），特征计算时取值可能矛盾。

**改进建议**：
- `cleaning/` 增加 `consistency.py`，清洗后做跨表核对
- 不一致时记日志，以某一表为准（如利润表为准）或标记 `inconsistent`
- `quality.py` 增加跨表一致性指标

**涉及文件**：新增 `fin_feature/cleaning/consistency.py`，修改 [fin_feature/cleaning/pipeline.py](../fin_feature/cleaning/pipeline.py)、[fin_feature/addons/quality.py](../fin_feature/addons/quality.py)

### 4.2 【低】异常值阈值静态，无自适应

**现状**：异常值阈值在 `rules.py` 硬编码，基于经验设定，未按行业/时间自适应。

**改进建议**：支持按行业分组的动态阈值（如 3σ），或 config 按行业配置。优先级低，当前阈值已覆盖主要异常。

---

## 五、工程化

### 5.1 【中】无日志轮转

**现状**：`logger.py` 用 `FileHandler`，日志文件无限增长，无大小/时间轮转。

**改进建议**：改用 `RotatingFileHandler(maxBytes=10MB, backupCount=5)`。

**涉及文件**：[fin_feature/utils/logger.py](../fin_feature/utils/logger.py)

### 5.2 【中】config 不支持多数据源

**现状**：`config.yaml` 的 `crawler` 段为单一数据源配置，无法同时配置多个数据源。

**改进建议**：改为 `sources` 列表，每项含 `name`/`base_url`/`report_names` 等，支持多源混合爬取。

**涉及文件**：[config.yaml](../config.yaml)、[fin_feature/config.py](../fin_feature/config.py)、[fin_feature/cli.py](../fin_feature/cli.py)

### 5.3 【低】无 CI / 自动化测试

**现状**：测试需手动运行 `pytest`，无 CI 自动触发。

**改进建议**：增加 GitHub Actions / GitLab CI 配置，push 时自动跑测试。优先级低，项目规模尚小。

### 5.4 【低】爬虫测试未 mock HTTP

**现状**：`test_crawler.py` 测试 filter 构建、字段映射等逻辑，但未 mock `requests` 测试重试机制和分页逻辑。

**改进建议**：用 `unittest.mock.patch` mock `session.get`，测试重试次数、异常处理、分页拼接。

**涉及文件**：[tests/test_crawler.py](../tests/test_crawler.py)

---

## 六、功能覆盖

### 6.1 【低】无数据导出功能

**现状**：数据仅在 SQLite 中，无导出为 CSV/Excel 的命令。

**改进建议**：CLI 增加 `export` 子命令，支持按公司/报告期导出特征表为 CSV/Excel（openpyxl 已装）。

### 6.2 【低】无 Web 可视化

**现状**：质量报告、回测报告为文本输出，无可视化界面。

**改进建议**：可选增加简单的 HTML 报告（回测分组柱状图、质量缺失率热力图）。优先级低，文本报告已满足考核要求。

---

## 迭代优先级建议

按「投入产出比」排序，建议迭代顺序：

1. **3.1 断点续爬** — 改动小（复用水位），体验提升大
2. **1.1 point-in-time 对齐** — 回测严谨性核心，考核可能被问
3. **4.1 跨表一致性校验** — 数据质量核心，体现严谨性
4. **2.1 IC/换手率** — 回测专业度提升
5. **5.1 日志轮转** — 一行改动
6. **1.2 特征标准化** — 量化常见处理
7. **3.2 并发爬取** — 性能提升
8. **2.2 多期回测** — 回测完整度

---

## 变更记录

| 日期 | 变更 |
|------|------|
| 2026-10-14 | 初始梳理，记录 14 项缺口/弱点 |
