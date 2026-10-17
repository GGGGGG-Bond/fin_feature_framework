# 2C 清洗与特征消费契约

日期：2026-10-17；基准 main `194610b`（MR!8）。用户要求合入后继续，架构 agent 直接实施并自验证；此契约和代码同批供 MR 审阅，不将自验证称为另一位 agent 独立复审。

## 输入与单位

CLI clean 默认消费 `statement_version` 中 eastmoney 的唯一当前版本，不再读取兼容 raw 选值。缺版本不自动回退；遗留 Python API 默认保留原路径，调用方可显式选择 input_mode="versions"。CLI features 使用版本清洗结果；直接 API compute_all 的原路径仍保留作兼容。

标准值唯一来源是版本内 standardized_fields，不用运行时 fixture 单位覆盖重新转换。清洗按标准单位做阈值诊断、缺失策略和时间对齐；未知单位与非法值不可通过 zero 策略伪装成已知值。标准值与清洗配置不可变保存在 clean_statement；兼容 clean 数值列继续保留原计量，不改变旧查询单位。新增 record_ref 指向版本、clean_ref 指向清洗结果，版本特征适配器只读 clean_statement 标准值，不二次除以 100。

旧版本可计算当期合法特征，但 evidence_sufficient=0 仍为不足，不因迁移或计算升级充分性。未知 currency/scope/period_basis 仍阻断跨期；来源表名相同、文件存在、版本可追溯均不等于口径可比。本批不放开阶段一拒算规则，不新增真实口径推断。

## 迁移与当前投影

注册 v5，新增 clean_statement（按 record_ref + config_hash 幂等保存标准值、规则、诊断、保留/丢弃状态）、clean_selection（包括 dropped 的当前选择），三张 clean 表添加可空 record_ref/clean_ref。不回填历史血缘、不改旧迁移、不自动运行清洗。常规初始化对旧库仍先备份再迁移；v5 缺结构拒绝。真实库升级不在本批执行。

一次版本清洗在一个 unit_of_work 中读取当前版本并重建当前 clean 投影，结果历史保留；丢弃记录保留原因和版本引用。变化公司所有报告期的 stage1-v1 当前特征失效（保守覆盖同比/环比依赖），保留 legacy/其他定义和全部不可变计算日志。重复同内容/同配置清洗不制造结果，也不失效未变化特征。清洗/投影/日志/失效任何一步失败全回滚。

计算前核对当前版本、清洗配置和当前 clean 引用一致；未清洗、修订后未重清洗或引用错误，拒绝而非消费陈旧值。版本计算与输出采用同一个 SQLite 事务视图，避免并发修订混入。

## 计算日志与血缘

保持 stage1-v1 财务公式。每次版本计算产生 compute_run_id，每个公司/报告期/特征追加 feature_compute_log（包括 None）；input_version_ids 为该特征字段依赖及所需报告期的版本集合，config_snapshot 保存实际清洗配置、字段来源/缺失、诊断和输入 clean_ref。gross_margin/current_ratio 的行业门禁也记录行业依据。current、previous、year_ago、prev_prev 差分依赖必须纳入对应特征血缘。

features 仅为当前投影，与不可变计算日志在同一事务提交。填充 compute_run_id/input_version_ids/code_commit/config_hash/not_computable_reason；as_of_date 保持 NULL，不提供时间旅行。code_commit 由 CLI 读取仓库实际 HEAD（dirty 状态另记），不接受无证据的默认假 SHA。legacy 当前行继续拒覆盖，计算日志仍可保存被拒投影的新结果。

不可计算原因优先保留该特征明确诊断；补充依赖字段的 unknown_unit/incomparable_basis、缺比较期/缺输入、非法值；计算异常不伪装成成功数值。NULL 也有非空原因，有限数值原因为空。

## 验收矩阵

| ID | 验收 |
|---|---|
| C01 | v4 含历史库升级/备份/重跑，旧值不改且血缘不编造；v5 缺结构拒绝 |
| C02 | v5 迁移故障全部回滚，外层事务仍受调用方控制 |
| C03 | CLI 消费版本而非矛盾 raw；无版本不偷回退 |
| C04 | ROE 16.75→版本 0.1675→特征只用 0.1675；fixture 污染不影响 |
| C05 | 原计量 clean 兼容；标准清洗 sidecar 有 record_ref、规则与诊断 |
| C06 | 重复清洗幂等；规则变化新结果；阈值只诊断，不清空值 |
| C07 | drop/修订/规则改变失效所有受影响公司比较期投影，历史日志/legacy 保留 |
| C08 | 清洗中途与提交失败全回滚，重连核验 |
| C09 | 当前、同比、环比/Q1 多段差分的字段级版本血缘完整；无关期间不混入 |
| C10 | 未知口径继续拒算，NULL 原因可区分；行业不适用与非正基数诊断保留 |
| C11 | 计算日志不可变；重复计算有新 run，当前投影指向本次计算 |
| C12 | 版本/配置过期、引用错组/错版本拒绝，不写部分结果 |
| C13 | 计算日志/投影/commit 故障整体回滚，legacy 拒覆盖不影响日志 |
| C14 | 旧 552 项离线回归、CLI 帮助、配置与文档链接、Git 排除保持 |
| C15 | 从特征版本引用到真实 snapshot + row_index + gzip 原体逐项核验；不足证据不升级 |

不实现 2D replay、第二来源、as_of、真实业务采集或生产部署。验收仅临时 SQLite/gzip 与脚本化 HTTP；精确测试名与结果交付时补充。
