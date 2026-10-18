# 12项必做特征的完整样例

这是2026-10-18执行的离线正式事实样例结果。茅台2024年末12/12有值；完整4个期间共48条结果、19条有值、48/48重放一致，证据对账通过。其他期间缺少比较期等空值仍保留。

| 特征 | 结果 |
|---|---|
| 流动比率变化 (`current_ratio_change`) | -1.7028 倍数差 |
| 资产负债率 (`debt_asset_ratio`) | 19.04% |
| 资产负债率变化 (`debt_asset_ratio_change`) | 5.4163 百分点 |
| 权益比例 (`equity_ratio`) | 80.96% |
| 销售毛利率 (`gross_margin_feat`) | 91.93% |
| 归母净利率 (`net_profit_margin`) | 49.52% |
| 单季归母净利润环比 (`profit_qoq`) | 32.77% |
| 归母净利润同比 (`profit_yoy`) | 15.38% |
| 单季营业总收入环比 (`revenue_qoq`) | 28.61% |
| 营业总收入同比 (`revenue_yoy`) | 15.66% |
| ROE变化 (`roe_change`) | 1.8300 百分点 |
| ROE变化率 (`roe_change_rate`) | 5.35% |

[目标期CSV](interview-features.csv) · [运行摘要](interview-summary.json) · [27项固定事实及官方链接](../examples/interview_financials.json) · [公式、期间与限制](../docs/interview.md)

输入由3份正式财报事实重建；不是供应商原始响应或本次网络采集。币种、单位、范围及跨期口径声明仅绑定本组具体输入，不放宽默认真实来源的未知门禁。毛利率采用营业收入/营业成本；营收增速与净利率使用营业总收入。

克隆后运行 `bash run.sh` 会生成新结果和可直接打开的HTML总览；Windows需WSL和Python>=3.12。投资收益回测仍为未完成附加项。
