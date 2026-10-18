# 金融企业字段映射补齐

基准 main `7c0014a`（MRrm -rf claude-extract && mkdir -p claude-extract 已合入），分支 feat/stage3-financial-mapping。本批复用已保存的招行、平安、中信证券响应，补总权益和归母净利润的明确代码/标题映射；无新依赖、迁移或业务库写入。

## 映射依据与约束

依据为上批实际保存的 2023/2024 年报响应，逐项检查 item_field、item_title、所属合并报表和数值，没有凭金额大小或接近程度猜字段。

| 标准字段 | 新增新浪代码 | 允许的精确标题 | 本次样本 |
|---|---|---|---|
| total_equity | TOTSHAREQUI | 股东权益 / 所有者权益合计 | 招行、平安 |
| total_equity | TOTALOWNEREQUI | 所有者权益合计 | 中信证券 |
| parent_netprofit | NETPARECOMPPROF | 归属于母公司的净利润 | 招行 |
| parent_netprofit | NETPARESHARPROF | 归属于母公司的净利润 | 平安 |

新候选与原候选同时出现时拒绝歧义，即使数字相同也不静默选一个。代码出现但标题未知时继续拒绝。归母权益和总净利润不能替代上述两项。实际命中替代代码的记录保存 source_field、source_label，mapping_version=source-sample-v2；原映射保持 v1，以保留既有非金融样本的重建兼容性。

这只是确定字段语义映射，不声称本批已完成官方年报数值抽查。unit 和 comparison_basis_id 仍保持 unknown，不从来源元信息或本次匹配推断修订生效时间。金融报表的 BIZINCO“营业收入”不映射成 total_operate_income“营业总收入”，OTHERBIZCOST“其他业务成本”不映射成 operate_cost“营业成本”。不为覆盖率替换定义。

## 使用已收证据离线更新

旧金融样本 JSON 不改动；新映射改变了记录内容，直接复用旧映射文件会被证据重建一致性检查拒绝。显式生成新目录，然后在计划中将 reuse_sample 指向新目录：

```python
from fin_feature.sample_batch import rebuild_saved_sample
rebuild_saved_sample('/path/old-financial-sample', '/path/new-financial-sample')
```

再执行：

```bash
.venv/bin/python -m fin_feature.cli sample-batch \
  --plan /path/new-plan.json --output-dir /path/new-batch
```

新目录保留原清单哈希、原状态、原抓取时间及重新解析时间。原体仍须通过哈希/大小/状态检查，采样器不会因映射改变自动联网。先前已完整映射的五个非金融样本可以直接复用。

## 本批真实结果

2026-10-17，从保存的三个金融公司两年数据离线重建并汇总八家公司：

| 样本 | 原双方有效数字项 | 本批双方有效数字项 | 每家计划项 |
|---|---:|---:|---:|
| 招商银行 | 4 | 8 | 12 |
| 中国平安 | 4 | 8 | 12 |
| 中信证券 | 6 | 8 | 12 |
| 其他五家（各） | 12 | 12 | 12 |

总计 74→84 个有效数字字段对 / 96 项（87.5%），缺失 22→12，8 家解析完成、无失败、本批财务 HTTP 请求数 0。84 项因单位/修订口径未知仍为 incomparable；剩余 12 项为三个金融样本的收入/成本字段，两年各两项。这是映射覆盖进展，不是财务核验通过率。报告退出 1 符合缺失/不可比语义。

结果目录 <local-evidence-directory>；三个新样本在同级 remapped-* 目录，原始目录均保留，真实响应/报告未入 Git。上一批单公司官方参考核查独立于本批，未据此扩大宣称八家公司官方核验完成。

新增 12 项回归覆盖四类替代代码、两个权益标题、标题错误、候选冲突、普通营收/其他成本/总净利润拒绝替换。后续优先对新增公司做官方公告抽查，并推进保存版本的时间查询；金融收入/成本如要纳入特征须另有明确字段定义。
