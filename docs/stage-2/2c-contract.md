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

## 2026-10-17：实现与验证映射

实现 `3b3c5d8` + `9f9a26d`，待固定 head MR 审阅。新增 tests/test_versioned_pipeline.py **45 项**（含参数化），旧 552 项保持；全量 **597 passed、11 deselected（14.33s）**。命令与临时输入边界见 [批次记录](../progress.md)。以下后缀均对应该测试文件中的 test_ 函数。

| ID | 用例后缀/实际验证 |
|---|---|
| C01 | c01_v4_upgrade_backup_preserves_rows_without_inventing_refs / c01_v5_incomplete_structure_is_rejected / c01_v5_constraint_impostor_is_rejected |
| C02 | c02_v5_failure_rollback_preserves_outer_transaction：DDL 后真实故障，外层 stock 行仍待提交 |
| C03 | c03_versions_are_authoritative_and_no_raw_fallback |
| C04/C05 | c04_c05_standard_values_convert_once_and_carry_refs：原值 16.75、标准值 0.1675、毛利率 0.4、负债率 0.6、FieldSource.record_ref；fixture 单位污染不影响 |
| C06 | c06_idempotent_cleaning_and_config_change / c06_zero_cannot_manufacture_unknown_unit（原字段有值及缺失两种） |
| C07 | c07_drop_invalidates_comparison_period_projections_but_preserves_logs：比较期变更、连续 dropped 修订、其他公司不受影响 |
| C08 | c08_clean_failure_is_atomic_after_reconnect / c08_c13_outer_transaction_is_not_committed_by_version_work |
| C09 | c09_qoq_lineage_includes_only_required_differencing_periods / c09_yoy_and_current_lineage_exclude_unrelated_periods / c09_c10_explicit_comparable_fixture_retains_numeric_formulas |
| C10 | c10_unknown_basis_remains_blocking_and_none_has_reason / c10_nonpositive_and_industry_diagnostics / c10_calculation_failures_have_explicit_reason；正向可比 fixture 的 ROE 变化 10、变化率 1、营收 YoY/QoQ 0.2；负利润比较基数拒算 |
| C11 | c11_repeated_computation_appends_logs_and_updates_projection |
| C12 | c12_stale_or_corrupt_inputs_fail_before_any_output / c12_report_period_must_be_normalized_quarter_end |
| C13 | c13_compute_failures_are_atomic_after_reconnect / c13_legacy_projection_does_not_block_immutable_new_log / c13_other_definition_with_same_key_is_preserved |
| C14 | c14_invalid_cleaning_config_is_rejected_without_writes / c14_invalid_cli_rules_are_rejected_before_db_initialization；全部旧离线回归保留，最新版本断言跟随迁移注册表 |
| C15 | c15_cli_feature_trace_reaches_real_http_body / c15_legacy_input_stays_unverified_after_compute |

实际 commit 故障发生于连接 commit 方法，不由测试预先回滚；清洗、计算和外层事务均重连核对结果。正向可比仅为显式受控声明，未将 unknown 生产输入放行。全部运行限临时库、真实临时 gzip 与脚本化 HTTP，未联网或升级业务库。
