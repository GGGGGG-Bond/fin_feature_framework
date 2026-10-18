# 财务数据处理与特征工程框架

Python / SQLite / CLI：财务采集 → 清洗 → 12项特征 → 双源与正式财报核验 → 价格收益分组回测。

**无需安装先看结果：**[八公司、双源与收益结果](showcase/expanded.md) · [12项完整样例](showcase/interview.md) · [当前完成度与边界](docs/progress.md)。

**面试交付：**[v0.3.0固定版本与下载](https://github.com/GGGGGG-Bond/fin_feature_framework/releases/tag/v0.3.0) · [一页说明](docs/project-brief.md) · [交付清单与复现](docs/interviewer-handoff.md)。下载delivery.zip解压后打开START_HERE.html，可免安装查看全部结果；包内同时包含源码。results.zip供只读查看，source.zip供单独复现，选一个即可。

## 克隆后运行

需要 **Linux + Python 3.12或更新版本（含venv/pip）**；Windows在WSL中执行。首次安装需要软件包网络，演示本身不请求金融接口。

Windows已装好WSL及其中的Python时，也可双击根目录`run-demo.cmd`，它在Linux临时目录计算，再把查看用文件导回`data/windows-demo/`并尝试打开浏览器。免安装结果包不要求WSL。

```bash
git clone https://github.com/GGGGGG-Bond/fin_feature_framework.git
cd fin_feature_framework
bash run.sh
```

一条命令完成环境准备、默认演示和HTML结果总览。终端会打印 `Overview: .../index.html`，用浏览器打开该文件即可；每次在 `data/demo/` 创建新目录，不覆盖前次结果。

```bash
bash run.sh --open         # 请求本机浏览器打开结果；WSL/无桌面环境可手动打开
bash run.sh --only interview # 单独运行12项完整特征样例
bash run.sh --only all       # 扩展展示与原有四组演示
```

已安装多个Python时可用 `PYTHON=python3.12 bash run.sh` 指定。重复运行会复用依赖环境；测试环境另按[开发指南](docs/development.md)安装pytest。原生Windows暂不支持证据文件所需的目录fsync。

## 默认演示看什么

- **八家公司 × 八季度 × 三表**：茅台、美的、宁德时代、三一、万科、招行、平安、中信证券，覆盖2023Q1—2024Q4。
- 192个报表版本、768条特征、232条有值，768/768冻结输入重放一致；空值保留原因，不为覆盖率放开口径门禁。
- **双源96个位置**：84处双方有值且数值一致；对2024正式年报，81一致、3与后续调整后的比较列一致、12缺值/定义不齐。
- **五组价格收益回测**：正常特征链计算信号，次交易日入场、固定持仓、双边成本、基准、每日净值和回撤；八公司样本验证流程，不证明策略有效。
- 另有茅台三份正式财报27项事实，**2024年末12/12特征有值**，覆盖ROE、同比、单季环比和流动比率变化。
- HTML按公司/期间筛选，展示双方值、正式出处与差异、全部特征及空值原因、净值图、CSV和血缘报告。

扩展展示选定字段来自**2026-10-18真实采集**，保留来源、时间及母原体摘要；Git不分发原体，离线重建API信封并生成独立演示证据，不冒充供应商原体。正式财报信号和12项完整样例则是人工核对事实重建输入。身份与时间分开保存，详见[扩展展示与回测](docs/showcase.md)、[完整特征口径](docs/interview.md)。

## 能力与限制

采集限频与分页、失败单元回滚、原始证据、修订版本、清洗诊断、特征血缘、质量报告和离线重放均已实现。东方财富主来源，新浪辅助核验，第二来源不静默拼接。

本批重新采集并核对八家公司；原先34/50/12是旧快照的历史统计。三一三个调整差异说明“双源相同”不等于核验完成，也不证明上游独立。第二来源仅核验，不拼接计算。

`return-backtest`是新增价格收益功能；旧`backtest`仍是盈利增长代理。回测使用已核对年报的保守公开日期声明，不把当前2026修订冒充2025当时可用。未知修订公开时间、未保存历史、生产调度及全市场验收仍有边界。

## 更多入口

[文档导航](docs/README.md) · [安装与CLI](docs/delivery.md) · [测试与验收](docs/acceptance.md) · [财务定义](docs/stage-1/README.md)。阶段契约保留历史设计背景，当前状态以progress/acceptance为准。

提供[Codespaces配置](.devcontainer/devcontainer.json)，但云端实例尚未实测。它需要GitHub登录及可用额度，不是免登录网页演示；见[GitHub说明](https://docs.github.com/en/codespaces/developing-in-a-codespace/creating-a-codespace-for-a-repository)。

保留经脱敏的96个原始迭代提交和18个合并关系；后续公开改动继续正常提交。[历史说明](HISTORY.md)。尚未指定开源许可证。
