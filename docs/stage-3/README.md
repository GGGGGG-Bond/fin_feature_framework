# 双源核验与正式披露抽查

> 本文保留阶段规则与历史交付记录；当前完成度及未完成项以[当前进展](../progress.md)为准。

当前限定结果：8家×2年×6字段=96项，84项双方有数值；34项正式财报核查一致、50项数值未人工核查、12项缺字段/定义。数据范围与[固定快照](review-result-20261018.json)一致，不能称为96项全部通过。

- [来源采样](source-sample.md)
- [固定公司采样](batch-sampling.md)
- [金融字段映射](financial-mapping.md)
- [正式财报参考核查](official-reference.md)
- [统一核查汇总](review-batch.md)

离线差异示例：

```bash
.venv/bin/python -m fin_feature reconcile --left examples/reconciliation/primary.json --right examples/reconciliation/secondary.json
```

构造例包含一致、冲突、不可比和缺失，退出1为预期。第二来源只核验，不拼接到主结果。相关操作文档中的早期样本数字是历史快照，当前结果以上方固定快照为准。
