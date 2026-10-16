# 2B 采集执行契约

版本：stage2b-v1；日期：2026-10-16（Asia/Shanghai）。架构交付，尚无 2B 功能实现。基准为 origin/main 的 c9da710b02eae5a5015a801987c41abd72f4d0e8，阶段 2A 已随 MR !4 验收合入。

本契约、[v4 数据与迁移](2b-schema.md)、[验收矩阵](2b-test-matrix.md)共同构成实现依据；2B 同主题上取代旧概要中“压缩重序列化 JSON”“按批次最终状态推进水位”的表述。2A 已发布行为与历史记录不倒改。按用户“契约到位后开工”的交接，实施方可按本契约进入 2B-1；本次架构侧只提交文档。

## 八项决定

| 问题 | 本批决定 |
|---|---|
| 尝试 | 每页实际 HTTP 调用的序号，从 1 开始；不是跨批次重跑次数。每次运行新 run，同一运行一个 unit 只执行一次 |
| 留存形态 | requests.Response.content 交给应用的完整响应体字节，gzip 存储；不是 resp.text、重序列化 JSON 或线级抓包 |
| 身份 | unit 的请求范围有稳定摘要；尝试唯一键 (unit_id,page_number,http_attempt)，一个响应尝试最多一个 snapshot。不同运行分别留存 |
| 双介质一致性 | 文件先原子发布，审计行随后独立提交；保留可对账孤儿，禁止失败时自动删除证据 |
| 写版本与行定位 | 2B 写版本与观察，2C 才切清洗读源；每个来源行保存 snapshot_id+row_index |
| CLI | 保留两个命令入口，共用一个财务采集协调器及持久化流程；crawl 的股票发现也留证据 |
| 状态 | 非空计划且所有财务单元合法空也是 success；0 单元 failed；任一失败退出 1，非法配置退出 2 |
| 失败单元 | 不留下部分兼容 raw/版本/观察/水位；文件、attempt/snapshot 和失败单元审计保留 |

## 1. 范围与分批

- 2B-1：RawStore、v4 迁移、证据校验/只读对账、数据访问接口；只做离线 fixture。
- 2B-2：带原始证据的分页、HTTP 尝试观察接口、统一采集单元协调器、版本与兼容 raw 写入。
- 2B-3：crawl/incremental 接线、状态与退出码、配置/帮助/文档及全矩阵验收。
- 各批可审阅提交；单独通过 2B-1 不等于采集链路或完整 2B 已验收。
- 继续使用 eastmoney 单源和阶段一字段映射/固定标准化规则。未知币种、范围、期间口径不猜；证据充分不等于财务可比。
- 不实现 2C 消费切换、2D replay、as_of、双源、定时调度、全市场运行或生产迁移。联网验收仍限此前获准小样本。

## 2. 运行、单元与请求身份

一个 CLI 调用生成一个新的 ingest_run。run 创建、running 状态先短事务持久化，再发请求。正常重跑新建 run，不在旧 run 内恢复失败 unit，不自动重取整个 unit。

金融 unit = 来源 + 报表 + 公司 + 规范化请求范围；股票发现 unit_kind=stock_list、report_name=stock_list、secucode=""，只做目录发现。unit_key 是这些逻辑字段的 canonical JSON SHA-256；unit_id 是本 run 内 UUID，UNIQUE(run_id,unit_key)。同一 unit 被重复调度直接复用已提交结果，不能再次 GET 或重复计数；running/failed 的同 run 重入明确拒绝，恢复留给新 run。

request_scope 必须含 endpoint（去敏）、实际 filters/columns/sort、page_size、recent_periods、显式日期范围，以及固定 mapping_version=eastmoney-stage1-v1。股票发现另含 stock_count、市场筛选。页号/HTTP 重试号不进入范围摘要；页数安全上限记录在有效配置中。公司集合去重、报表白名单及所有参数在请求前校验。

http_attempt 对每一页从 1 开始，记录实际 HTTP GET 的次序。当前 max_retries 为最大尝试总次数（含第一次），保留配置键，帮助文字纠正含义。网络/HTTP 失败按现有上限和退避重试；success=false 业务失败不重试；JSON 解码、结果结构错误终止 unit。不重试存储故障，不额外启动无界 unit 重试。

## 3. HTTP 证据与 RawStore

增加可选 attempt observer/sink 到请求边界，未传时保留旧 fetch_page/fetch_all 的签名和成功返回类型。新证据路径必须依次暴露：

1. on_attempt_start：先登记 attempt_id、unit/page/http_attempt、UTC 开始时间，独立短事务提交。
2. 收到响应：在 raise_for_status、JSON 解析、success 校验之前读取 response.content 并发布证据，记录状态码与结束/接收时间。
3. on_attempt_finish：记录 ok/http_error/business_error/decode_error/schema_error/transport_error/storage_error 之一及去敏原因。页校验完成前不能先标 ok；传输层成功返回只交付待验证 capture，由页验证层结束成功尝试。
4. 网络无响应：不伪造 body、空哈希或 snapshot，仅保留 transport_error attempt。收到 4xx/5xx、HTML、非法 JSON、success=false 的响应都留原字节。

bytes 指 HTTP 客户端交给应用的响应体，不承诺网络封包/传输压缩编码的原貌。raw_format=http-body-v1。compute_raw_hash(body: bytes) = SHA256(body)，与 metric/semantic 哈希不同；禁止 decode、JSON canonicalization 或 normalize_numeric 后再算原始摘要。例如空格/编码不同的 JSON 原文可以有不同 raw_hash，但相同规范化财务值仍复用版本。

文件 gzip 解压后必须逐字节等于 body；摘要对解压后的字节算，不对 gzip 头/压缩字节算。保存 http_status、content_type、text_encoding（已知时）、raw_bytes、fetched_at；encoding 只是解码元信息，不改被保存的字节。HTTP headers 只白名单保留必要字段，不保存 Cookie/Authorization。请求参数只保存业务白名单，不输出整段 body 到日志。

默认 raw_root = 已解析数据库文件所在目录/raw；database.path 原有解析方式不在本批偷偷改动。raw_store.root 可显式覆盖，相对路径按已解析 DB 目录解释；启动后固定绝对根，数据库内仅存根下相对 file_path。默认 data/raw 已被 /data/ 忽略；可配置路径仍必须不被 Git 跟踪，补充文档与忽略配置。

路径固定为：
~~~
eastmoney/{run_id}/{unit_key前两位}/{unit_id}/p000001/a000001.body.gz
~~~
页/尝试位数可扩展，不能截断数值。路径仅用生成的 UUID/摘要和数字，不用公司名、任意 report_name 或用户输入作路径段。拒绝绝对相对混用、..、越根与 symlink 越根。每个 run 各写一份，本批不做跨运行内容寻址、硬链接去重或 GC。

发布：同目录唯一临时文件 → 写完/flush/fsync/关闭 → 原子且“不替换已有目标”的发布 → fsync 父目录。禁止 os.replace 覆盖既有证据。可用 link 创建目标后移除临时文件或等价 create-if-absent 策略。同一身份已存在且解压字节/摘要完全一致时复用；不同内容、已有损坏文件或不支持所需原子语义时明确失败，不覆盖、不回退普通覆盖写。

新增 raw_store.max_body_bytes（默认 32 MiB，正整数），对留存与对账解压施加边界；超限为失败且不提交业务数据，不将截断文件标成完整证据。

## 4. 文件、审计与业务事务

协调器拥有 run 生命周期；入口若 conn 已有活动事务，必须在发请求前拒绝，避免把网络和审计提交嵌入调用方事务。底层 Repository/UOW 仍保持 2A 的可组合、嵌套 savepoint 行为。

按顺序：
1. 创建 run、计划 unit，短事务提交。
2. 无业务事务持有期间请求各页；每次收到响应先发布文件，再登记 snapshot/完成 attempt，分别短事务提交。
3. 全部页面、范围、行结构和标准化准备完成后，开始一个 unit_of_work：兼容 raw + 内容版本 + 行观察 + 水位 + unit success/success_empty/提交后计数共同提交。
4. 任一步写入失败：该业务 UOW 全回滚；随后另一个短事务将 unit 标 failed。审计 attempt/snapshot/文件不回滚。
5. 所有单元完成后，从已持久化 unit 状态计算 run 最终状态，再短事务提交。内存计数只在 UOW 成功退出后累加。

不在网络请求期间持有 SQLite 写锁。run/审计状态写入失败时退出 1；不要打印批次成功，可能留下 running，供对账识别。进程硬中断不承诺立即写完 ended_at；保留 pending/running，不擅自把未知工作标成功。

失败后保留三类对象：①有 snapshot 引用的失败响应，②有文件但 snapshot 插入未提交的孤儿，③只有 attempt、无响应的网络失败。后两类不得升级为 evidence_sufficient=1。临时文件/孤儿不自动删，后续运行也不能覆盖。

新增 audit-raw --run-id（或受限的 --all）只读命令：检查引用文件存在/解压/大小/哈希、孤儿和临时文件、legacy-unverified、未完成 run/unit/attempt、已成功单元却缺证据等。输出结构化报告；发现问题退出 1，干净退出 0，参数错误 2。不得执行删除、自动补抓、重算、变更水位或自动将 running 判成功；本批无自动恢复/GC 命令。新的 run 可重跑同逻辑范围，旧审计保持。

## 5. 分页结果与空成功

新 fetch_all_with_raw 返回结构化 FetchResult：
- records：每项 SourcedRecord(value: dict, snapshot_id, row_index, page_number, attempt_id)，row_index 为 result.data 的 0-based 下标。
- pages：全部最终成功页的 CapturedPage 引用，含页号、snapshot/attempt、声明页数和源行数。
- complete、source_row_count；仅全部请求范围完整时 complete=True。
失败抛带 unit/attempt/snapshot 引用的类型化异常，不返回可误当成功的部分 records；失败响应引用也已在审计库中。

每页先留证据再校验：success 必须为 true，result 对象，data 为对象列表，pages 为非 bool 非负整数。首响应 pages=0 或 1 且 data=[] 为合法空（若 count 存在需为 0）；pages>1 的首空页，以及非首空页均为结构不完整。非空页要求 pages>=1。后续声明页数必须与首响应相同；count 若出现须为非 bool 非负整数，与首个已出现 count 一致；完整取完时 sum(len(data)) 与 count 一致。count 按源行计，不按去重后的值计。

max_pages/安全上限截断不能变成成功；预知页数超过上限，保留首响应后以 incomplete_scope 失败，不提交部分版本。新增 crawl.max_pages_per_unit 默认 1000，为正整数，两个 CLI 共用。recent_periods 在完整分页后按已规范化报告期选最近 N 个不同报告期，保留这些期全部修订，不按前 N 行取值；不提前停页。所有收到的原始页都留存，即使该页的记录最终不进入 N 期业务集合。

合法空财务单元：保留有效空响应 snapshot，unit=success_empty，record_count=0，不写版本/兼容 raw，不推进或清空既有水位。错误结构/失败响应绝不当空成功。

## 6. 页到行、版本和兼容写入

2B 就创建 statement_version/statement_observation，2C 才让清洗从版本读入；本批 clean/features 仍由原有命令消费兼容 raw。不能因此把标准化值写回原计量 raw/clean 造成二次转换。

新财务行必须与请求公司匹配，且有可规范化的报告期；关键标识无效、data 元素非对象或映射失败使整个单元失败。公告/修订日缺失保持 NULL，非法非空可记诊断后置未知；不得借 fetched_at 补公告日。非法数值按唯一标准化入口返回 None+诊断，保留原字段与原页证据。固定 FIELD_UNITS，不消费 fixture 运行时覆盖。

先收集完整选中记录，再按公司/期/表分组，以与阶段一相同规范化后的完整映射记录调用 dedup.sort_key 升序处理，最后选中值成为当前投影；不能按 HTTP 返回/页到达顺序使旧公告盖掉已知新公告。全体历史内容与每个来源行的观察仍保存，歧义保留诊断。同值不同公告复用版本但不丢新观察；A→B→A 可恢复 A。

当前投影的候选不能仅限本次返回：业务 UOW 内合并同组已持久化的历史候选（原始映射字段及各次观察元信息），再按同一 dedup.sort_key 选取唯一当前版本。本次只回较旧公告时不得让旧版降级覆盖已知新版；A→B→A 的恢复也必须有选值规则优先的 A 观察支持。旧观察缺 entry_time 保持未知，不用 fetched_at/observation_seq 代替；歧义依旧诊断。可复用兼容 raw 与版本/观察构造候选，不要求 2C 消费切换。

每个新观察持久化 snapshot_id+row_index（指向解压 body 经同一 JSON 解析后 result.data 下标），并保存该次公告/修订、entry_time、标准化诊断。observed_at 取该响应真实接收时间（UTC），不是最终数据库提交时间；未获知修订时间不推断。

同一行在两个页出现：各自关联自己的 snapshot/row_index，不能任取一页；相同语义只一个内容版本。一个页内重复行的两个下标各自可追踪。同一 snapshot+row_index 重处理是同一个来源出现，幂等复用观察；与已保存版本或元信息矛盾则报 CaptureConflict，不静默改旧观察。禁止用列表.index/值相等倒推页归属。

版本/观察是不可变证据，当前标记按既定规则更新。旧内容版本被新快照再次观察时，版本行的 snapshot/evidence_sufficient 仍描述首次捕获，不覆盖为新值；本次充分性通过新观察关联的已核验快照和成功单元判定，不依赖旧版本首次标志。新采集结果应同时返回 version_id 与本次 observation 身份，不能把首次 snapshot 冒充本次输入。evidence_sufficient=1 只用于 http-body-v1、文件校验通过、分页完整且行定位一致的输入；失败页、未校验 legacy 引用、缺文件/错哈希/错 run 不能宣称充分。是否可比仍按阶段一未知拒算策略，不能由证据充分推导可比。

兼容 raw 非空公告键按同一选值顺序写入，保留原计量。公告为 NULL 的重复抓取，须按公司/期、空公告、entry_time 及映射内容（排除本次 crawled_at）识别同一旧行，不因 SQLite NULL 主键允许重复就无限插入；不同内容仍保留。不得为实现幂等删除旧历史行；计数区分 source_rows、selected_rows、new_versions、new_observations、compat_writes。

## 7. 两条 CLI 和批次状态

两个入口必须调用同一 financial ingestion service，不能各复制分页、标准化、文件/版本/水位写入逻辑。crawl 先经带证据的股票发现再产生财务单元；incremental 从本地 stock_list 产生财务单元。同一公司/报表/范围应有相同持久化与退出语义，允许来源公司集合不同。

股票发现采用相同 RawStore/attempt 审计，但不生成财报版本/财务水位。当前 StockListCrawler 不继承 BaseCrawler，新带证据路径必须复用同一请求执行器/observer 协议，不能另写一套失败留存逻辑。新带证据方法须显式失败，禁止沿用 fetch_stock_list 出错 break 后返回部分列表的成功语义；旧无证据接口可保留兼容调用。每页校验 data.diff 为对象列表、total 为非 bool 非负整数；ret 若出现须为 0。请求 count 是前 N 只的明确范围：足够 N 或按 total 完整遍历可用数据后成功，保存最后一页全量原体。中途失败不更新 stock_list，不启动财务单元；公司代码/市场不合法、分页总量变化不能静默跳过。发现 0 公司为 empty_universe 失败，不伪装完成。

| 输入/单元结果 | run 最终状态 | failed_units | 退出码/数据 |
|---|---|---|---|
| 非法 N/count/page_size/上限/配置 | 不发请求，不创建新 run | — | 2 |
| 0 计划单元（如本地空股票表） | failed / no_units | 0 | 1，无财务写入 |
| 股票发现失败或结果为空 | failed | 1（发现单元） | 1，不更新列表、不启动财务 |
| 非空财务计划全成功，含全部合法空 | success | 0 | 0；空单元不推进水位 |
| 至少一个财务单元成功或空成功，另有失败 | partial_failure | 实际 failed 单元数 | 1；成功单元已提交 |
| 财务单元全部失败 | failed | 财务 failed 数 | 1 |
| 中断/最终状态提交失败 | 保留 running/未完成 | 不编造 | 1（能退出时），交只读对账 |

股票发现成功不是把“全部财务失败”改成 partial_failure 的理由；run 终态汇总以财务单元为主，发现阶段失败是独立先决失败。failure_count 不能混入未启动的公司，单位失败只计一次，不按失败页/HTTP 次数重复计。

水位仅在财务 unit 完整成功的同一业务事务内单调推进：旧值与选中记录最大可信 notice_date 取较新者；无有效公告/空成功不变，不用公告水位过滤报告期，不用采集时间顶替。unit.status 与水位共同提交。run.running 期间已成功单元可以有水位；最终状态来自单元汇总，存在成功单元时不得把整个批次最终标成 failed。这正式替代旧概要按 run 最终 status 才允许水位的循环约束。

## 8. 交付约定

先实施 [v4 数据与迁移](2b-schema.md)，再接线，最后完成 [验收矩阵](2b-test-matrix.md)。每个 MR 明确基准/结果 SHA、变更、故障注入范围、实测与限制，更新 progress/DEC-006；存储目录、数据库、失败 body 和对账输出不进 Git。

本次设计选择不要求实现方再就八项常规选择逐一索取确认。不能自行简化为只留成功页、返回截断成功、删失败证据，或把 2C/2D 标成 2B 完成。真实运行范围/生产操作另按用户明确指令。
