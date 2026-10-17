# 真实来源限量采样

基准 main `6a63e1d`（MRURL=https://registry.npmjs.org/@anthropic-ai/claude-code-linux-x64/-/claude-code-linux-x64-2.1.212.tgz 已合入），分支 feat/stage3-sina。本批连接新浪和东方财富，复用 RawStore 保存响应字节，映射到 reconcile-v1 并生成报告；不写业务数据库、不改变主来源选值。

## 运行

```bash
.venv/bin/python -m fin_feature.cli source-sample \
  --secucode 600519.SH \
  --report-date 2024-12-31 --report-date 2023-12-31 \
  --output-dir data/new-source-sample
```

目录必须不存在。范围固定为一家公司（SH/SZ）、一至两个不同年末日期、资产负债表及利润表。最多四次 HTTP 请求，请求间隔 1 秒、单次超时 25 秒，不自动重试或扩展范围。新浪取最新 20 个报告期的第一页，再选择指定日期；旧日期不在本页时明确失败，不声称全历史抓取。东方财富按公司和日期过滤，要求单页完整、公司/日期一致且无重复候选。

输出：raw/ 下 gzip 原始响应、每请求 snapshot.json、manifest.json、eastmoney.json、sina.json、report.json。每个收到的 HTTP 响应先存原体再检查状态或解析；HTTP/JSON/映射失败保留已收证据和失败清单，停止后续请求，不生成成功报告。网络失败可能没有响应体。文件写失败按失败处理；若连 manifest 都无法落盘，CLI 仍退出 2。文件留存与摘要由现有 RawStore 负责，未新增数据库模型或重试平台。

退出 0：比较项全部通过；1：采集完成但存在差异/缺失/不可比；2：采集、格式、范围或文件失败。manifest 的 collected 仅表示请求与映射完成，不表示财务核验通过。该独立采样目录不属于业务库 ingest_run，不能直接用业务库 audit-raw 对账；可用 snapshot 的 raw_hash 校验 gzip 解压字节。

## 映射与真实探测发现

接口依据：AKShare 官方仓库 [stock_financial_report_sina 实现](https://github.com/akfamily/akshare/blob/main/akshare/stock_fundamental/stock_finance_sina.py)。直接使用已验证的新浪 endpoint，无新 AKShare 依赖。运行时检查业务状态、核心 item_field 与中文标题配对；核心重复字段或标题变更拒绝，不按位置取值。

| 标准字段 | 新浪 item_field | 新浪中文标题 |
|---|---|---|
| total_operate_income | BIZTOTINCO | 营业总收入 |
| parent_netprofit | PARENETP | 归属于母公司所有者的净利润 |
| operate_cost | BIZCOST | 营业成本 |
| total_assets | TOTASSET | 资产总计 |
| total_liabilities | TOTLIAB | 负债合计 |
| total_equity | RIGHAGGR | 所有者权益(或股东权益)合计 |

营业收入不替代营业总收入，归母权益不替代总权益。缺少核心项输出 null。原始字段代码、record_ref、证据相对路径和来源元信息保留；新浪明确的 rCurrency 与 rType=合并期末分别映射币种与合并范围。年度利润表为全年累计流量，BS 为年末余额。

本次响应未提供已确认的金额单位，双方 unit 保持 unknown；东方财富报表代码含义尚未独立核实，币种/范围不猜测。comparison_basis_id 和 alignment_evidence 保持 unknown，不因金额接近、报告期相同或公告日期相同而宣称修订对齐。

2026-10-17 实查：600519.SH 的 2024 年报，两来源均返回 2026-04-17 的公告日期；新浪 update_time 另有独立值。完整原值保留，不把它回填为首次披露日或 as_of 生效日。新浪响应没有证券身份回显，证券关联仅来自请求参数；双方不同接口也不保证底层数据供应商独立。报告明确列出限制，evidence_verified=false，不声称已人工核实公告。

## 本批结果与后续

真实 CLI：600519.SH，2023/2024 年报，4 次 HTTP 请求成功，12 项字段均有记录，12 项不可比，退出 1 符合预期。原始文件和报告保存在开发 worktree 的 data/source-sample-20261017/，未入 Git。另有前置接口探测 4 次请求；本轮总计 8 次财务 HTTP 请求，未全市场采集。

新增 19 项离线测试覆盖映射语义、公司/日期/页完整性、未知元信息、失败留证、磁盘失败、目录保护及不写业务库。下一批优先抽查这些实际样本的公告、单位和修订口径，再扩大到原计划 8 家×2 年。当前是来源接线完成，真实数值一致性验收仍待完成。
