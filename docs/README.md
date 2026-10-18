# 技术参考与历史资料目录

**统一入口：[根README文档索引](../README.md#文档索引)。** 本页用于找到技术细目和历史快照，不再复制另一套当前状态导航。

## 技术定义与契约

| 主题 | 细目 |
|---|---|
| 财务字段与公式 | [阶段1总览](stage-1/README.md)、[数据字典](stage-1/data-dictionary.md)、[公式](stage-1/feature-definitions.md)、[权威字段](stage-1/field-authority.md)、[验收矩阵](stage-1/test-matrix.md) |
| 数据模型及迁移 | [阶段2总览](stage-2/README.md)、[模型](stage-2/data-model.md)、[迁移](stage-2/migration.md)、[验收矩阵](stage-2/test-matrix.md) |
| 原体与采集 | [2B契约](stage-2/2b-contract.md)、[schema](stage-2/2b-schema.md)、[运行](stage-2/2b-operations.md)、[测试映射](stage-2/2b-test-matrix.md) |
| 清洗及冻结输入 | [2C契约](stage-2/2c-contract.md)、[2C操作](stage-2/2c-operations.md)、[2D重放](stage-2/2d-flow.md) |
| 双源及正式披露 | [阶段3总览](stage-3/README.md)、[单公司采样](stage-3/source-sample.md)、[批量采样](stage-3/batch-sampling.md)、[金融字段](stage-3/financial-mapping.md)、[官方参考](stage-3/official-reference.md)、[核对汇总](stage-3/review-batch.md) |
| 截至日与刷新 | [阶段4](stage-4.md) |

## 当前固定结果及可追溯清单

| 内容 | 文件与含义 |
|---|---|
| 正式财报核验与研究 | [三公司来源与规则](verified-coverage.md)、[78项固定事实](../examples/reviewed/README.md)、[滚动计划](../examples/return-study-plan.json) |
| v0.3.0八公司基线 | [展示摘要](showcase-result.json)、[逐特征覆盖复核](feature-coverage-v0.3.0.json)；详细数值见[扩展结果](../showcase/expanded.md) |
| 固定输入来源 | [快照清单](../examples/snapshots/manifest.json)、[84项财报事实](../examples/snapshots/official-reviews.json)、[输入说明](../examples/snapshots/README.md) |
| 正式发布身份 | [v0.4.2资产清单](https://github.com/GGGGGG-Bond/fin_feature_framework/releases/download/v0.4.2/release-manifest.json)、[资产SHA-256](https://github.com/GGGGGG-Bond/fin_feature_framework/releases/download/v0.4.2/SHA256SUMS.txt) |

当前版本为v0.4.2，包含八公司供应商视图、三公司正式财报核验和滚动成本研究，见[项目成果](progress.md)；迭代记录见[版本变更](../CHANGELOG.md)。

## 历史计划与验证快照

| 历史资料 | 阅读约束 |
|---|---|
| [建设路线](implementation-plan.md)、[阶段2初期计划](stage-2-implementation-plan.md) | 记录设计过程；阶段“待实施”不表示当前未实现 |
| [早期验收JSON](acceptance-result.json)、[公开候选验证](public-readiness.json)、[0.2.1面试样例](interview-readiness.json) | 只支持各自版本和当时测试计数，不能合并为最新验收 |
| [阶段3早期快照](stage-3/review-result.json)、[阶段3后续旧快照](stage-3/review-result-20261018.json) | 保留旧34/50/12等分母；不替代v0.3.0新采样核对 |
| [旧三公司样例](../showcase/README.md)、[旧三公司摘要](../showcase/summary.json)、[茅台独立摘要](../showcase/interview-summary.json) | 与默认八公司样本分别统计 |
| [历史迁移说明](../HISTORY.md)、[脱敏历史校验](history-preservation.json) | 说明真实迭代及公开整理；旧提交ID可能因脱敏改变 |

维护方法见[documentation.md](documentation.md)。业务定义仍有效的契约继续作为技术参考，历史授权、审批顺序及“待MR”仅描述当时协作，不能覆盖当前用户指令或AGENTS.md。
