# 官方公告抽查与阶段 3 汇总

本批基准 main `2018df7`。金融映射已合入，本分支继续整合先前的官方参考检查、部分字段/年报单位检查、两组新增公告核查及统一汇总，以多个提交一起交付。

## 命令

```bash
.venv/bin/python -m fin_feature.cli review-batch \
  --batch-dir /path/financial-batch \
  --reviews /path/reviews.json \
  --output-dir /path/new-review-output
```

reviews.json 示例（相对路径按本文件目录解析）：

```json
{
  "schema": "reference-review-plan-v1",
  "reviews": [
    {"secucode": "601318.SH", "reference": "pingan-2024-reviewed.json", "document": "pingan-2024.pdf"}
  ]
}
```

命令重新读取批次 plan.json，按原固定公司/年度/六字段建立分母，重新核验每份原体、哈希和映射行；不直接相信旧 summary.json 的计数。有参考项的公司使用 reference-check；未提供参考的项目不计为通过，重复/计划外公司直接拒绝。字段级 fields.json 保留每个计划身份的状态。

输出 summary.json、summary.md、fields.json 及逐公司 *.review.json。退出 0 仅表示全部计划字段通过数值参考检查；1 表示有未查、缺失或数值差异；2 表示样本/参考文件损坏、读取/计划错误等。即使数字全通过，历史可见性、来源修订身份和底层供应商独立性仍不自动通过。空 reviews 表示未抽查，不能形成空集合验收成功。

## 抽查格式与单位

详见 [reference-check](official-reference.md)。参考文件 pins 两份输入 JSON 和官方 PDF 的 SHA-256，人工检查页/行/列。coverage=exact 为默认全集；明确 selected-fields 才允许只查子集，并输出 input_pairs/unchecked_pairs。金融企业当前只检查资产、负债、总权益、归母净利润，不把缺失的收入/成本改定义。

参考项可保存 document_value 和 document_unit，支持元/万元/百万元；标准 value 始终为元，代码核验精确换算。source_units 是对固定输入的人工核实声明，不能推广到其他公司/批次。代码验证文件身份和数字，不自动识别 PDF 内容，也不为人工声明提供电子签名或来源认证。实际核查由本 agent 按 PDF 图像逐页完成，和独立审阅是不同状态。

## 2026-10-17 真实核查

| 公司 | 年报依据 | 物理 PDF 页 / 印刷页 | 年度字段对 | 结果 |
|---|---|---|---:|---|
| 茅台 | [官网 2024 年报](https://www.moutai.com.cn/mtgf/articleFileDir/2025-04/08/8055b7bed7db41bdbc617f4c9b9ec591.pdf) | 58-64 / 相同 | 12 | 12 一致 |
| 中国平安 | [官网 2024 年报](https://www.pingan.com/app_upload/images/info/upload/eb52da50-5b94-4fb7-a7d5-a36500e8ed9d.pdf) | 155-159 / 151-155 | 8 | 8 一致 |
| 万科 | [巨潮披露 2024 年报](https://static.cninfo.com.cn/finalpage/2025-04-01/1222974139.PDF) | 151-155 / 148-152 | 12 | 12 一致 |

两年度均为对应 2024 年报本期列及 2023 比较列，不声称独立验证了 2023 原始版本。平安报表单位为人民币百万元，按明确声明转换；万科负利润原值保留，不与总净利润混用。三家公司 32 项使用 0.01 元绝对容差、无相对容差。新参考文件在 examples/references/；真实接口 JSON、官方 PDF 和报告在 data/，不入 Git。

八家公司固定分母仍为 96。双方有效数字 84；已按官方文件核查 32，32 一致；52 项有数字但未核查、12 项源字段缺失。因此未核查计划项为 64，整体退出 1，不是全阶段验收通过。本批没有新增财务接口请求，只下载官方披露文件并重用已保存响应。

实际输出：<local-evidence-directory>。PDF 在同级 official-documents；茅台 PDF 仍引用此前核查目录。原证据引用目录须保留。新参考文件分别记录每家公司独立输入哈希，不复用茅台声明去放行其他公司。

## 收尾状态与下一步

| 工作 | 当前结论 |
|---|---|
| 双来源真实接线与固定 8 家×2 年采样 | 已跑通，有原体与明确缺失分母 |
| 关键工业/金融字段映射 | 已覆盖 84/96，12 项定义不同继续缺失 |
| 官方公告数值抽查 | 3 家、32 项已完成，本批统一汇总 |
| 剩余 52 个有数字项目的公告核查 | 待补，不按缺失或通过混写 |
| 来源修订版本对齐/真实修订案例 | 待核实，数值相等不证明同一修订版 |
| 历史截至日与修订传播 | 后续阶段，未知时间不回填 |

随后按用户流程优先节奏推进保存版本的系统观察时间查询，同时保留上述公告核查清单；不把尚未核实的公开时间用于市场 as_of。性能、结构优化及全面异常矩阵后置。

公开[本次计数快照](review-result.json)随文档提交，不含接口原始响应；未来读取该文件不构成重新验证。
