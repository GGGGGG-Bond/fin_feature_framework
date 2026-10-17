# 迁移规范契约

契约版本：stage2-v1。源码：`fin_feature/storage/migrations.py`。

## 迁移框架

### schema_version 表

跟踪已应用的迁移版本。`run_migrations(conn)` 读取当前版本，依次执行未应用的迁移。

### 迁移注册表

```python
MIGRATIONS: list[tuple[int, str, Callable]] = [
    (1, "v1_baseline", migrate_v1_baseline),
    (2, "v2_stage2", migrate_v2_stage2),
    (3, "v3_rereview_fixes", migrate_v3_rereview_fixes),
]
```

### _apply_migration（R7 修复）

每个迁移在 savepoint 中执行，失败回滚不残留 DDL 或数据。R7 修复：检查 `conn.in_transaction`，仅无外层事务时才提交/回滚，不提交外层已有事务。

```python
def _apply_migration(conn, version, name, fn):
    in_transaction = conn.in_transaction
    conn.execute("SAVEPOINT migration_savepoint")
    try:
        fn(conn)
        conn.execute("INSERT INTO schema_version (version, name) VALUES (?, ?)", (version, name))
        conn.execute("RELEASE SAVEPOINT migration_savepoint")
        if not in_transaction:
            conn.commit()
    except Exception as e:
        conn.execute("ROLLBACK TO SAVEPOINT migration_savepoint")
        if not in_transaction:
            conn.rollback()
        raise RuntimeError(f"迁移 v{version} ({name}) 失败：{e}") from e
```

## migrate_v1_baseline（version=1）

兼容旧库：
- 执行 ALL_DDL（幂等 CREATE TABLE IF NOT EXISTS）
- 确保 features.definition_version 列存在
- 创建基础索引

## migrate_v2_stage2（version=2）

阶段 2 核心迁移：
- 创建 6 张新表（ingest_run, raw_snapshot, statement_version, statement_observation, reconciliation_result, feature_compute_log）
- features 加 6 列（幂等检查列存在性）
- 旧 raw_* 数据迁移到 statement_version
- 创建阶段 2 索引

## migrate_v3_rereview_fixes（version=3 — A2 修复）

旧 v2 草稿库（d29637d/4f99ee8/5390dac/181c4b6）记录 `schema_version=2`，但结构不完整：缺 `statement_observation` 表、缺观察元信息列、缺新哈希 CHECK 约束、缺阶段 2 索引。`run_migrations` 只看版本号，会静默返回成功，首次观察写入才报 `no such table`。v3 幂等补齐：

- **旧 `statement_version` 缺新哈希约束**：为空则重建（SQLite 无法 ALTER 加 CHECK）；**含数据则拒绝**（`RuntimeError`，提示保留原库、从受支持的阶段一副本迁移），不静默接受、不删原库数据。
- 幂等补齐全部阶段 2 表（旧草稿库缺 `statement_observation` 等）。
- 补 `statement_observation` 的 A3 观察元信息列（`notice_date`/`revision_notice_date`/`revision_type`）。
- 创建阶段 2 索引（`IF NOT EXISTS`）。

## 旧 raw_* 迁移约束

- `evidence_sufficient=0`（无原始响应，不编造）
- `snapshot_id=NULL`、`run_id=NULL`（不编造采集批次）
- `observed_at` 用 crawled_at（若为 NULL 则用迁移时间）——这是**系统观察时间**，不是公告或修订时间
- `currency=unknown`（R4-a 修复：不推断 CNY，哈希与 INSERT 使用同一值）
- `metric_hash`/`semantic_hash` 用 `hashing.py` 统一计算
- **A6 修复**：标准化字段经 `normalize_numeric` 统一数值表示后再计算哈希与存储，与运行时 `create_statement_version` 共用同一处理
- `revision_type`：统一标 unknown
- **迁移是复制不是移动**：旧 raw_* 表数据保留不删除

## 当前投影选值（B1 修复）

旧投影用 `ORDER BY COALESCE(notice_date, crawled_at, 迁移时间), crawled_at, rowid`，既没有读取 `entry_time`，又让未知公告日的记录凭采集时间获得公告优先级，与阶段一已验收的选值规则分歧，会静默改变当前有效版本。

修复后：

- `SELECT *` 读取完整旧来源元信息（含 `entry_time`）并消费；
- 分组键与阶段一同口径：`report_date` 经 `normalize_date` 规范化后分组（无法解析时保留原值，不丢弃历史记录）；
- 当前投影**复用阶段一同一函数** `fin_feature.cleaning.dedup.sort_key`（`notice_date` → `entry_time` → 内容摘要），不维护第二套排序；
- `observation_seq` 是组内按该规则的**升序位置**，序号最大者为当前投影；它是确定性的排序位置，**不代表已证实的公告或修订先后**；
- `is_superseded`：同组内仅被选中的最后一条为 0，其余为 1；
- `notice_date` 存规范化后的原始公告日，未知保持 NULL，**不用采集时间补齐，不制造日期证据**（原值仍留在 raw_* 表）；
- 并列歧义（同日同 `entry_time`，或同日均无 `entry_time`）按内容摘要确定性选取，并以 `dedup_ambiguous` 动作记入 `clean_log`（detail 前缀 `迁移投影：`，与阶段一自身写下的同名日志区分），保留歧义而不声称恢复真实先后；
- SQL 的 `ORDER BY ... rowid` 只给同一 DB 上完全并列（含内容摘要并列）的行一个可复现的物理顺序，不参与选值判断。

**存量影响**：旧投影只存在于未发布的本地草稿库（2A 尚未合入 main，无已发布库）。修正后从阶段一副本重跑迁移即可；不提供就地重投影，也不修改已迁移库的既有行。

## R4-b 修复：统一标准化入口

迁移调用 `standardize_record(raw_record, fields, unit_map=FIELD_UNITS)`，复用统一标准化入口：
- 保留有限数检查（NaN/Inf → None + invalid_value）
- 保留布尔检查
- 保留未知单位处理
- 不受运行时 `_unit_overrides` 污染（使用固定 FIELD_UNITS 映射）

非有限值（NaN/Inf）在 raw_fields 中转为可逆字符串标记（`str(v)`），standardized_fields 中过滤为 None。

## 迁移可验证性

`verify_migration(conn) -> dict` 返回：
- raw_count：三表 raw_* 记录数合计
- sv_count：statement_version 记录数
- sv_evidence_insufficient：evidence_sufficient=0 的记录数
- sv_superseded：is_superseded=1 的记录数
- sv_current_count：is_superseded=0 的当前投影条数（B1）
- sv_groups_not_single_current：当前投影不唯一的组数，应为 0（B1）
- migration_ambiguous_selections：迁移投影保留的并列歧义条数（B1）
- schema_version：当前迁移版本
- raw_counts_by_table：各 raw_* 表记录数
- sv_counts_by_type：各 table_type 记录数

测试断言：迁移数据全部 evidence_sufficient=0、旧表记录数不变、哈希可从存储字段重算、当前投影与阶段一选值一致且每组唯一、全部历史记录保留、重跑幂等。

## v5：2C 清洗血缘（2026-10-17，待 MR）

注册 `v5_clean_lineage`，实现位于 storage/clean_model.py；旧 v1—v4 迁移函数保持不变。新增 clean_statement/clean_selection 与三张 clean 表的可空外键。已被接受但没有兼容清洗表的旧 v2/v3 草稿，由 v5 幂等补建缺失 clean 表；存在但缺列/约束的伪完整结构拒绝，不删旧数据。

常规 Database.init/init_schema 发现待执行迁移，仍先使用 SQLite backup API 保存旧库（包括 WAL 提交），再迁移。低层 run_migrations 只管理迁移事务，调用方须选择已验收备份入口；本批只在临时库验证，未执行真实升级。迁移不运行 clean/features、不回填 record_ref、不把旧结果标证据充分。升级后首轮 clean 显式重建当前投影；先确认使用的库与配置。

C01/C02 验证含数据 v4 副本、v5 幂等、不完整及伪约束结构拒绝、DDL 后异常回滚与外层事务所有权。已有迁移测试的“最新版本”断言改从注册表推导，原 v3 备份版本和内容断言仍保留。
