# 对照官方年报核实固定样本

基准 main `2802f93`，分支 feat/stage3-verified-sample。上一批真实采样已合入。本批交付 reference-check CLI、可审阅参考值与实际报告，使用已保存样本，不重新采集财务接口，不改业务库。

## 实际结果

已逐页检查[茅台官网 2024 年报](https://www.moutai.com.cn/mtgf/articleFileDir/2025-04/08/8055b7bed7db41bdbc617f4c9b9ec591.pdf)，143 页。PDF SHA-256：5299f4940e2ce4e91084b73dc457d558b9d335fa76fbfee6227e4254eb7f4a30。核对合并报表的元/CNY 单位、合并范围、行名及 2024/2023 两列，2023 数据采用本报告比较列，不将它称为独立核对过的 2023 原始披露版本。

| 字段 | PDF 页码（从 1 起，与印刷页一致） |
|---|---|
| total_assets | 59（表头/单位在 58） |
| total_liabilities | 60（同上） |
| total_equity | 61（合并表续表上部，区别下部母公司表） |
| total_operate_income / operate_cost | 63（合并利润表，全年） |
| parent_netprofit | 64（合并利润表续表） |

保存样本 600519.SH、2023/2024 年报，双方共 24 个金额对照 12 个参考项，12/12 项 match_reference；最大绝对差 0.00003 元，使用绝对容差 0.01 元、无相对容差。原始样本的 unknown 单位/口径、公告日期和记录引用未修改。单位声明只适用本次人工检查且哈希固定的字段，不推广到其他公司或接口全量数据。

本次只证明保存字段与指定官方报表数字一致。不同网站不能证明底层供应商独立，数值一致不能证明接口记录属于同一修订版；provider_revision_alignment_verified=false、historical_visibility_verified=false 保留。前批发现的接口公告日期 2026-04-17 也保持原值，不用于 as_of。

## 重复运行

```bash
.venv/bin/python -m fin_feature.cli reference-check \
  --left /path/source-sample/eastmoney.json \
  --right /path/source-sample/sina.json \
  --reference examples/references/moutai-2024-reviewed.json \
  --document /path/moutai-2024.pdf
```

JSON 报告输出 stdout：0=两方全部字段与参考一致；1=至少一方缺失/数值差异；2=哈希、覆盖、元信息或文件错误。代码校验两份样本哈希、参考文件哈希、官方文件字节哈希与 PDF 头；要求参考/两方身份集合完全一致，参考页/行/列齐全，已有明确口径不得冲突。它不自动解析 PDF 或验证参考文件的人工声明真实性，document_content_check 明确说明人工检查来自参考文件。哈希校验不是公告语义自动识别。

参考 JSON 是公开事实的小规模手工摘录，包含 12 项金额、页码、行列、单位、来源文件哈希及 reviewed_by。更换任一输入文件都会拒绝，必须重新核查并生成对应参考；不能仅更新哈希就称完成审阅。测试使用明确人工数据，真实 API 响应与 PDF 留在 data/ 中不入 Git。

实际位置：已采样目录 `<local-evidence-directory>`；本批 PDF/结果 `<local-evidence-directory>`。本批另核对四份已有 gzip 的解压 SHA-256。命令本身只核查映射 JSON 和参考 PDF 的哈希，不重解析原始 gzip；原体到映射关系由采样模块与这次样本检查保证。

## 状态与下一步

完成第一家公司两年度六字段的官方报表数值核查。尚未完成计划的 8 家×2 年覆盖、修订案例与历史可见性验收。下一步按同样小流程扩公司/行业，并保留无法确认的字段；不扩展通用审批、PDF 自动解析或历史修订平台。
