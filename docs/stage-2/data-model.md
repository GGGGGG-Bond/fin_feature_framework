# 数据模型与 DDL 契约

契约版本：stage2-v1。源码：`fin_feature/storage/schema.py`。

## 新增表（6 张）

### 1. schema_version（迁移版本管理）

```sql
CREATE TABLE IF NOT EXISTS schema_version (
    version         INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    applied_at      TEXT DEFAULT (datetime('now', 'localtime'))
);
```

### 2. ingest_run（采集批次）

```sql
CREATE TABLE IF NOT EXISTS ingest_run (
    run_id          TEXT PRIMARY KEY,
    source_name     TEXT NOT NULL,
    request_scope   TEXT,
    started_at      TEXT DEFAULT (datetime('now', 'localtime')),
    ended_at        TEXT,
    status          TEXT NOT NULL DEFAULT 'running',
    failed_units    INTEGER DEFAULT 0,
    crawler_version TEXT,
    detail          TEXT
);
```

### 3. raw_snapshot（原始响应留存）

```sql
CREATE TABLE IF NOT EXISTS raw_snapshot (
    snapshot_id      TEXT PRIMARY KEY,
    run_id           TEXT NOT NULL,
    source_name      TEXT NOT NULL,
    report_name      TEXT NOT NULL,
    secucode         TEXT,
    page_number      INTEGER,
    request_params   TEXT,
    fetched_at       TEXT DEFAULT (datetime('now', 'localtime')),
    raw_content_hash TEXT NOT NULL,
    file_path        TEXT NOT NULL,
    record_count     INTEGER,
    FOREIGN KEY (run_id) REFERENCES ingest_run(run_id)
);
```

### 4. statement_version（标准化版本与血缘 — 核心表）

```sql
CREATE TABLE IF NOT EXISTS statement_version (
    version_id           TEXT PRIMARY KEY,
    snapshot_id          TEXT,
    run_id               TEXT,
    secucode             TEXT NOT NULL,
    report_date          TEXT NOT NULL,
    table_type           TEXT NOT NULL,
    source_name          TEXT NOT NULL DEFAULT 'eastmoney',
    notice_date          TEXT,
    revision_notice_date TEXT,
    revision_type        TEXT DEFAULT 'unknown',
    observed_at          TEXT DEFAULT (datetime('now', 'localtime')),
    metric_hash          TEXT NOT NULL,
    semantic_hash        TEXT NOT NULL,
    observation_seq      INTEGER NOT NULL,
    consolidate_scope    TEXT DEFAULT 'unknown',
    period_basis         TEXT DEFAULT 'unknown',
    currency             TEXT DEFAULT 'unknown',
    amount_unit          TEXT DEFAULT 'unknown',
    industry_type        TEXT DEFAULT 'unknown',
    standardized_fields  TEXT,
    raw_fields           TEXT,
    evidence_sufficient  INTEGER DEFAULT 0,
    is_superseded        INTEGER DEFAULT 0,
    superseded_by        TEXT,
    FOREIGN KEY (snapshot_id) REFERENCES raw_snapshot(snapshot_id),
    FOREIGN KEY (run_id) REFERENCES ingest_run(run_id),
    CHECK (evidence_sufficient = 0 OR snapshot_id IS NOT NULL),
    CHECK (length(metric_hash) = 64 AND metric_hash NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(semantic_hash) = 64 AND semantic_hash NOT GLOB '*[^0-9a-f]*'),
    UNIQUE (secucode, report_date, table_type, source_name, observation_seq)
);
```

**R5 修复**：`evidence_sufficient` 默认 0（非 1），CHECK 约束确保声明充分时必须有 snapshot_id。
**R6/A5 修复**：CHECK 约束确保 metric_hash/semantic_hash 为 64 字符十六进制。`NOT GLOB '*[^0-9a-f]*'` 校验全串（`GLOB '[0-9a-f]*'` 只约束首字符，`*` 匹配任意后续字符）。
**B1 修复**：旧数据迁移写入的 `observation_seq`/`is_superseded` 表示按阶段一选值规则排序的位置（序号最大者为当前投影），不代表已证实的公告/修订先后；`observed_at` 为该记录的系统观察时间。选值与歧义处理见 [迁移规范](migration.md)。

### 5. statement_observation（观察关系 — R3/A3 修复）

```sql
CREATE TABLE IF NOT EXISTS statement_observation (
    observation_id        TEXT PRIMARY KEY,
    version_id            TEXT NOT NULL,
    snapshot_id           TEXT,
    run_id                TEXT,
    observed_at           TEXT DEFAULT (datetime('now', 'localtime')),
    notice_date           TEXT,
    revision_notice_date  TEXT,
    revision_type         TEXT DEFAULT 'unknown',
    FOREIGN KEY (version_id) REFERENCES statement_version(version_id),
    FOREIGN KEY (snapshot_id) REFERENCES raw_snapshot(snapshot_id),
    FOREIGN KEY (run_id) REFERENCES ingest_run(run_id)
);
```

**R3 修复**：同一版本可被多次观察，每次观察关联一个 snapshot。同内容重复观察不制造新版本，但通过本表保存观察与 snapshot 的关联。
**A3 修复**：观察行保存本次观察的公告/修订元信息（`notice_date`/`revision_notice_date`/`revision_type`）。`statement_version` 行保留**首次**观察的元信息，不被后续观察覆盖；后续修订元信息归属于观察行，不得丢弃或用更新冒充最新披露。

### 6. feature_compute_log（不可变计算记录）

```sql
CREATE TABLE IF NOT EXISTS feature_compute_log (
    result_id           TEXT PRIMARY KEY,
    compute_run_id      TEXT NOT NULL,
    secucode            TEXT NOT NULL,
    report_date         TEXT NOT NULL,
    feature_name        TEXT NOT NULL,
    definition_version  TEXT NOT NULL,
    input_version_ids   TEXT NOT NULL,
    feature_value       REAL,
    not_computable_reason TEXT,
    code_commit         TEXT,
    config_snapshot     TEXT,
    as_of_date          TEXT,
    computed_at         TEXT DEFAULT (datetime('now', 'localtime'))
);
```

注：`compute_run_id` 无外键约束（compute_run ≠ ingest_run，R4 修复）。

### 7. reconciliation_result（双源核验，阶段 3 预留）

结构见 `schema.py`，阶段 2 仅建表不写入。

## features 表加列（6 列，幂等）

```sql
ALTER TABLE features ADD COLUMN compute_run_id TEXT;
ALTER TABLE features ADD COLUMN input_version_ids TEXT;
ALTER TABLE features ADD COLUMN code_commit TEXT;
ALTER TABLE features ADD COLUMN config_hash TEXT;
ALTER TABLE features ADD COLUMN as_of_date TEXT;
ALTER TABLE features ADD COLUMN not_computable_reason TEXT;
```

## 哈希计算规范

源码：`fin_feature/storage/hashing.py`。

- **序列化**：`json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)`
- **算法**：SHA-256
- **metric_hash**：仅标准化字段值（过滤 None）
- **semantic_hash**：字段值 + source_name + currency + consolidate_scope + period_basis + amount_unit + industry_type
- **R6 修复**：Repository 内部调用 `compute_metric_hash`/`compute_semantic_hash` 计算哈希，不接受调用方提供的哈希值，确保哈希与负载一致
- **A6 修复**：`normalize_numeric` 统一数值表示（整数值的 float 转 int，如 `100.0`→`100`），避免 Python/JSON 数值类型差异（`100` vs `100.0`）制造内容修订。存储与哈希使用同一规范化结果。

## 事务所有权（R1/A1 修复）

源码：`fin_feature/storage/repository.py`。

- 所有写入方法不自行提交（移除 `with self.conn`）
- `Repository.unit_of_work()` 上下文管理器：成功提交，异常回滚
- **A1 修复**：`unit_of_work` 遵守连接已有事务的所有权。最外层检查 `conn.in_transaction`，无外层事务时 `BEGIN` 自有事务；有外层事务（`with conn:` 或外层 `unit_of_work`）时使用独立 `SAVEPOINT`。内层成功只 `RELEASE SAVEPOINT`，失败只 `ROLLBACK TO SAVEPOINT` 自己的范围，绝不提交/回滚外层事务。
- `Repository.commit()` / `Repository.rollback()` 显式控制
- 增量采集单元（raw + 水位）在 `unit_of_work` 中原子提交

## 四种时间语义

| 字段 | 含义 |
|---|---|
| report_date | 报告期（财务数据所属期间） |
| notice_date | 公告时间（数据首次发布） |
| revision_notice_date | 修订公告时间（修订发布，未知为 NULL） |
| observed_at | 系统观察时间（本系统记录时间） |

不得混用过滤水位。水位（crawl_watermark.last_notice_date）用于诊断和修订检测，不用于报告期过滤。

## 2B 增量规范入口（待实现）

本文记录已发布 2A/v3；2B 的 v4 增量表、列和约束见 [2B 数据与迁移契约](2b-schema.md)。不得通过修改旧迁移 DDL 提前创建这些对象，也不得将规范交付表述为 v4 已运行。

## 2C / v5 增量（2026-10-17，待 MR）

上文为 2A/v3 发布时的数据模型快照；v4 已随 MR!8 合入，原始模型不就地改写。2C 代码 `9f9a26d` 新增：

| 对象/字段 | 语义与约束 |
|---|---|
| clean_statement.clean_id | 固定版本 + 配置哈希生成的 SHA-256 标识；主键非空 |
| record_ref | statement_version.version_id 外键，不据旧 clean 原值猜测回填 |
| config_hash / config_snapshot | 有效默认规则与配置覆盖、PIPELINE_VERSION；与 record_ref 联合唯一 |
| status | kept / dropped；丢弃也保留结果与诊断 |
| standardized_fields | 标准化版本输入经缺失策略处理的标准值 JSON，无二次单位转换 |
| compatibility_fields / diagnostics | 原计量当前投影、规则与数据问题 JSON |
| clean_selection | 公司/报告期/表类型主键，clean_ref 外键；完整记录包括 dropped 的当前选择 |
| clean_*.record_ref / clean_ref | 可空版本/清洗外键；旧行仍为 NULL，新 clean 数值列单位不变 |
| features 与 feature_compute_log | 使用既有 2A 血缘列；每次计算追加日志，当前投影只指向最近成功计算 |

DDL 和结构核验见 `fin_feature/storage/clean_model.py`。主键、联合唯一、FK、状态和哈希 CHECK 纳入 v5 核验；新表不加入旧版 ALL_DDL 或回改旧迁移。as_of_date 保持 NULL，不增加截至日查询。
