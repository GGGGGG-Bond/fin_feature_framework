# 财务数据处理与特征工程框架

Python / SQLite / CLI：公开财务采集 → 清洗 → 12项特征 → 双源与正式财报核验 → 质量、重放与价格收益。

[![Offline acceptance](https://github.com/GGGGGG-Bond/fin_feature_framework/actions/workflows/offline.yml/badge.svg)](https://github.com/GGGGGG-Bond/fin_feature_framework/actions/workflows/offline.yml)

**本页是文档总入口。** 固定交付版本为[v0.4.2](https://github.com/GGGGGG-Bond/fin_feature_framework/releases/tag/v0.4.2)；main中的文档持续维护，已发布标签和下载文件保持冻结。

## 快速入口

| 你想做什么 | 从这里开始 |
|---|---|
| 不安装环境，先看完整结果 | [下载完整交付包](https://github.com/GGGGGG-Bond/fin_feature_framework/releases/download/v0.4.2/fin-feature-framework-v0.4.2-delivery.zip)，解压打开START_HERE.html |
| 只想快速在线阅读 | [八公司、双源及收益结果](showcase/expanded.md)、[三公司36项完整计算](docs/verified-coverage.md) |
| 三分钟了解项目 | [一页说明](docs/project-brief.md)、[PDF下载](https://github.com/GGGGGG-Bond/fin_feature_framework/releases/download/v0.4.2/project-brief-v0.4.2.pdf) |
| 下载与复现 | [交付消息模板及链接](docs/submission-message.md)、[文件选择与复现说明](docs/interviewer-handoff.md) |
| 核对是否完成、还有哪些缺口 | [当前进展](docs/progress.md)、[验收证据](docs/acceptance.md)、[缺口与升级优先级](docs/gaps-and-weaknesses.md) |

完整包已含源码、HTML、CSV/JSON、PDF与校验清单，不必重复下载三种ZIP。只读查看可选[结果包](https://github.com/GGGGGG-Bond/fin_feature_framework/releases/download/v0.4.2/fin-feature-framework-v0.4.2-results.zip)；需要完整Git历史时克隆仓库。

## 一键运行

Linux/WSL需要Python≥3.12（含venv/pip）。首次安装需要软件包网络，默认演示不访问金融接口。

```bash
git clone https://github.com/GGGGGG-Bond/fin_feature_framework.git
cd fin_feature_framework
bash run.sh
```

终端打印结果页路径。Windows可使用下列原生Python命令；已有WSL及其中的Python时，也可双击源码根目录的`run-demo.cmd`或完整包的`RUN_DEMO.cmd`，结果导回`data/windows-demo/`。免安装查看不需要Python或WSL。

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -c requirements-tested.txt -e .
.venv\Scripts\python.exe -X utf8 scripts/demo.py --open
```

原生Windows需要Python≥3.12和支持硬链接的本地文件系统（如NTFS）；普通演示无需管理员或符号链接权限。Windows跳过目录fsync，不承诺与Linux相同的断电持久性；完整测试中的符号链接安全用例另需开发者模式或相应权限。

```bash
bash run.sh --open            # 尝试打开默认浏览器
bash run.sh --only interview  # 只运行12项完整特征样例
bash run.sh --only all        # 扩展展示及其他四组演示
```

复现正式交付时，克隆命令增加`--branch v0.4.2`即可固定版本。运行失败、平台前提及下载校验见[交付指南](docs/interviewer-handoff.md)。

## 文档索引

| 主题 | 权威入口 | 内容与使用场景 |
|---|---|---|
| 范围与完成度 | [功能范围](docs/requirements.md)、[当前进展](docs/progress.md) | 哪些要求已实现，限定范围是什么 |
| 验证与已知缺口 | [验收记录](docs/acceptance.md)、[覆盖明细](docs/gaps-and-weaknesses.md) | 实测环境、样本分母、空值原因、升级顺序 |
| 成果与运行 | [一页说明](docs/project-brief.md)、[交付指南](docs/interviewer-handoff.md)、[发送消息](docs/submission-message.md) | 看什么、下载什么、怎么发送与复现 |
| 正式财报与滚动研究 | [三公司完整计算与时间依据](docs/verified-coverage.md)、[事实清单](examples/reviewed/README.md) | 78项事实、36/36目标结果、滚动形成与成本敏感性 |
| 示例与收益口径 | [扩展展示规则](docs/showcase.md)、[完整特征样例口径](docs/interview.md) | 真实采样、事实重建、双源差异、回测假设 |
| 安装、CLI、开发 | [运行操作](docs/delivery.md)、[开发与发布](docs/development.md) | 环境、采集范围、测试、打包与校验 |
| 财务定义 | [字段字典](docs/stage-1/data-dictionary.md)、[12项公式](docs/stage-1/feature-definitions.md)、[字段权威](docs/stage-1/field-authority.md) | 单位、累计/单季、比较基数、缺失与行业适用性 |
| 架构与决策 | [架构](docs/architecture.md)、[设计决策](docs/decisions.md) | 模块职责、取舍及升级边界 |
| 证据、事务、版本 | [阶段2技术参考](docs/stage-2/README.md)、[数据模型](docs/stage-2/data-model.md)、[迁移](docs/stage-2/migration.md) | 原体、尝试、版本/观察、原子性、冻结计算输入 |
| 双源与正式披露 | [阶段3技术参考](docs/stage-3/README.md)、[金融字段映射](docs/stage-3/financial-mapping.md) | 第二来源仅核验、差异/不可比/缺失、官方出处 |
| 截至日与修订 | [历史查询与刷新](docs/stage-4.md) | 系统观察/公开时间、修订失效、保存历史的边界 |
| 文档维护与历史 | [维护规则](docs/documentation.md)、[参考目录](docs/README.md)、[历史说明](HISTORY.md) | 文档职责、更新检查、历史计划/快照的位置 |

## v0.4.2结果与边界

- 八家公司、2023Q1—2024Q4、三类报表：192版本，768条特征，232条有值，768/768冻结输入重放一致。
- 正式财报视图：贵州茅台、美的集团、宁德时代共78项事实，2024年末目标36/36有值；全144位置中57有值、144/144重放一致。供应商未知口径仍拒算，两种输入分别统计。
- 双源96位置：84处双方有值且数值一致；对2024年报81一致、3对应后续调整比较列、12缺值/定义不齐。两网站相同不证明独立上游或修订口径一致。
- `return-backtest`提供五组价格收益；`return-study`提供三个滚动区间、0/10/30bps成本、全样本/非金融/制造示例三个子集，共9场景。`backtest`用于盈利增长代理分组。手选小样本不证明策略有效。
- 离线回归与完整验收见[验收记录](docs/acceptance.md)；GitHub已实际运行Linux Python3.12/3.14矩阵。11项联网测试已补跑通过（`tests/test_e2e.py`），不并入离线验收；正式交付另核对解压、Windows/WSL启动和匿名下载。

字段来自真实采样，离线API信封为重建；Git及交付包不分发母原体或数据库。正式财报信号使用人工核对事实及保守公开日期声明，不恢复未知历史。全市场、生产调度、Codespaces均不列为已验证能力。许可证尚未指定。

## 维护说明

当前状态以progress为准，执行证据以acceptance及固定Release清单为准，财务公式以stage1-v1定义为准。历史阶段文档保留其日期和限制，不当作当前待办或新的审批要求。更新文档后运行`python scripts/check_docs.py`；完整验收脚本也会检查本地链接。具体职责与更新规则见[文档维护](docs/documentation.md)，发布变化见[版本变更](CHANGELOG.md)。
