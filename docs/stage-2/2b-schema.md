# 2B v4 数据与迁移契约

版本：stage2b-v1。依赖 [2B 执行契约](2b-contract.md)。本文件是待实现的 v4 规范，不表示下列结构已经存在。

## 发布版本与迁移边界

基准 c9da710 的数据库最新版本为 v3。2B-1 注册单独 migrate_v4_ingestion_evidence；不重写已发布 v1/v2/v3 的 DDL、数据含义或已迁移投影。新表不要加入旧 ALL_DDL 让 v1 提前创建 v4 对象；空库仍顺序执行到 v4。

v4 对有效 v3 库采取增量建表/加列/建索引，保留全部原行和外键。原快照缺失的信息标 legacy-unverified，不能把旧文件路径/哈希自动猜成已验证 HTTP 原体，也不编造旧 unit/attempt/row_index。原始版本及历史 evidence_sufficient 值保留，但读写与对账必须同时检查 raw_format；旧充分性标记不能被当成 v4 已核验结论。

真实已有库升级前用 SQLite backup API 生成一致性副本（包含 WAL 已提交内容），唯一新文件且不覆盖；失败即停止迁移。测试只用临时库，本次文档交付不执行真实升级。v4 DDL、数据填充和版本登记必须在同一自有事务或 savepoint 内；失败后结构、数据、版本均可重试。不能在迁移函数中 executescript 触发隐式提交。版本自称 v4 却缺必需表/列/索引时明确拒绝，不静默接受不完整库。

## 规范 DDL

字段可以增加索引或诊断，但下列键、状态域及含义不可缩减；时间由应用给出 UTC ISO-8601（Z），不把旧本地时间字面值重标为 UTC。

~~~sql
CREATE TABLE ingest_unit (
    unit_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES ingest_run(run_id),
    unit_key TEXT NOT NULL CHECK(length(unit_key)=64 AND unit_key NOT GLOB '*[^0-9a-f]*'),
    source_name TEXT NOT NULL,
    unit_kind TEXT NOT NULL CHECK(unit_kind IN ('financial','stock_list')),
    report_name TEXT NOT NULL,
    secucode TEXT NOT NULL,
    request_scope TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('planned','running','success','success_empty','failed')),
    started_at TEXT,
    ended_at TEXT,
    failure_code TEXT,
    source_rows INTEGER NOT NULL DEFAULT 0 CHECK(source_rows>=0),
    selected_rows INTEGER NOT NULL DEFAULT 0 CHECK(selected_rows>=0),
    new_versions INTEGER NOT NULL DEFAULT 0 CHECK(new_versions>=0),
    new_observations INTEGER NOT NULL DEFAULT 0 CHECK(new_observations>=0),
    compat_writes INTEGER NOT NULL DEFAULT 0 CHECK(compat_writes>=0),
    detail TEXT,
    UNIQUE(run_id, unit_key)
);

CREATE TABLE ingest_attempt (
    attempt_id TEXT PRIMARY KEY,
    unit_id TEXT NOT NULL REFERENCES ingest_unit(unit_id),
    page_number INTEGER NOT NULL CHECK(page_number>=1),
    http_attempt INTEGER NOT NULL CHECK(http_attempt>=1),
    started_at TEXT NOT NULL,
    ended_at TEXT,
    outcome TEXT NOT NULL CHECK(outcome IN (
        'running','ok','http_error','business_error','decode_error','schema_error',
        'transport_error','storage_error'
    )),
    error_code TEXT,
    detail TEXT,
    UNIQUE(unit_id,page_number,http_attempt)
);

ALTER TABLE raw_snapshot ADD COLUMN attempt_id TEXT REFERENCES ingest_attempt(attempt_id);
ALTER TABLE raw_snapshot ADD COLUMN raw_format TEXT NOT NULL DEFAULT 'legacy-unverified'
    CHECK(raw_format IN ('legacy-unverified','http-body-v1'));
ALTER TABLE raw_snapshot ADD COLUMN http_status INTEGER;
ALTER TABLE raw_snapshot ADD COLUMN content_type TEXT;
ALTER TABLE raw_snapshot ADD COLUMN text_encoding TEXT;
ALTER TABLE raw_snapshot ADD COLUMN raw_bytes INTEGER CHECK(raw_bytes IS NULL OR raw_bytes>=0);
ALTER TABLE raw_snapshot ADD COLUMN validation_status TEXT NOT NULL DEFAULT 'legacy_unverified'
    CHECK(validation_status IN ('legacy_unverified','pending','valid','invalid'));

CREATE UNIQUE INDEX uq_raw_snapshot_attempt ON raw_snapshot(attempt_id)
    WHERE attempt_id IS NOT NULL;
CREATE INDEX ix_ingest_unit_run_status ON ingest_unit(run_id,status);
CREATE INDEX ix_ingest_attempt_unit ON ingest_attempt(unit_id,page_number,http_attempt);

ALTER TABLE statement_observation ADD COLUMN row_index INTEGER
    CHECK(row_index IS NULL OR row_index>=0);
ALTER TABLE statement_observation ADD COLUMN entry_time TEXT;
ALTER TABLE statement_observation ADD COLUMN normalization_diagnostics TEXT;
CREATE UNIQUE INDEX uq_observation_source_row ON statement_observation(snapshot_id,row_index)
    WHERE snapshot_id IS NOT NULL AND row_index IS NOT NULL;
~~~

迁移实现需先判断列/索引存在，支持故障回滚后的重试；上面 ALTER 清单是完整目标，不是让代码不加判断地重复运行。old row_index 全部 NULL，不去重删除历史观察来强行建索引。stock_list 的 unit.secucode=""，其 snapshot.secucode=NULL；财务两者都为实际公司。

## 引用、元信息与不可变性

- run/source、unit/run/source、attempt/unit、snapshot/run/unit/source/report/company 必须在 Repository/服务边界逐项一致；外键分别存在不等于一致。
- raw_snapshot.attempt_id 必须唯一指向实际收到响应的尝试。无响应只保留 attempt，不创建假快照。新 http-body-v1 快照要求 attempt_id、100..599 的状态码、raw_bytes、真实 fetched_at、合法 SHA-256、相对路径。
- 同一 attempt 重登记，核心字段与内容一致返回现有 snapshot_id；任一不同报冲突，不更新原证据，也不另插同一尝试。
- snapshot 的 body/hash/path/接收时间/HTTP 元信息不可变。pending 校验可一次完成为 valid/invalid，并填写 record_count（有效页为 len(data)，非法/非 JSON 可为 NULL）；不允许从 invalid 翻为 valid。
- valid 只代表该响应页语义校验通过，是否可产生业务版本还要求完整 unit。失败 HTTP/业务/解码响应均 invalid，不作版本输入。
- 有 snapshot 但 create_statement_version 未显式传 run_id 时，导出 snapshot.run_id；不能保存已知快照却未知的本次 run。显式不同则拒绝。
- 单元状态 planned→running→success/success_empty/failed，成功元组与计数随业务事务提交。已完成 unit 不就地重跑；不能重新把成功单元改为 failed 以覆盖历史。run 终态一次汇总，硬中断保留 running。
- observation.snapshot_id+row_index 是新采集出现的身份；相同身份、相同 version/元信息/诊断幂等，矛盾报冲突。一个页重复的不同下标、跨页的相同行都是不同出现。
- 版本行的 evidence_sufficient 是首次捕获标志，匹配旧内容时不覆盖；本次观察的充分性由其 v4 快照、行定位、文件校验及 unit 成功状态判定。返回本次 observation 引用供后续消费，不能只返回首次 snapshot。
- 首次内容版本元信息不改为最新披露；每个观察记录本次元信息、entry_time、接收时间及标准化诊断。新增可选 API 参数传 observed_at，不靠默认 localtime 表示新捕获。
- 固定来源 mapping_version 和有效配置放 unit.request_scope/run.request_scope；代码标识放 crawler_version。2B 使用既有 2A metric/semantic 算法和固定映射，不以新原体哈希替换内容身份；任何日后映射定义变更需另行版本设计。

## 必须提供的接口能力

下面固定行为和职责，具体类型命名可等价实现，但不能只返回无来源的记录列表：

- RawStore.publish(context, body: bytes) -> StoredResponse；context 包含 run/unit/page/http_attempt/fetched_at 和必要 HTTP 元信息。返回相对路径、raw_hash、raw_bytes、format。
- RawStore.verify(reference) -> VerifiedBody：受 raw_root 限制，读文件、有限解压并核对实际 bytes/hash/大小；只返回校验结果不能改文件。对账和写入共用。
- compute_raw_hash(bytes)；不能复用会改数值/JSON 的 metric 规范化函数。
- Repository 的 unit/attempt CRUD：登记、短事务状态更新、按 run 查询、已完成单元幂等读取、最终汇总。
- save_raw_snapshot 扩展 v4 参数并保留有用的旧调用参数。新写入必须检查真实文件及哈希，不能仅信任调用方传的摘要/“已验证”布尔。
- create_statement_version 新证据写入必须校验：format=http-body-v1、valid 页、unit 完整、run/source/report/company、文件当前存在且哈希正确、row_index 内容能重建传入映射/标准值。需要的 verifier 可注入 Repository（协议不依赖具体 crawler），缺 verifier 时不能声明新证据充分。
- 原低层无文件的测试快照不是 v4 充分证据。原 2A 假路径 fixture 应改为临时真实文件；可保留 legacy-unverified 的审计写入能力，但新 version/observation 不能据它声明 evidence_sufficient=1。
- observation 幂等预检需在当前投影修改前，或在完整业务 UOW 内；冲突失败不能残留被恢复/被替代的部分当前状态。

文件验证代表提交时核对过原始证据，不承诺外部进程永远不能破坏文件；audit-raw 和后续 replay 必须重新校验，不能只信历史标志。

## 协调器与契约依赖

HTTP sink 仅登记 evidence，不创建版本或推进水位；分页 complete 通过后，协调器才开启业务 UOW。unit 完整性应由已校验的 FetchResult/所有最终页证据在业务 UOW 内证明，不能先单独提交“unit.success”再写版本。Repository 可接收当前 unit 的完整性证明/页集合并核验，而不是强制查询一个必须事先提交的 success 状态，避免新的循环依赖。

兼容 raw 的 NULL 公告幂等与当前选值见 [执行契约](2b-contract.md)；不得用删除旧 raw 历史来解决。原阶段一 clean/features 保持原计量契约。

## 配置与审计输出

- raw_store.root：省略时 DB 所在目录/raw；自定义相对路径也以 DB 所在目录解析，新 file_path 始终相对根。
- raw_store.max_body_bytes：默认 33554432，正整数；解压校验同限。
- crawl.max_pages_per_unit：默认 1000，正整数；达到截断条件为失败。
- 其他 retry/rate/timeout/stock_count/recent_periods 沿用既有配置键并严格校验，不能偷偷扩大抓取范围。

run.request_scope 存有效、去敏配置及入口模式；unit.request_scope 存可复现的业务请求范围。audit-raw 输出 run/unit/attempt/snapshot 身份、诊断码和计数；详细 body 仍留在被忽略的证据目录，报告默认 data/audit，不进 Git。
