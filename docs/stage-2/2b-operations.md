# 2B 采集与只读对账操作指南

本指南对应 2B-3 代码 `16e2fe4`。当前只有离线与脚本化 HTTP 验证，真实小样本运行结果另行记录。阶段状态见 [进展](../progress.md)，执行规则见 [契约](2b-contract.md)。

## 两个入口

在项目根目录运行；自定义配置参数放在子命令前。以下为操作示例，本批没有执行真实采集。

```bash
.venv/bin/python -m fin_feature.cli --config /path/config.yaml crawl
.venv/bin/python -m fin_feature.cli --config /path/config.yaml incremental
```

- `crawl`：先发现配置的前 stock_count 家公司。发现单元独立留存每页与每次尝试，达到 N 家或稳定 total 证明源耗尽才成功；发现失败不改股票列表、不启动财务。最后一页超过 N 的原体仍完整保存，只将选中的前 N 家用于当次财务计划。
- `incremental`：读取全部本地 stock_list，不重新发现股票，不受 stock_count 截断。crawl 更新列表采用 upsert，既有其他公司保留；因此其后 incremental 范围可能大于上次 crawl 的 N 家，运行前检查本地列表与配置报表。
- 两者使用同一财务协调器，逐公司/报表形成单元，按照 recent_periods 回扫最近 N 个不同报告期，保留其中全部修订。原始页全部留存后再筛选报告期；水位只作成功单元的单调记录，不把公告日期用于过滤报告期。
- 财务单元原子提交兼容 raw、版本、观察、当前投影、水位及成功状态；失败保留原体与审计，业务不留部分提交。不会自动运行 clean/features；在 2B-3 时它们仍使用阶段一兼容表；本分支的 2C 已切换 CLI 消费，见 [2C 指南](2c-operations.md)，采集本身仍不自动清洗/计算。

## 退出与汇总

采集完成打印 JSON，包含 run_id、status、failed_units、total、success、failed、incomplete。

| 情况 | run/结果 | 退出码 |
|---|---|---|
| 非空财务计划全部成功（含合法空响应） | success | 0 |
| 部分/全部财务单元失败或 incomplete | partial_failure / failed | 1 |
| 股票发现为空或失败 | failed，按一个发现前置单元计数，不造财务单元 | 1 |
| 本地股票列表为空、财务计划 0 单元 | failed / no_units，failed_units=0 | 1 |
| 参数、配置类型、范围或路径预检非法 | 请求前拒绝，不初始化 DB、不创建 run | 2 |
| 运行异常、中断、提交失败 | failed 返回或未完成状态保留供对账 | 1 |

failed_units 按失败/未完成单元计，不按 HTTP 尝试或页数计。发现成功不参与财务成功率，不能将全部财务失败改成部分成功。进程强制终止可能无法返回退出码；running 状态供 audit-raw 诊断，不自动修复或把水位补成成功。

## 配置与存储

| 项 | 规则 |
|---|---|
| database.path | 持久 SQLite 文件；相对启动目录，不能是 :memory: |
| raw_store.root | null 默认已解析数据库同目录 raw/；显式相对路径也相对数据库目录，不能包含 DB 文件 |
| raw_store.max_body_bytes | 正整数，默认 33554432，原响应/解压上限 |
| crawl.stock_count | 正整数，crawl 的公司上限 |
| crawl.recent_periods | 正整数或 null（全部历史） |
| crawl.report_names | 已支持的三类财务报表映射或报告名列表，不接受未知报表 |
| crawl.max_pages_per_unit | 正整数，默认 1000；安全截断记 incomplete_scope，非成功 |
| crawler.page_size / timeout | 正整数；timeout 为秒 |
| crawler.max_retries | 正整数，包含首次请求的每页最多尝试数 |
| crawler.min_interval | 有限非负秒数，股票与财务共享限速器 |
| crawler.base_url / stock_list_url | http(s) 接口，不携带账号、query 或 fragment；业务参数由白名单构造 |

配置在 DB 初始化和 GET 前验证；类型校验排除 bool 冒充整数。有效配置会初始化库，旧库仍执行已验收的备份后迁移规则；备份失败则不迁移。未知草稿结构明确拒绝，勿跳过检查。首次真实运行应使用明确的小范围和独立库，先确认备份位置及原库保留；本批没有升级真实业务库。

证据目录沿用 2B-1 的 run/unit/page/attempt 组织，不跨批次覆盖复用身份。已收到的失败响应也留存，transport_error 无响应时可以没有文件。存储失败不会被普通 HTTP 重试吞掉；文件与 DB 不能跨系统原子提交，孤儿/temp 留给只读对账。默认数据、报告、日志、gzip 证据与原子临时文件由 .gitignore 排除；自定义日志、数据库和报告位置需保持在已有忽略目录中，提交前检查暂存清单。

## 只读对账

```bash
.venv/bin/python -m fin_feature.cli --config /path/config.yaml audit-raw --run-id RUN_ID --report-dir reports/raw-audit
.venv/bin/python -m fin_feature.cli --config /path/config.yaml audit-raw --all --report-dir reports/raw-audit
```

--run-id 与 --all 必须二选一。使用 SQLite mode=ro 与只读 RawStore，不初始化迁移、不补抓、不删除，不切 WAL 或创建缺失证据根。报告目录必须在证据根外；默认 data/audit，产生 TXT 与 JSON，含问题列表和计数。健康为 0，问题或读写失败为 1，选项/报告位置非法为 2。检查缺失、篡改、孤儿、临时文件、旧不足证据、未完成 attempt/run，以及财务和股票源信封与范围完整性。

对账健康只证明已保存证据满足当前规则，不证明财务口径可比、真实来源可靠或历史截至日可用；特征血缘切换、重放与 as_of 分别由 2C/2D/阶段 4 完成。
