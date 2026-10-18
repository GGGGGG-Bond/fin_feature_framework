# 当前缺口、覆盖与升级优先级

[返回文档总索引](../README.md#文档索引)

适用于v0.3.1文档修订版；覆盖复核对象是已发布v0.3.0（固定提交83457bc）的数据，日期2026-10-18。两个版本的财务公式与固定输入不变。以下不是已排期承诺；交付版已经可以查看和复现，升级按实际用途推进。

## 先纠正旧记录

旧限制页的“50个有值位置未核对”和“没有价格收益回测”已过时。v0.3.0新采样有84项正式财报核查：81匹配2024年报、3匹配后续调整比较列；另12个位置缺值/定义不齐。价格收益入口return-backtest已实现；旧backtest仍是盈利增长代理。旧快照保留历史分母，不改写其结果。

## 最有价值的缺口：真实跨期特征覆盖

主样本768个固定位置，232有值、536为空；有值来自资产负债率、权益比例、归母净利率和非金融企业毛利率。八类跨期增长/变化特征在主样本全部为空。独立茅台正式事实样例12/12有值，只证明绑定输入的完整公式链，不等于其他公司的真实跨期口径已经核实。

536个空值原因：incomparable_basis 296、missing_comparison_period 152、unknown_unit 40、not_applicable 48。按当前规则，每条空结果记录一个主要原因，不是所有潜在问题的去重总数。行业不适用不能当成待填缺失。

| 特征 | 主样本有值 / 位置 | 空值原因 |
|---|---:|---|
| current_ratio_change | 0 / 64 | unknown_unit: 40；not_applicable: 24 |
| debt_asset_ratio | 64 / 64 | — |
| debt_asset_ratio_change | 0 / 64 | missing_comparison_period: 8；incomparable_basis: 56 |
| equity_ratio | 64 / 64 | — |
| gross_margin_feat | 40 / 64 | not_applicable: 24 |
| net_profit_margin | 64 / 64 | — |
| profit_qoq | 0 / 64 | missing_comparison_period: 8；incomparable_basis: 56 |
| profit_yoy | 0 / 64 | missing_comparison_period: 32；incomparable_basis: 32 |
| revenue_qoq | 0 / 64 | missing_comparison_period: 8；incomparable_basis: 56 |
| revenue_yoy | 0 / 64 | missing_comparison_period: 32；incomparable_basis: 32 |
| roe_change | 0 / 64 | missing_comparison_period: 32；incomparable_basis: 32 |
| roe_change_rate | 0 / 64 | missing_comparison_period: 32；incomparable_basis: 32 |

统计由公开完整交付包的results/features.json逐行复核，来源文件及完整ZIP均绑定SHA-256，见[覆盖复核JSON](feature-coverage-v0.3.0.json)。它不把单独12项样例或早期三公司数据合入分母。

## 升级顺序与完成条件

| 优先级 | 可补充项 | 完成条件 |
|---|---|---|
| P1 数据可用性 | 先补少量非金融公司的可靠比较期、计量与合并范围，逐字段核实修订基准；流动比率优先使用明确披露的流动资产/负债 | 内容绑定的依据可追溯；真实跨期结果有数值和独立期望值；未知输入仍拒算；不能仅增加爬取期数或全局放宽comparable |
| P1 时间依据 | 建立可信的首次/修订公告映射，并从现在开始留存不可变观察 | 有可核对公告日期、所对应内容版本和截止日选择反例；不追认未保存的历史字节或首次公开时间 |
| P2 工程验证 | 在GitHub配置实际运行的离线CI，覆盖最低Python版本与当前验证版本，记录可用运行器 | 远端检查通过并保留报告；当前只有GitLab模板，不能把配置文件称为已验证CI |
| P2 行业与核验 | 为金融企业另行明确适用定义，继续核实上游独立性；当前12处缺口保留 | 新定义有单独版本与正式披露依据，不能将营业收入/总收入或异义成本强行互换 |
| P2 回测研究 | 扩展行业内样本、滚动形成期、成本敏感性及执行约束 | 分母、剔除和价格缺失透明；区分价格收益、可执行组合和策略有效性证据 |
| P3 体验与发布 | 浏览器排版目测、Codespaces实测、平台矩阵、文档/发布版本引用检查 | 记录实际运行环境；不把未测试平台写成一键保证；新资产使用新版本 |
| 作者决定 | 选择公开许可证、长期维护范围 | 由项目负责人作选择；当前尚未指定许可证，不由自动化代选 |

P1描述的是下一轮最优先提升的数据质量，并不是已发现会使现有固定结果失效的代码缺陷。当前未做新的全代码独立复审，不能由832项本地测试推断“没有任何bug”。

## 当前交付要说明的边界

八家手选公司不代表全市场；网站不同不证明独立上游；保守公开日期声明不等于历史系统已知。未知跨期口径拒算，不恢复未保存历史及旧软件环境。原生Windows不支持证据目录fsync，重算走Linux/WSL；免安装结果不要求WSL。Codespaces、在线服务、生产调度和远端CI尚未验证。

HTML已做本地引用检查及4种筛选逻辑DOM桩验证，新增页面未做自动浏览器排版目测；面试前可手动检查筛选、图表和下载。PDF已单页渲染检查。当前交付资产已完成匿名下载、摘要核对、解压完整性和一键复算，见[验收记录](acceptance.md)与[交付说明](interviewer-handoff.md)。
