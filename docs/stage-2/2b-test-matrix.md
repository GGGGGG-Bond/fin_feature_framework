# 2B 验收矩阵

版本：stage2b-v1。共同依据：[执行契约](2b-contract.md)、[v4 模型](2b-schema.md)。下列是必须覆盖的场景组，不是宣称已有测试或预定测试总数。以固定实现 SHA 独立复现，不仅引用实现方的通过计数。

## 2B-1：证据、模型与失败边界

| ID | 场景/注入 | 必须断言 |
|---|---|---|
| B01 | JSON 空格/编码差异、HTML、非法 JSON、空 body | gzip 解压逐字节等于原体；hash=SHA256(bytes)；不是 parsed JSON hash |
| B02 | 同 run/unit/page/http_attempt 相同 body 再发布 | 文件与 snapshot 身份复用，内容及捕获时间不被改写 |
| B03 | 同身份不同 body、已损坏目标、并发发布竞争 | 报冲突/失败；已有文件逐字节不变；不使用覆盖写回退 |
| B04 | 同内容不同 run、不同公司/范围/页/尝试 | 各自身份和路径分离；新 run 不依赖全局文件去重 |
| B05 | 缺文件/错 hash/错大小/截断 gzip | 保存或充分性声明拒绝；没有新增业务版本/观察 |
| B06 | ../、绝对路径、symlink 越根、非法路径段 | 拒绝越根；根外文件不被读写或覆盖 |
| B07 | temp 写入、fsync、发布、snapshot DB 插入各点故障 | 无假成功；业务 0 写入；已有证据不删；孤儿/temp 可被对账识别 |
| B08 | 体积超限、超限解压、GZip 损坏 | 有界失败，不把截断 body 登记为完整响应 |
| B09 | 真实 v3 含历史库升级 v4；空库顺序迁移 | 旧表/行/原始 hash 与标记保持；新增信息 legacy-unverified/NULL；FK 检查通过 |
| B10 | v4 CREATE/ALTER/INDEX/版本登记后注入失败；外层事务 | DDL/行/登记全部回滚、外层未提交；重试与一次成功一致 |
| B11 | WAL 已提交数据的备份；备份失败 | 副本包含已提交行；失败不执行迁移；备份不覆盖原文件 |
| B12 | v4 缺表/列/索引的伪完整库 | 初始化明确拒绝，不以版本数字 alone 跳过结构检查 |
| B13 | unit_key 范围参数改变、参数键序、公司列表重复 | 同语义键序稳定；N/筛选/来源/报表/页大小改变不混单元；计划无重复 unit |
| B14 | unit/run/source、attempt/unit、snapshot/run/report/company 错配 | 逐项拒绝；run_id 缺省但有 snapshot 时导出其 run，不保存矛盾/未知批次 |
| B15 | 同 attempt 重登记、同 row locator 重处理/矛盾输入 | 同内容幂等；不同内容报冲突；当前指针及原观察不被部分修改 |

## 2B-2：尝试、分页、行血缘与单元事务

| ID | 场景/注入 | 必须断言 |
|---|---|---|
| B16 | 网络无响应→500 body→200 body | 三个真实 attempt 次序；前者无 snapshot，后两者各有原体；仅最终成功页可作业务输入 |
| B17 | success=false、非法 JSON、结果结构错误 | 先留原体再报错；正确终止/不伪装空成功；日志不输出 body |
| B18 | RawStore/审计写入失败 | 不进入或继续业务写入，不因存储错误继续 HTTP 重试后吞掉问题 |
| B19 | 多页正常成功 | 页数完整、每行 snapshot+0-based index 可回原页重建；行数与 count 一致 |
| B20 | 第二页最终失败、页数变化、count 变化、早空页 | 前页文件/审计保留；业务 raw/版本/观察/水位无部分提交 |
| B21 | pages/data/success 非法类型；pages=0 但非空；data 元素非对象 | 全部明确失败；拒绝 bool 充当整数；不把缺字段当空列表 |
| B22 | max_pages 或安全上限截断 | incomplete_scope 非零；不返回 complete=True，不推进任何该单元水位 |
| B23 | 全部分页后选最近 N 个报告期、多次修订、乱序报告期 | 原页全留存；选中 N 个不同规范化报告期的全部记录；不是截前 N 行/提前停页 |
| B24 | 一行跨页出现、页内两行相同、行顺序变化 | 每个出现保留自己 locator；同语义版本复用；不以值相等猜页归属 |
| B25 | 同日期不同内容、同内容不同公告、A→B→A | 版本/观察/首次及本次元信息正确，当前投影唯一；旧不足版本新增已核验观察不冒改首次标志 |
| B26 | B1 两反例、元信息歧义、日期格式与抓取顺序相反、后续批次只返回旧公告 | 与阶段一同选值；NULL 公告不借采集时间取胜；歧义有据；已观察新版不因后续旧页降级 |
| B27 | 16.75%、原金额、未知单位、非有限值、fixture 单位污染 | 原计量 raw 不变；标准值/诊断正确且可序列化；证据充分不自动放行可比性 |
| B28 | 公司不符、无有效报告期、映射失败 | 整单元业务回滚；保留失败响应审计；不静默跳行后报完整成功 |
| B29 | raw_* 空公告重复成功抓取 | 同一映射内容不无限新增旧 raw；内容变化保留历史；兼容 raw 仍是原计量 |
| B30 | raw、版本、当前替代、观察、水位、unit 状态各写入后失败 | 该单元各表全行快照一致回滚；成功单元不受其他单元失败影响 |
| B31 | commit 失败、提交前内存计数已增加的诱因 | CLI 非零；统计以已提交结果为准，不将回滚记录算成功 |
| B32 | 协调器在已有外层事务中被调用 | 请求前拒绝；无 GET/文件/审计提交；底层 Repository 嵌套 UOW 原回归仍通过 |
| B33 | 捕获与提交间延迟、未知公告/修订、时区输入 | 新 observed/fetched 时间为真实接收 UTC；不借其填公告，不把旧时间重标 UTC |

## 2B-3：入口、状态、对账与完整验收

| ID | 场景/注入 | 必须断言 |
|---|---|---|
| B34 | crawl 与 incremental 相同财务公司/表/范围 | 调同一服务；版本/水位/失败/退出码语义一致；crawl 股票发现另计 |
| B35 | 非空计划全非空成功、全部合法空、空/非空混合 | run=success，退出 0；空响应有快照，无业务行且既有水位不变 |
| B36 | 部分失败、全部财务失败、发现成功但财务全失败 | partial_failure/failed 正确；退出 1；failed_units 按失败单元，不按重试/页计 |
| B37 | 0 单元、本地空股票表、股票发现返回 0 公司 | no_units/empty_universe 明确失败，不伪装成功；不造财务失败单位 |
| B38 | 非法 N/count/page_size/max_retries/上限/目录配置 | 退出 2，请求数 0，不创建新 run，不扩大范围 |
| B39 | 股票列表第二页失败/结构错/总量改变/无效市场代码 | 保留失败证据，不返回部分列表供正式财务使用，不改 stock_list |
| B40 | 发现前 N 只，最后一页超过 N；合法源耗尽不足 N | 原页完整保留，业务集合遵从声明范围；有总量证据的耗尽可成功 |
| B41 | run 最终状态提交失败/进程在各边界中断 | 能退出时为 1；未完成状态保留，对账能识别；不得补造 success 或自动改水位 |
| B42 | audit-raw：缺失、篡改、孤儿、temp、legacy、running | 结构化问题列表/计数，非零；DB/文件前后快照无改动；不补抓/删除 |
| B43 | 水位旧值更晚、未知公告、空成功、成功后其他单元失败 | 单调不回退；仅成功单元同事务推进；不按公告水位过滤报告期 |
| B44 | HTTP/分页诊断与请求参数、运行产物 Git 排除 | 参数白名单，body 不进日志；默认目录被忽略；报告/失败体无新增 Git 跟踪 |
| B45 | 旧成功 API 返回类型、阶段一 clean/features 与 2A 回归 | 不提前切 2C；原公式、未知拒算、原计量和嵌套事务断言保留 |
| B46 | 新写入 legacy 引用/假文件/伪“verified”标志 | 不能声明充分；fixture 以临时真实文件满足新边界，不删断言或绕开验证器 |

## 交付证据与运行限制

- 2B-1 至少覆盖 B01—B15、B42 的存储对账部分；2B-2 覆盖 B16—B33 及相关幂等；2B-3 覆盖 B34—B46，最终全项目回归。
- 测试建议分 test_raw_store、test_ingestion_migration、test_capture_pagination、test_ingestion_unit、test_ingestion_cli 等；组织可调整，ID 必须可追到实际测试/fixture/结果。
- 缺失/错误/故障分支必须实际注入；不仅验证返回值、int 类型或同连接可见。事务与持久化测试重连；篡改测试使用真实临时文件；迁移在填入旧数据后执行。
- 以基准现有 263 项为回归起点，测试数量不作为单独验收条件；允许为新严格证据校验改造假文件 fixture，不能把旧断言改弱来制造通过。
- 全项目离线测试及 CLI --help；新增配置非法场景、链接/格式检查；小样本真实联网仅在已获准范围核验，不把 mock 结果写成来源探测。没有必要为本架构文档本身运行网络或业务迁移。
- 每次送审固定 head、列明输入范围/临时 DB 和目录/验证命令/实际结果/失败样例；提交推送前核对项目、分支和作者/提交者。用户按既有流程创建/合入 MR。
- 2B 完成不宣称已能重放特征或历史时点查询；这两项分别属于 2D/阶段 4。

## 2B-1 实现映射（实现 agent 记录，2026-10-16；待独立审阅）

固定实现：分支 `feat/stage2b-1`，基准 `593d65b`。命令：

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_raw_store.py tests/test_ingestion_migration.py tests/test_ingestion_unit.py tests/test_raw_audit.py -q -p no:cacheprovider
```

| ID | 用例位置 |
|---|---|
| B01 | `test_raw_store.py::TestB01BodyIdentity`（逐字节/gzip/空格与编码差异/HTML/非法 JSON/空 body；`compute_raw_hash` 原语见 `TestRawHashPrimitive`） |
| B02 | `test_raw_store.py::TestB02ReuseSameIdentity`（文件字节与 mtime 不变、同 attempt 复用快照） |
| B03 | `test_raw_store.py::TestB03ConflictNoOverwrite`（不同 body、损坏目标、抢占竞争、link 不支持拒绝覆盖写回退） |
| B04 | `test_raw_store.py::TestB04IdentitySeparation`（run/公司/页/尝试/unit_key 各自分离；路径布局与页号不截断） |
| B05 | `test_raw_store.py::TestB05MissingOrTamperedEvidence`（缺文件/错哈希/错大小/截断 gzip/提交后篡改/legacy 不能声明充分，且无新增版本与观察） |
| B06 | `test_raw_store.py::TestB06PathContainment`（`..`、绝对路径、非法段、symlink 越根；根外文件不变） |
| B07 | `test_raw_store.py::TestB07FailureInjection`（fsync 失败、snapshot 插入失败留孤儿并被对账识别、业务 UOW 回滚保留文件与 attempt） |
| B08 | `test_raw_store.py::TestB08SizeBounds`（留存超限、解压超限、损坏 gzip、非法上限配置） |
| B09 | `test_ingestion_migration.py::TestB09UpgradeFromV3`（v3 含历史库升级保行/哈希/legacy 标记、空库按序迁移、幂等、FK 检查） |
| B10 | `test_ingestion_migration.py::TestB10MigrationFailureRollback`（建完索引后注入失败：DDL/列/版本回滚、外层事务未提交且外层数据仍在、重试与一次成功结构一致） |
| B11 | `test_ingestion_migration.py::TestB11Backup`（WAL 已提交行进副本、备份失败不迁移、目标已存在拒绝且原文件不变、自动命名唯一、空库不需备份） |
| B12 | `test_ingestion_migration.py::TestB12IncompleteV4Rejected`（缺表/缺列/缺索引、仅版本号=4 的伪完整库、gap 报告只读） |
| B13 | `test_ingestion_unit.py::TestB13UnitKeyAndPlanning`（范围参数区分、键序稳定、同语义只计划一次、新 run 独立单元、stock_list 形状、单元生命周期与失败重入拒绝、`max_pages_per_unit` 配置边界） |
| B14 | `test_ingestion_unit.py::TestB14CrossEntityConsistency`（run/来源、attempt/unit、snapshot 报表/公司/页/状态码、快照归属 run、run_id 导出、版本 run 错配、单元证明身份） |
| B15 | `test_ingestion_unit.py::TestB15ReplayAndConflict`（同 attempt 重登记与重复结算、快照重放幂等与冲突、来源行重放幂等、矛盾输入不部分修改投影与观察、同内容不同行共用版本、核验器不符拒绝、校验单向） |
| B42 | `test_raw_audit.py`（缺文件/篡改/孤儿/temp/legacy/未完成 run-unit-attempt/成功单元缺证据、run 范围过滤、报告前后库与文件指纹不变、`cmd_audit_raw` 退出码 0/1/2 与报告落盘） |

未在本批覆盖：B16-B33（2B-2）、B34-B41、B43-B46（2B-3）。

## 2B-1 审阅整改映射（实现 agent 记录，2026-10-17；待复审）

固定送审头 `b8b2b3f`，整改在 `feat/stage2b-1` 继续提交。审阅材料：分支 `docs/review-stage2b1-b8b2b3f`（commit `8857ecc`）。命令：

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/ -q -p no:cacheprovider -m 'not requires_network'
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -I docs/reviews/stage2b-1-b8b2b3f-repro.py
```

| 编号 | 级别 | 整改用例位置 |
|---|---|---|
| R1 | P1 | `test_ingestion_migration.py::TestR1InitSchemaBackup`（含历史 v3 库升级先备份、备份失败阻断迁移、空库不备份、已 v4 不备份） |
| R2 | P1 | `test_raw_audit.py::TestR2AuditReadOnly`（journal_mode 不变且不建根、根已存在写报告不改模式、缺库返回 1、缺根返回 1） |
| R3 | P2 | `test_raw_audit.py::TestR3NoFalseOrphans`（两个健康 run 过滤干净、其他 run 孤儿不在范围） |
| R4 | P1 | `test_ingestion_unit.py::TestR4LocatorGroupAndDiagnostics`（不同报告期冲突、不同诊断冲突、全同幂等） |
| R5 | P2 | `test_ingestion_migration.py::TestR5BackupExclusive`（并发目标拒绝、失败只清自己临时） |
| R6 | P2 | `test_ingestion_migration.py::TestR6StructureGapColumns`（缺 unit/attempt 列拒绝、缺 detail 拒绝） |
| R7 | P1 | `test_ingestion_unit.py::TestR7AllFinalPagesVerified`（另一最终页缺文件拒绝、被篡改拒绝、全在则接受） |
| R8 | P2 | `test_raw_audit.py::TestR8SuccessUnitAllFinalPagesValid`（pending 最终页报告、invalid 最终页报告、全 valid 干净、失败重试页不阻断） |

反例脚本结果：R1/R2/R3/R4/R6/R7/R8 均按预期抛错；R5 反例为假阳性（monkeypatch `sqlite3.connect` 拦截对 `target` 的连接，但整改改为私有临时 + `os.link`，从不直接连接 `target`，故不触发；已用直接探针验证真实竞争被阻断）。

## 2B-1 复审整改映射（实现 agent 记录，2026-10-17；待复审）

固定送审头 `e3d03f7`，整改在 `feat/stage2b-1` 继续提交。审阅材料：分支 `docs/review-stage2b1-e3d03f7`（commit `5550052`）。命令：

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/ -q -p no:cacheprovider -m 'not requires_network'
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD:$PWD/tests" .venv/bin/python -I docs/reviews/stage2b-1-e3d03f7-repro.py
```

| 编号 | 级别 | 整改用例位置 |
|---|---|---|
| C1 | P2 | `test_ingestion_migration.py::TestC1StructureValidationComplete`（缺 started_at/ended_at/failure_code/detail 拒绝、同名非唯一索引拒绝、错列索引拒绝） |
| C2 | P1 | `test_ingestion_unit.py::TestC2TerminalFailureNotSufficient`（http_error 拒绝、非 2xx 拒绝、2xx 接受）；`test_raw_audit.py::TestC2AuditTerminalFailure`（http_error 报告、非 2xx 报告） |
| C3 | P2 | `test_raw_audit.py::TestC3MissingDeclaredPage`（缺中间页报告、缺尾页报告、完整页干净） |
| C4 | P2 | `test_raw_audit.py::TestC4FailedResponseEvidence`（http_error 无 snapshot 报告、transport_error 无 snapshot 干净） |

反例脚本结果：R1-R8 原反例全部关闭（返回 result）；C1-C4 均按预期抛错（缺陷被阻断）。脚本末尾 `assert not any('probe_error'...)` 失败是预期——脚本把"无 probe_error"当作"缺陷仍存在"，C1-C4 现在正确抛错说明缺陷已修复。

## 2B-1 边界复审整改映射（实现 agent 记录，2026-10-17；待复审）

固定送审头 `c3522d2`，整改在 `feat/stage2b-1` 继续提交。审阅材料：分支 `docs/review-stage2b1-c3522d2`（commit `7b92c23`）。命令：

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/ -q -p no:cacheprovider -m 'not requires_network'
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD:$PWD/tests" .venv/bin/python -I docs/reviews/stage2b-1-c3522d2-repro.py
```

| 编号 | 级别 | 整改用例位置 |
|---|---|---|
| D1 | P2 | `test_ingestion_migration.py::TestD1IndexPredicateValidation`（WHERE 0 拒绝、反向 IS NULL 拒绝、正确谓词接受、遗漏非空条件拒绝） |
| D2 | P1 | `test_ingestion_unit.py::TestD2FinalAttemptFromDatabase`（旧成功→较新失败拒绝、旧成功→较新成功只接受最新） |
| D3 | P2 | `test_raw_audit.py::TestD3EnvelopeCompleteness`（无 count 缺页报告、无 count 完整页干净、HTML 报告、非法 JSON 报告） |

反例脚本结果：R1-R8/C1-C4 原反例全部关闭（返回 result）；D1-D3/D3b 均按预期抛错（缺陷被阻断）。脚本末尾 `assert not any('probe_error'...)` 失败是预期——D1-D3 现在正确抛错说明缺陷已修复。

## 2B-1 完整性复审整改映射（实现 agent 记录，2026-10-17；待复审）

固定送审头 `c905d4b`，整改在 `feat/stage2b-1` 继续提交。审阅材料：分支 `docs/review-stage2b1-c905d4b`（commit `d68c892`）。命令：

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/ -q -p no:cacheprovider -m 'not requires_network'
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD:$PWD/tests" .venv/bin/python -I docs/reviews/stage2b-1-c905d4b-repro.py
```

| 编号 | 级别 | 整改用例位置 |
|---|---|---|
| E1 | P1 | `test_ingestion_unit.py::TestE1ProofCoversAllFinalPages`（子集证明拒绝、全证明接受、重复页拒绝） |
| E2 | P2 | `test_raw_audit.py::TestE2EnvelopeDispatchByUnitKind`（合法空财务页干净、健康股票发现干净、股票发现缺 diff 报告） |
| E3 | P2 | `test_raw_audit.py::TestE3StrictEnvelopeValidation`（success=false 报告、缺 data 报告、bool 页数拒绝、混合 count 行数不一致、多余页报告） |

反例脚本结果：R1-R8/C1-C4/D1-D3 原反例全部关闭（返回 result）；E1-E3 均按预期抛错（缺陷被阻断）。脚本末尾 `assert not any('probe_error'...)` 失败是预期——E1-E3 现在正确抛错说明缺陷已修复。

## 2B-1 声明范围与原体一致性复审整改映射（实现 agent 记录，2026-10-17；待复审）

固定送审头 `2b86e07`，整改在 `feat/stage2b-1` 继续提交。审阅材料：分支 `docs/review-stage2b1-2b86e07`（commit `6ea6bf7`）。命令：

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/ -q -p no:cacheprovider -m 'not requires_network'
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD:$PWD/tests" .venv/bin/python -I docs/reviews/stage2b-1-2b86e07-repro.py
```

| 编号 | 级别 | 整改用例位置 |
|---|---|---|
| F1 | P1 | `test_ingestion_unit.py::TestF1ProofCoversDeclaredRange`（源端声明两页只登记一页拒绝、全声明范围接受、合法空单页不误拒）；`TestF2F3WritePathUsesBody`（写入路径共用原体结果：pages=0 非空拒绝、count 与原体行数不符拒绝、非对象元素拒绝、record_count 与原体不符拒绝） |
| F2 | P2 | `test_raw_audit.py::TestF2LegalEmptyCombination`（pages=0 非空报告、多页首空报告、非首空报告、合法空干净） |
| F3 | P2 | `test_raw_audit.py::TestF3BodyRowCountAndObjectElements`（原体长度 vs metadata 报告、财务非对象报告、股票非对象报告） |

反例脚本结果：R1-R8/C1-C4/D1-D3/E1-E3 原反例全部关闭（返回 result）；F1-F3 共 7 组反例（F1、F2a/b/c、F3a/b/c）均按预期抛错（缺陷被阻断）。脚本末尾 `assert not any('probe_error'...)` 失败是预期——F1-F3 现在正确抛错说明缺陷已修复。共享解析器：`fin_feature/storage/envelope.py`（`ParsedEnvelope`/`parse_envelope`），写入路径与只读对账消费同一份解析结果。

## 2B-1 复审通过（架构与审阅 agent，2026-10-17）

固定 `0710d8d` 复审通过：F1-F3 关闭，此前整改保持有效，未发现新的阻断项；离线 420 passed、11 deselected，独立验收 28 组全部通过。审阅材料：分支 `docs/review-stage2b1-0710d8d`（commit `1ce764a`）。

## 2B-2 实现映射（实现 agent 记录，2026-10-17；已复审并完成 R1-R3 整改，待新 head 复审）

固定实现：分支 `feat/stage2b-2`，基准 `0710d8d`。命令：

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/ -q -p no:cacheprovider -m 'not requires_network'
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_capture_pagination.py tests/test_ingestion_coordinator.py -q -p no:cacheprovider
```

| ID | 用例位置 |
|---|---|
| B16 | `test_capture_pagination.py::test_b16_retry_sequence_keeps_each_attempt_evidence`（无响应→500 body→200：三 attempt 次序、后两者各有原体、仅最终页可作输入、参数白名单）；`test_b16_transport_failures_exhaust_attempts` |
| B17 | `test_capture_pagination.py::test_b17_envelope_failures_store_body_first`（success=false/非法 JSON/结构错误：先留原体、不重试、异常不含 body） |
| B18 | `test_capture_pagination.py::test_b18_audit_write_failure_stops_without_retry`、`test_b18_raw_store_publish_failure_stops_without_retry`（不重试、不继续）；`test_ingestion_coordinator.py::test_b18_storage_failure_stops_before_business_writes`（存储失败后无兼容 raw/版本/观察/水位写入） |
| B19 | `test_capture_pagination.py::test_b19_multi_page_locators_and_count`（页数/行数/count 一致、每行 snapshot+0-based index 可回原页） |
| B20 | `test_capture_pagination.py::test_b20_second_page_failure_keeps_prior_evidence`（500/页数变化/count 变化/第二页空：前页文件与审计保留、异常路径无部分结果）；`test_b20_pagination_failure_returns_no_partial_result`；业务表无部分提交由 B30/B28 用例断言 |
| B21 | `test_capture_pagination.py::test_b21_invalid_envelopes_fail_explicitly`（bool pages、缺 pages、data 非列表、success 非严格 true、pages=0 非空、非对象行）；`test_b21_empty_result_is_not_treated_as_missing` |
| B22 | `test_capture_pagination.py::test_b22_declared_pages_over_limit_is_incomplete_scope`（预知超限不继续请求、首响应留存、`incomplete_scope` 非零） |
| B23 | `test_ingestion_coordinator.py::test_b23_recent_periods_selects_all_revisions_of_latest_periods`（完整分页后按规范化报告期选最近 N 期全部修订；原页全留存） |
| B24 | `test_capture_pagination.py::test_b24_duplicate_rows_keep_their_own_locators`（跨页/页内重复各自 locator）；`test_ingestion_coordinator.py::test_b25_same_content_different_notice_is_one_version_two_observations`（同语义版本复用） |
| B25 | `test_ingestion_coordinator.py::test_b25_versions_observations_and_a_b_a`（版本/观察/首次与本次元信息、A→B→A 恢复唯一投影）；`test_b25_same_content_different_notice_is_one_version_two_observations`；`test_b25_insufficient_old_version_gains_new_verified_observation`（旧不足版本新增观察不冒改首次标志） |
| B26 | `test_ingestion_coordinator.py::test_b26_ambiguous_notice_and_entry_uses_digest_and_logs`（并列歧义日志与确定性）、`test_b26_null_notice_does_not_borrow_fetch_time`、`test_b26_later_batch_with_older_notice_does_not_downgrade`、`test_b26_date_format_order_uses_normalized_notice` |
| B27 | `test_ingestion_coordinator.py::test_b27_standardization_keeps_original_measure_and_diagnostics`（16.75%、原金额、fixture 单位污染、inf 可序列化、可比性维度不自动提升）、`test_b27_non_finite_value_is_diagnosed_not_selected`、`test_b27_unknown_unit_is_not_guessed` |
| B28 | `test_ingestion_coordinator.py::test_b28_invalid_row_fails_whole_unit_without_partial_writes`（公司不符/报告期非法：整单元回滚、失败响应审计保留）、`test_b28_mapping_failure_rolls_back_unit`（映射失败注入） |
| B29 | `test_ingestion_coordinator.py::test_b29_null_notice_compat_raw_is_idempotent`（空公告重复成功抓取不新增、内容变化保留历史、raw 原计量） |
| B30 | `test_ingestion_coordinator.py::test_b30_failure_at_each_write_point_rolls_back_all_business_tables`（兼容 raw/版本/当前投影/水位/成功结算五点注入：各业务表一致回滚、证据保留）、`test_b30_successful_unit_unaffected_by_other_unit_failure` |
| B31 | `test_ingestion_coordinator.py::test_b31_commit_failure_rolls_back_and_reports_failed`（真实 `commit()` 抛错：UOW 回滚、连接无残留事务、单元/批次 failed、无业务写入）；真实 SQLite 锁失败与 commit+rollback 双失败见 R1 |
| B32 | `test_ingestion_coordinator.py::test_b32_active_outer_transaction_rejected_before_request`（请求前拒绝、无 GET/run/attempt、外层结束后可运行；底层嵌套 UOW 回归继续由 2A 测试覆盖） |
| B33 | `test_ingestion_coordinator.py::test_b33_observed_at_is_real_receive_utc_for_first_and_repeat`（首次与重复观察 observed_at 均等于响应真实接收 UTC）、`test_b33_invalid_notice_is_unknown_and_never_backfilled`（非法公告置未知、不用 fetched_at 补齐、不推进水位） |

补充：`test_ingestion_coordinator.py::test_run_and_unit_identity_and_request_scope` 断言 run/unit 身份、重复计划只请求一次、`request_scope` 不含页号/重试号且含固定映射版本。B31 的 CLI 非零退出在 2B-3 接线后由 CLI 用例复验；本批以 `RunResult.exit_code` 断言等价语义。未在本批覆盖：B34-B46（2B-3）。

## 2B-2 复审整改映射（实现 agent 记录，2026-10-17；待新 head 复审）

固定 `9277eeb` 复审的 2 P1 + 1 P2 整改用例（均为 `tests/test_ingestion_coordinator.py`）：

| ID | 用例 |
|---|---|
| R1 | `test_r1_real_commit_failure_rolls_back_and_reports_failed`（外部读锁使 SQLite 自身 commit 失败：不预先回滚；断言批次/单元 failed、连接无活动事务、先前成功单元与版本/水位不变、已有/重连 reader 可读、失败单元因外部锁保留 running）；`test_r1_unrecoverable_rollback_stops_run_without_claiming_success`（commit+rollback 双失败：`transaction_broken`、终止运行、批次 failed 不虚报）；`test_b31_commit_failure_rolls_back_and_reports_failed`（commit() 自身抛错、可回滚路径） |
| R2 | `test_r2_same_notice_stale_record_does_not_downgrade_compat_raw`（同公告日较旧 entry 后到：兼容 raw、阶段一实际去重、当前版本一致为 200；较新修订升级 300；失败批次回滚后原计量不变） |
| R3 | `test_r3_mixed_report_date_formats_use_one_selection_rule`（正序/逆序参数化：当前版本、兼容 raw、阶段一选值一致，版本保留原始日期）；`test_r3_cross_batch_mixed_format_matches_stage1`（跨批次历史候选同口径） |

验证命令与 2B-2 相同；整改后全量 475 passed、11 deselected，专项 55 passed。

## be4908d 独立复审（2026-10-17）

实跑 475 passed、11 deselected，2B-1 原测试保持；[报告](../reviews/stage2b-2-be4908d.md)、[脚本](../reviews/stage2b-2-be4908d-repro.py)、[9 组结果](../reviews/stage2b-2-be4908d-results.json)。B31 的实际 COMMIT 失败、commit+rollback 双失败后停止下一单元/不结算 run，以及 run 终态提交失败均明确断言通过；R3 原反例选值三处一致。

B26/R2 仍有未覆盖路径：同公告日次批 EITIME=null 或不提供时，历史 entry_time 从比较输入消失，兼容 raw/实际 deduplicate 降为 1.0、版本当前仍为 200。已有 R2 双方 entry 明确的用例通过不足以验收完整规则；需两变体断言 200 和保留历史 entry，并继续保留新明确修订升级与回滚对照。2B-2 剩余 1 P1，暂不进入 2B-3。

## d5adbe4 直接修复与自验证（2026-10-17）

用户授权本 agent 修复 R2；[交付](../reviews/stage2b-2-entry-evidence-fix.md)、[闭环脚本](../reviews/stage2b-2-entry-evidence-acceptance.py)、[结果](../reviews/stage2b-2-entry-evidence-results.json)。全量 478 passed、11 deselected；代码提交上 9 组闭环通过，R1/R2/R3 均按修复后预期断言。

B26/R2 新增 `test_ingestion_coordinator.py::test_r2_missing_entry_keeps_full_historical_selection_evidence` 的 null、absent、empty 三个参数：具体断言三条消费路径一致、历史 entry 保留、明确新修订可升级、未知观察不伪造 entry、原始 body 不变、重复内容版本复用、UTC 与重连持久化。修复前 null/absent 明确失败，修复后全通过。旧 be4908d 复现脚本只记录历史缺陷，应在该固定头运行；修复后使用本节闭环脚本。R2 由同一 agent 实施自验证，未冒充另一 agent 独立复审；B34-B46/2B-3 不在本批。
