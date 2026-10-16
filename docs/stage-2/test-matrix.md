# 验收测试矩阵

契约版本：stage2-v1。源码：`tests/test_migrations.py`、`tests/test_statement_version.py`、`tests/test_ingest_run.py`、`tests/test_compute_log.py`、`tests/test_stage2_rereview_fixes.py`。

## 验收标准与测试映射

| 验收标准 | 测试 | 批次 |
|---|---|---|
| 1. 特征追溯至原始输入 | T2-01/T2-02 | 2C+2D |
| 2. 离线重算等价 | T2-03/T2-04 | 2D |
| 3. 修订不覆盖证据 | T2-05/T2-06 | 2A |
| 4. 失败不伪装成功 | T2-07/T2-08 | 2A |
| 5. 旧数据迁移可验证 | T2-09/T2-10 | 2A |

## 2A 测试清单

### test_migrations.py（12 项）

| 测试 | 覆盖 |
|---|---|
| test_v1_baseline_on_empty_db | 空库迁移 |
| test_v2_stage2_on_empty_db | 阶段 2 表存在 |
| test_run_migrations_twice_no_duplicate | 幂等性 |
| test_features_columns_added_once | features 加列幂等 |
| test_migration_idempotent | 重复迁移不更新版本号 |
| test_migration_failure_injects_rollback | R7 savepoint 回滚 |
| test_raw_migrated_to_statement_version | T2-09 旧数据迁移 |
| test_migration_evidence_sufficient_zero | 迁移数据 evidence_sufficient=0 |
| test_migration_observation_seq_continuous | observation_seq 连续 |
| test_migration_is_superseded_correct | is_superseded 分布 |
| test_verify_migration_returns_metrics | verify_migration 接口 |
| test_verify_migration_sv_count_equals_raw_count | 迁移计数 |

### test_statement_version.py（8 项）

| 测试 | 覆盖 |
|---|---|
| test_create_statement_version | 版本创建 |
| test_same_date_content_change_creates_new_version | T2-05 修订不覆盖 |
| test_same_content_idempotent | T2-06 同内容幂等 |
| test_a_b_a_recovery | R2 A→B→A 恢复 |
| test_get_latest_statement_version | 最新非替代版本 |
| test_observation_seq_unique_constraint | 唯一约束 |
| test_currency_change_creates_new_version | 语义变化新版本 |
| test_get_latest_versions_for_report | 三表关联接口 |

### test_ingest_run.py（7 项）

| 测试 | 覆盖 |
|---|---|
| test_create_ingest_run | 批次创建 |
| test_finish_ingest_run_success | 成功状态 |
| test_finish_ingest_run_partial_failure | T2-08 部分失败 |
| test_finish_ingest_run_all_failed | T2-07 全部失败 |
| test_running_to_success | 状态转换 |
| test_running_to_failed | 状态转换 |
| test_failed_units_counted | 失败计数 |

### test_compute_log.py（4 项）

| 测试 | 覆盖 |
|---|---|
| test_save_compute_log | 计算记录保存 |
| test_get_compute_log | 计算记录读取 |
| test_old_result_preserved_after_new_computation | 不可变记录 |
| test_compute_log_not_computable | 不可计算原因 |

### test_stage2_rereview_fixes.py（53 项 — R1-R7 + A1-A6 复审修复）

| 测试类 | 测试数 | 覆盖 |
|---|---|---|
| TestR1TransactionOwnership | 4 | R1 水位持久化、原子回滚、unit_of_work |
| TestR3ObservationAssociation | 4 | R3 观察表、重复观察关联 |
| TestR4aCurrencyConsistency | 3 | R4-a currency=unknown、哈希可重算 |
| TestR4bNonFiniteRejection | 3 | R4-b inf 拒绝、严格 JSON、统一入口 |
| TestR5EvidenceConsistency | 6 | R5 错来源/公司/表拒绝、默认不足 |
| TestR6HashVerification | 7 | R6 内部计算、不静默去重、格式校验 |
| TestR7MigrationRollback | 1 | R7 savepoint 回滚 |
| TestA1NestedTransaction | 3 | A1 嵌套 unit_of_work 不提交/回滚外层 |
| TestA2LegacyV2DraftUpgrade | 5 | A2 旧 v2 草稿库升级/拒绝 |
| TestA3ObservationMetadata | 3 | A3 观察公告/修订元信息 |
| TestA4RunConsistency | 4 | A4 快照/版本 run 一致、来源校验 |
| TestA5HashGlobConstraint | 5 | A5 哈希 GLOB 全串校验 |
| TestA6NumericNormalization | 5 | A6 int/float 同值不产生新版本 |

## 验证命令

```bash
# 离线测试（不含网络）
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/ -v -p no:cacheprovider -m 'not requires_network'

# 端到端（含网络）
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/ -v -p no:cacheprovider

# 复审修复专项
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_stage2_rereview_fixes.py -v -p no:cacheprovider
```

当前结果：257 passed（193 offline + 11 online + 53 复审修复）。
