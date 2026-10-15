# 阶段 2 实施计划 — 输入留存、版本模型与离线重放

## Context

阶段 1（正确性与财务定义）已完成并合入 main（commit `20093a8`，156 测试通过）。阶段 1 建立了 `definition_version` 标记、语义合并、跨期可比性门禁和单季转换，但**仅做了最小定义隔离**，明确不建设完整版本存储、原始响应留存或血缘追溯（见 stage-1/README.md 第 17 行）。

阶段 2 解决阶段 1 留下的核心缺口：当前无法从特征值追溯到原始输入，原始 API 响应在解析后即丢弃，同一日期的内容修订会覆盖旧证据，失败批次可能伪装成成功，旧库迁移无版本管理。

**当前代码库现状**（经三个探索代理 + 逐文件精读确认）：

| 概念 | 现状 | 证据 |
|---|---|---|
| 采集批次 (ingest_run) | 完全不存在 | crawl_watermark 仅记录 last_notice_date；CLI 命令间独立无 run_id |
| 原始响应留存 (raw_snapshot) | 完全不存在 | fetch_all 返回提取后的 list[dict]，原始 JSON 在 _request_with_retry 中丢弃 |
| 内容哈希 | 完全不存在 | 全代码库无任何哈希计算 |
| 版本模型 (statement_version) | 完全不存在 | raw_* 靠 (secucode, report_date, notice_date) 隐式保留多版本；clean_* 是 INSERT OR REPLACE 最新覆盖 |
| 血缘追溯 | 预留但未填充 | SemanticRecord.record_ref (standardize.py:177)、FieldSource.record_ref (semantic.py:74) 均为 None |
| 迁移机制 | 仅一个 ad-hoc 加列 | _migrate_features_definition_version 检查列存在性，无 schema_version 表 |

**用户决策**：先交付 docs/stage-2/ 正式契约文档集（与阶段 1 流程一致），首批仅交付 2A（数据模型与迁移框架）供独立审阅，通过后再继续 2B-2D。

基准：`20093a8`（main 分支）。契约依据：docs/implementation-plan.md 第 63-93 行（存储与追溯、历史时点与修订增量）。

---

## 阶段 2 范围与验收标准

**任务**（implementation-plan.md 第 111-113 行）：新增采集批次、原始响应留存、版本与血缘；实现安全迁移；让清洗、特征与核验消费标准化记录，降低对具体来源字段的依赖。

**验收标准**（5 条）：
1. 任意示例特征能追至准确原始输入
2. 相同数据及定义可离线重算得到相同业务值
3. 同一日期的内容修订不会覆盖证据
4. 失败批次不会伪装成成功
5. 旧数据迁移可验证且原库保留

**阶段边界**（不实现）：
- as_of 时间旅行查询（阶段 4）
- 第二来源核验（阶段 3）
- 完整修订传播重算（阶段 4）
- 不删除旧 raw_*/clean_*/features 表，不强制旧数据迁移到新模型

---

## 子批次划分

| 批次 | 范围 | 交付物 | 审阅 |
|---|---|---|---|
| **2A（首批）** | 契约文档 + 数据模型与迁移框架 | docs/stage-2/ 契约集；schema.py 新表 DDL；migrations.py 迁移框架；repository.py CRUD；旧数据迁移 + 验证 | 独立审阅，通过后再继续 |
| 2B | 采集层改造 | raw_store.py 原始响应留存；fetch_all_with_raw；CLI 集成 ingest_run + statement_version 创建 | 待 2A 通过 |
| 2C | 清洗与特征层改造 | pipeline.py 从 statement_version 读取；semantic.py 填充 record_ref；engine.py 填充血缘列 | 待 2B 通过 |
| 2D | 离线重放 | replay.py；CLI replay 子命令；追溯查询 | 待 2C 通过 |

---

## 首批交付（2A）详细内容

### 第一部分：契约文档集（docs/stage-2/）

仿照 stage-1/ 结构，创建四份契约文档：

1. **docs/stage-2/README.md** — 阶段 2 实现契约总纲
   - 契约版本 stage2-v1，源码审阅基准 `20093a8`
   - 开工前阅读顺序、交付要求、完成条件
   - 阶段边界声明（不实现 as_of/双源/完整修订传播）

2. **docs/stage-2/data-model.md** — 数据模型与 DDL 契约
   - 5 张新表的完整 DDL（见下方设计）
   - 字段定义、主键、外键、索引
   - 版本控制机制（content_hash + observation_seq）
   - 四种时间语义（report_date / notice_date / revision_notice_date / observed_at）

3. **docs/stage-2/migration.md** — 迁移规范契约
   - schema_version 表与迁移框架设计
   - migrate_v1_baseline（兼容旧库）
   - migrate_v2_stage2（新表 + 旧数据迁移）
   - 旧 raw_* 迁移约束（evidence_sufficient=0，不编造原始响应）
   - 迁移可验证性要求

4. **docs/stage-2/test-matrix.md** — 验收测试矩阵
   - 10 个测试案例（T2-01 至 T2-10）覆盖 5 条验收标准
   - 交付证据要求

### 第二部分：数据模型 DDL

**设计决策 D1**：feature_result 扩展现有 features 表（加列），不新建表。理由：features 表主键 (secucode, report_date, feature_name) 结构合理，阶段 1 已有 definition_version 列，156 个测试依赖此表。新建表会导致双写、查询分裂、backtest 模块需大改。

**设计决策 D2**：statement_version 的标准化字段用 JSON 列存储。理由：三张表字段集合不同（FI/IS/BS 字段各异），展开列会导致大量 NULL。JSON 列允许异构表统一存储，SQLite 内置 JSON1 扩展支持 json_extract 查询。标准化字段是写入后不可变的快照。

**设计决策 D3**：版本区分用内容哈希 + 观察序号。同一 (secucode, report_date, table_type) 下，content_hash 不同即为新版本。observation_seq 自增便于排序。旧版本标记 is_superseded=1 但不删除。

#### 新增 5 张表（写入 fin_feature/storage/schema.py）

```sql
-- 1. 迁移版本管理
CREATE TABLE IF NOT EXISTS schema_version (
    version         INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    applied_at      TEXT DEFAULT (datetime('now', 'localtime'))
);

-- 2. 采集批次
CREATE TABLE IF NOT EXISTS ingest_run (
    run_id          TEXT PRIMARY KEY,          -- UUID
    source_name     TEXT NOT NULL,             -- eastmoney
    request_scope   TEXT,                      -- JSON: {report_names, secucodes, recent_periods}
    started_at      TEXT DEFAULT (datetime('now', 'localtime')),
    ended_at        TEXT,
    status          TEXT NOT NULL DEFAULT 'running',  -- running/success/partial_failure/failed
    failed_units    INTEGER DEFAULT 0,
    crawler_version TEXT,                      -- 采集器版本标识
    detail          TEXT                       -- 失败原因摘要
);

-- 3. 原始响应留存
CREATE TABLE IF NOT EXISTS raw_snapshot (
    snapshot_id     TEXT PRIMARY KEY,          -- UUID
    run_id          TEXT NOT NULL,             -- 关联采集批次
    source_name     TEXT NOT NULL,
    report_name     TEXT NOT NULL,             -- RPT_LICO_FN_CPD 等
    secucode        TEXT,                      -- 可空（股票列表请求无公司）
    page_number     INTEGER,
    request_params  TEXT,                      -- JSON（去除凭据）
    fetched_at      TEXT DEFAULT (datetime('now', 'localtime')),
    content_hash    TEXT NOT NULL,             -- SHA-256
    file_path       TEXT NOT NULL,             -- 不可变内容文件路径
    record_count    INTEGER,
    FOREIGN KEY (run_id) REFERENCES ingest_run(run_id)
);

-- 4. 标准化版本与血缘（核心表）
CREATE TABLE IF NOT EXISTS statement_version (
    version_id              TEXT PRIMARY KEY,          -- UUID
    snapshot_id             TEXT,                       -- 关联原始响应（旧数据迁移为 NULL）
    run_id                  TEXT,                       -- 关联采集批次（旧数据迁移为 NULL）
    secucode                TEXT NOT NULL,
    report_date             TEXT NOT NULL,
    table_type              TEXT NOT NULL,             -- financial_indicator/income_statement/balance_sheet
    notice_date             TEXT,                      -- 公告时间
    revision_notice_date    TEXT,                      -- 修订公告时间（未知为 NULL）
    revision_type           TEXT DEFAULT 'unknown',    -- initial/revision/error_correction/unknown（P1-1 修复）
    observed_at             TEXT DEFAULT (datetime('now', 'localtime')),  -- 系统观察时间
    content_hash            TEXT NOT NULL,             -- SHA-256（standardized_fields 内容，sort_keys+ensure_ascii=False，见 P2-2/R8）
    observation_seq         INTEGER NOT NULL,          -- 同一(secucode,report_date,table_type)内自增
    consolidate_scope       TEXT DEFAULT 'unknown',    -- consolidated/parent_only/unknown
    period_basis            TEXT DEFAULT 'unknown',    -- flow_ytd/flow_single_quarter/stock_end/unknown
    currency                TEXT DEFAULT 'unknown',
    amount_unit             TEXT DEFAULT 'unknown',
    industry_type           TEXT DEFAULT 'unknown',    -- industrial/financial/unknown
    standardized_fields     TEXT,                      -- JSON: 标准化字段快照
    raw_fields              TEXT,                      -- JSON: 原始字段快照（映射后）
    evidence_sufficient     INTEGER DEFAULT 1,         -- 1=有原始响应；0=旧数据迁移（无原始响应）
    is_superseded           INTEGER DEFAULT 0,         -- 0=当前有效；1=已被新版本替代
    superseded_by           TEXT,                      -- 指向替代版本 version_id
    FOREIGN KEY (snapshot_id) REFERENCES raw_snapshot(snapshot_id),
    FOREIGN KEY (run_id) REFERENCES ingest_run(run_id)
);

-- 5. 双源核验结果（阶段 3 用，阶段 2 仅预留表结构）
CREATE TABLE IF NOT EXISTS reconciliation_result (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    left_version_id     TEXT NOT NULL,
    right_version_id    TEXT NOT NULL,
    field_name          TEXT NOT NULL,
    comparable_status   TEXT,                      -- comparable/incomparable/unknown
    left_value          REAL,
    right_value         REAL,
    converted_value     REAL,
    tolerance           REAL,
    difference          REAL,
    conclusion          TEXT,                      -- match/conflict/unknown
    evidence            TEXT,                      -- JSON: 证据引用
    reconciled_at       TEXT DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (left_version_id) REFERENCES statement_version(version_id),
    FOREIGN KEY (right_version_id) REFERENCES statement_version(version_id)
);
```

#### features 表加列（6 列，幂等 ALTER TABLE）

```sql
ALTER TABLE features ADD COLUMN compute_run_id TEXT;
ALTER TABLE features ADD COLUMN input_version_ids TEXT;   -- JSON 数组
ALTER TABLE features ADD COLUMN code_commit TEXT;
ALTER TABLE features ADD COLUMN config_hash TEXT;
ALTER TABLE features ADD COLUMN as_of_date TEXT;
ALTER TABLE features ADD COLUMN not_computable_reason TEXT;
```

#### 索引

```sql
CREATE INDEX IF NOT EXISTS idx_sv_secucode ON statement_version(secucode);
CREATE INDEX IF NOT EXISTS idx_sv_report_date ON statement_version(report_date);
CREATE INDEX IF NOT EXISTS idx_sv_table_type ON statement_version(table_type);
CREATE INDEX IF NOT EXISTS idx_sv_current ON statement_version(secucode, report_date, table_type, is_superseded);
CREATE INDEX IF NOT EXISTS idx_raw_snap_run ON raw_snapshot(run_id);
CREATE INDEX IF NOT EXISTS idx_ingest_run_status ON ingest_run(status);
```

### 第三部分：迁移框架

**新建 fin_feature/storage/migrations.py**，替代当前 ad-hoc 加列方式：

- `MIGRATIONS: list[tuple[int, str, Callable]]` — 注册表，每项 (version, name, function)
- `run_migrations(conn)` — 读取 schema_version 当前版本，依次执行未应用的迁移
- 每个迁移在独立事务中执行，写入 schema_version 记录；失败回滚不更新版本号

**migrate_v1_baseline(conn)** — 兼容旧库：
- 执行 ALL_DDL（幂等 CREATE TABLE IF NOT EXISTS）
- 确保 features.definition_version 列存在（替代 _migrate_features_definition_version）
- version=1

**migrate_v2_stage2(conn)** — 阶段 2 核心迁移：
- 创建 5 张新表 + 索引
- features 加 6 列（幂等检查列存在性）
- 旧 raw_* 数据迁移到 statement_version（见下方约束）
- version=2

**旧 raw_* 迁移约束**（implementation-plan.md 第 77 行）：
- `evidence_sufficient=0`（标记无原始响应，不编造）
- `snapshot_id=NULL`、`run_id=NULL`（不编造采集批次）
- `observed_at` 用 crawled_at（若为 NULL 则用迁移时间）
- `content_hash` 用标准化字段内容计算
- `observation_seq` 按 (secucode, report_date, table_type) 分组，按 `notice_date, crawled_at, rowid` 排序自增（P1-2 修复：notice_date 为 NULL 时回退到 crawled_at 再到 rowid，确保确定性可复现）
- `is_superseded`：同组内仅最新（排序最大）为 0，其余为 1
- `revision_type`：旧数据迁移统一标 `unknown`（不编造修订类型）
- **迁移是复制不是移动**：旧 raw_* 表数据保留不删除
- 旧 clean_*/features 数据保留不动，不强制迁移

**迁移可验证性**：
- `verify_migration(conn) -> dict` 返回 raw_count、sv_count、sv_evidence_insufficient、sv_superseded、schema_version 等指标
- 测试断言：sv_count >= raw_count（三表合计）、迁移数据全部 evidence_sufficient=0、旧表记录数不变

### 第四部分：repository.py CRUD

新增方法（fin_feature/storage/repository.py）：

- `create_ingest_run(source_name, request_scope, crawler_version) -> str` — 创建批次返回 run_id
- `finish_ingest_run(run_id, status, failed_units, detail)` — 更新批次状态
- `save_raw_snapshot(run_id, source_name, report_name, secucode, page_number, request_params, content_hash, file_path, record_count) -> str` — 创建原始响应记录
- `create_statement_version(secucode, report_date, table_type, ...)` — 创建版本记录，处理内容哈希比较和旧版本替代
- `get_latest_statement_version(secucode, report_date, table_type) -> Row | None` — 获取最新非替代版本
- `get_statement_versions(secucode, report_date, table_type) -> list[Row]` — 获取全部版本历史
- `supersede_old_versions(secucode, report_date, table_type, new_version_id)` — 标记旧版本为替代
- `get_latest_versions_for_report(secucode, report_date) -> dict[str, Row]` — 获取同一 report_date 三表（FI/IS/BS）各自的最新非替代版本（O-1 修复：三表关联接口，供 2C merge 使用）
- `get_schema_version() -> int` — 获取当前迁移版本

### content_hash 计算规范（P2-2 / R8 修复）

`statement_version.content_hash` 和 `raw_snapshot.content_hash` 的计算必须跨环境可复现：
- 序列化方式：`json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(',', ':'))`
- `content_hash` 覆盖范围：仅 `standardized_fields` JSON 内容（不含元数据字段，元数据变化不视为内容修订）
- `raw_snapshot.content_hash` 覆盖范围：原始响应 JSON 的规范化序列化
- 哈希算法：SHA-256

### config_hash 计算规范（O-2 修复）

`features.config_hash` 覆盖范围：当前生效的 YAML 配置全文（config.yaml）的 SHA-256，2C 实现时填充。离线重放时验证配置一致性。

### 水位推进与批次状态关联（O-4 接口约定）

2B 将修改 `update_watermark` 使其仅在 `ingest_run.status IN ('success', 'partial_failure')` 时推进水位。`status='failed'` 时不推进水位（implementation-plan.md 第 91 行）。此为 2A 到 2B 的接口约定，2A 在契约文档中声明。

---

## 后续批次概要（2B-2D，待 2A 审阅通过后展开）

### 2B 采集层改造
- 新建 `fin_feature/crawler/raw_store.py`：RawStore 类将每页响应保存为 gzip JSON 文件，路径 `raw/eastmoney/{run_id}/{report_name}_p{page}.json.gz`，计算 SHA-256（P2-6：路径规范作为契约约束写入 data-model.md）
- eastmoney.py 新增 `fetch_all_with_raw` 返回 (记录列表, 原始页响应列表)，旧方法保持不变
- CLI cmd_crawl/cmd_incremental 创建 ingest_run，采集后创建 raw_snapshot 和 statement_version
- ingest_run 状态跟踪（success/partial_failure/failed）
- 水位推进与批次状态关联（O-4：仅 status IN success/partial_failure 时推进水位）

### 2C 清洗与特征层改造
- pipeline.py run_cleaning 增加 use_statement_version 参数，优先从 statement_version 读取最新非替代版本
- semantic.py merge_three_tables_semantic 填充 FieldSource.record_ref 为 version_id
- engine.py compute_all 填充 features 血缘列（compute_run_id、input_version_ids、code_commit、config_hash、as_of_date）

### 2D 离线重放
- 新建 `fin_feature/addons/replay.py`：给定 secucode + report_date + feature_name，从 statement_version 读取输入重建 FeatureContext 重算
- CLI 新增 replay 子命令
- 业务值等价判定：只比较数值（浮点容差 1e-9），忽略 compute_run_id/computed_at/批次 ID

---

## 测试计划（首批 2A）

### 新增测试文件

**tests/test_migrations.py** — 迁移框架与旧数据迁移：
- T2-09：旧库迁移完整性（raw_* 数据正确迁移到 statement_version，evidence_sufficient=0，旧表不删除）
- T2-10：迁移幂等性（重复运行迁移不产生重复数据，schema_version 不重复）
- 迁移框架：空库迁移、已有 schema_version 的库跳过已应用迁移、迁移失败回滚

**tests/test_statement_version.py** — 版本模型 CRUD：
- 版本创建与 content_hash 比较
- 同一日期内容变化 → 新版本，旧版本 is_superseded=1（T2-05，验收标准 3）
- 同内容幂等（content_hash 相同不创建新版本）（T2-06，验收标准 3）
- get_latest_statement_version 返回最新非替代版本
- observation_seq 自增正确

**tests/test_ingest_run.py** — 采集批次 CRUD：
- 创建/更新批次状态
- 全部失败 status='failed'（T2-07，验收标准 4）
- 部分失败 status='partial_failure'（T2-08，验收标准 4）

### 验收标准与测试映射

| 验收标准 | 测试 | 批次 |
|---|---|---|
| 1. 特征追溯至原始输入 | T2-01/T2-02 | 2C+2D |
| 2. 离线重算等价 | T2-03/T2-04 | 2D |
| 3. 修订不覆盖证据 | T2-05/T2-06 | 2A |
| 4. 失败不伪装成功 | T2-07/T2-08 | 2A |
| 5. 旧数据迁移可验证 | T2-09/T2-10 | 2A |

首批 2A 覆盖验收标准 3/4/5；验收标准 1/2 在 2C/2D 完成后覆盖。

### 验证命令

```bash
# 离线测试（不含网络）
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/ -v -p no:cacheprovider -m 'not requires_network'

# 端到端（含网络）
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/ -v -p no:cacheprovider

# 安装验证
.venv/bin/pip install -e . && python -m fin_feature.cli --help

# 迁移验证（旧库迁移后检查）
.venv/bin/python -c "from fin_feature.storage.migrations import verify_migration; ..."
```

---

## 关键设计决策

| 编号 | 决策 | 理由 |
|---|---|---|
| D1 | feature_result 扩展 features 表加列，不新建表 | 保持主键结构，156 测试不受影响，避免双写和查询分裂 |
| D2 | statement_version 标准化字段用 JSON 列 | 三表字段异构，展开列大量 NULL；JSON 不可变快照天然适合 |
| D3 | 版本区分用 content_hash + observation_seq | 哈希区分内容修订，序号便于排序；旧版本标记不删除 |
| D4 | 迁移是复制不是移动 | 旧 raw_*/clean_*/features 保留，不破坏阶段 1 已验证状态 |
| D5 | 旧数据迁移标记 evidence_sufficient=0 | 不编造原始响应，明确标注证据不足 |
| D6 | fetch_all_with_raw 新方法，旧方法不变 | 向后兼容，阶段 1 测试和调用方不受影响 |

---

## 风险与缓解

| 风险 | 缓解 | 对应测试 |
|---|---|---|
| R1: JSON 字段顺序影响 content_hash | 序列化时 sort_keys=True + ensure_ascii=False + separators 紧凑模式（见 content_hash 规范） | T2-05/T2-06 |
| R2: 旧库 crawled_at 为 NULL | observed_at 回退到迁移时间 | T2-09 |
| R3: features 加列后旧查询行为变化 | 新列默认 NULL，不影响现有查询；definition_version 过滤逻辑不变；as_of_date 为阶段 4 预留，2A-2D 不填充（P2-3） | 全量回归 |
| R4: 迁移中途失败导致不一致 | 每个迁移在独立事务中执行，失败回滚不更新版本号 | 迁移框架测试 |
| R5: 旧 raw_* 同一 (secucode, report_date, notice_date) 有重复或 notice_date 为 NULL | 迁移时按主键去重，observation_seq 按 `notice_date, crawled_at, rowid` 排序（P1-2 修复） | T2-09 |
| R6: schema_version 表不存在（首次迁移） | run_migrations 先检查 schema_version 表是否存在，不存在则创建 | 迁移框架测试 |
| R7: 外键约束与迁移顺序交互 | 建表顺序必须为 schema_version → ingest_run → raw_snapshot → statement_version → reconciliation_result（外键依赖链） | 迁移框架测试 |
| R8: content_hash 跨 Python 版本确定性 | 契约规定序列化规范 `json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(',', ':'))`，确保跨环境可复现 | T2-05/T2-06 |

---

## 修改文件清单（首批 2A）

| 文件 | 改动 |
|---|---|
| `docs/stage-2/README.md` | 新建：阶段 2 实现契约总纲 |
| `docs/stage-2/data-model.md` | 新建：数据模型与 DDL 契约 |
| `docs/stage-2/migration.md` | 新建：迁移规范契约 |
| `docs/stage-2/test-matrix.md` | 新建：验收测试矩阵 |
| `fin_feature/storage/schema.py` | 新增 5 张表 DDL + 索引；features 加列 DDL |
| `fin_feature/storage/migrations.py` | 新建：迁移框架 + migrate_v1_baseline + migrate_v2_stage2 + verify_migration |
| `fin_feature/storage/database.py` | init_schema 改为调用 run_migrations；保留旧 _migrate 兼容或移除 |
| `fin_feature/storage/repository.py` | 新增 ingest_run/raw_snapshot/statement_version CRUD 方法 |
| `tests/test_migrations.py` | 新建：T2-09/T2-10 + 迁移框架测试 |
| `tests/test_statement_version.py` | 新建：T2-05/T2-06 + 版本模型 CRUD 测试 |
| `tests/test_ingest_run.py` | 新建：T2-07/T2-08 + 批次 CRUD 测试 |
| `docs/progress.md` | 追加阶段 2 首批记录 |
| `docs/decisions.md` | 追加 DEC-005 阶段 2 契约决策 |

---

## 实现顺序（首批 2A）

1. **契约文档**：创建 docs/stage-2/ 四份文档，冻结数据模型和迁移规范
2. **schema.py**：新增 5 张表 DDL + 索引 + features 加列
3. **migrations.py**：迁移框架 + v1_baseline + v2_stage2 + verify_migration
4. **database.py**：init_schema 改用 run_migrations
5. **v1_baseline 全量回归**（P2-4）：migrate_v1_baseline 对空库和已有库均能正确执行后，先运行全量 156 测试验证不破坏阶段 1，再进行 v2_stage2 开发
6. **repository.py**：新增 CRUD 方法
7. **测试**：test_migrations + test_statement_version + test_ingest_run
8. **验证**：全量测试 + 迁移验证 + 安装验证
9. **文档同步**：progress.md + decisions.md

---

## 限制与边界

- 首批 2A 仅实现数据模型和迁移框架，不改动采集/清洗/特征的业务逻辑
- 旧数据迁移标记 evidence_sufficient=0，不编造原始响应或采集批次
- 旧 raw_*/clean_*/features 表保留不删除，阶段 1 已验证状态不受影响
- statement_version 表创建后暂无数据写入（2B 采集层改造后才有新数据），但旧数据迁移会填充
- features 加列后旧查询行为不变（新列默认 NULL）
- 后续 2B-2D 待 2A 独立审阅通过后展开
