# 阶段 3 首批：离线双源核验入口

基准 main `65a5528`（2D 已合入），分支 `feat/stage3-reconcile`。按流程优先节奏，先提供六字段比较与报告。没有新增依赖、数据库迁移或主来源写入；尚未接新浪采集器，不能据此宣称完成真实双源验收。

## 运行

```bash
.venv/bin/python -m fin_feature.cli reconcile \
  --left examples/reconciliation/primary.json \
  --right examples/reconciliation/secondary.json
```

人工样例包含 6 项：一致 3、差异 1、不可比 1、缺失 1，预期退出 1。JSON 报告写 stdout，可自行重定向保存。退出 0 表示所提供项目全部一致；1 表示需要检查的差异/缺失/不可比；2 表示输入文件、身份或格式错误。空输入拒绝，不报告空集合全部通过。

## 输入与判定

完整格式见上述 JSON 样例。每份文档包含 schema=reconcile-v1、source（调用来源）、upstream（实际上游）和非空 records。相同 upstream 不构成独立来源；工具只能检查提供的声明，不能验证来源真实性。

每行用 secucode + report_date + field 对齐，重复身份直接拒绝（先由调用方明确选择版本）。证券代码显式 SH/SZ/BJ，日期为合法季度末。字段限：total_operate_income、parent_netprofit、operate_cost（IS 累计流量），total_assets、total_liabilities、total_equity（BS 期末余额）。营业总收入不能映射为普通营业收入；总权益不能替换为归母权益。

必须明确 currency=CNY、scope=consolidated/parent_only、table_type、period_basis、unit=yuan/wan_yuan/yi_yuan。双方 comparison_basis_id 须相同且已知，并提供 alignment_evidence（版本对齐依据）、evidence_ref、record_ref。相同公告日期不自动证明版本对齐；未知元信息输出不可比。外币与单季流量暂不支持，不做汇率换算或口径推断。

金额采用 Decimal 转换为元。默认 abs_tol=1 元、rel_tol=0.000001，可用 --abs-tol/--rel-tol 覆盖；比较条件为 |左-右| <= max(abs_tol, rel_tol*max(|左|,|右|))。本批仅金额共用容差，接真实来源时根据字段及披露精度配置批次；尚无比例指标。负值保留，零基数可比较；null、bool、非有限数、非法数字及绝对值超过 1e30 视为缺失/无效。

报告包含双方输入、标准金额、差额、实际容差、原因、文件 SHA-256、来源和可比较比例。分母仅为两份文件所提供身份的并集；双方都漏掉的项目无法发现，这不是计划样本的整体覆盖率。evidence_verified=false 明确说明工具保留证据引用但未打开/核实公告或原始文件。报告不改变主来源选值，也不向特征链补入第二来源数据。

## 本批验证与下一步

新增 19 项测试覆盖单位、口径、零/负值、缺失、重复身份、同上游拒绝、CLI/退出码与文件不变。人工样例不代表真实数据发现。

下一批：限定公司/报告期探测新浪来源并保留原体，建立真实字段与单位映射，接入本入口；再逐步完成计划的 8 家×2 年样本、公告抽查。未知口径如实报告，全面异常矩阵及性能优化留到集中验收。
