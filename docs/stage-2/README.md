# 阶段 2 实现契约

契约版本：stage2-v1。日期：2026-10-16。源码审阅基准：`181c4b6`（fix/stage2-2a-rereview 分支）。本契约经三轮独立审阅（R1-R7、A1-A6）修订，当前为复审修复后版本。

## 开工前阅读与交付

按顺序阅读 [数据模型与 DDL](data-model.md)、[迁移规范](migration.md)、[验收测试矩阵](test-matrix.md)。这三份文档共同构成阶段 2A 的验收依据。

实现 agent 从最新 origin/main 建立独立分支。首批 2A 仅交付数据模型与迁移框架，供独立审阅通过后再继续 2B-2D。

## 阶段 2 范围

阶段 2 解决阶段 1 留下的核心缺口：
- 采集批次（ingest_run）跟踪
- 原始响应留存（raw_snapshot）
- 版本与血缘（statement_version + statement_observation）
- 安全迁移（schema_version + 迁移框架）
- 事务所有权（unit_of_work 原子单元）

## 验收标准（5 条）

1. 任意示例特征能追至准确原始输入（2C+2D）
2. 相同数据及定义可离线重算得到相同业务值（2D）
3. 同一日期的内容修订不会覆盖证据（2A）
4. 失败批次不会伪装成成功（2A）
5. 旧数据迁移可验证且原库保留（2A）

首批 2A 覆盖验收标准 3/4/5；验收标准 1/2 在 2C/2D 完成后覆盖。

## 阶段边界（不实现）

- as_of 时间旅行查询（阶段 4）
- 第二来源核验（阶段 3）
- 完整修订传播重算（阶段 4）
- 不删除旧 raw_*/clean_*/features 表，不强制旧数据迁移到新模型

## 子批次划分

| 批次 | 范围 | 审阅 |
|---|---|---|
| **2A（首批）** | 数据模型与迁移框架 | 独立审阅，通过后再继续 |
| 2B | 采集层改造（raw_store、fetch_all_with_raw、CLI 集成） | 待 2A 通过 |
| 2C | 清洗与特征层改造（pipeline 从 statement_version 读取、填充 record_ref） | 待 2B 通过 |
| 2D | 离线重放（replay.py、CLI replay 子命令） | 待 2C 通过 |

## R1-R7 复审修复摘要

| 编号 | 问题 | 修复 |
|---|---|---|
| R1 [P1] | 事务所有权断裂：水位丢失、log_clean_action 提前提交 | 移除全部写入方法的 `with self.conn`，新增 `unit_of_work()` 上下文管理器；增量单元原子提交 raw+水位 |
| R3 [P1] | 同内容重复观察的新 snapshot 未关联 | 新增 `statement_observation` 表，`create_statement_version` 在同事务写入观察关联 |
| R4-a [P1] | 哈希写 unknown currency，INSERT 写 CNY | 迁移 currency 统一 unknown，哈希与 INSERT 使用同一规范化对象 |
| R4-b [P1] | 手写标准化绕过非有限数检查 | 迁移改用 `standardize_record(unit_map=FIELD_UNITS)`，复用统一入口的有限数检查 |
| R5 [P2] | 错来源/公司/表的 snapshot 仍标证据充分 | 新增 `_verify_snapshot_consistency` 校验 source/secucode/report 一致；schema 默认 0 + CHECK 约束 |
| R6 [P2] | 哈希只检查长度，不校验十六进制和内容一致性 | 新增 `hashing.py` 模块，Repository 内部计算哈希；schema CHECK 约束十六进制格式 |
| R7 [P2] | 迁移提交外层事务 | `_apply_migration` 检查 `in_transaction`，仅无外层事务时提交/回滚 |

## A1-A6 复审修复摘要（第三轮）

| 编号 | 问题 | 修复 |
|---|---|---|
| A1 [P1] | 嵌套 unit_of_work 无条件提交/回滚外层事务 | 最外层 `BEGIN` 自有事务，嵌套用独立 `SAVEPOINT`，内层只管理自己范围 |
| A2 [P1] | 旧 v2 草稿库静默接受，首次观察写入报缺表 | 新增 v3 迁移补齐表/列/索引；含数据的不兼容草稿明确拒绝 |
| A3 [P1] | 后续公告/修订元信息被丢弃 | `statement_observation` 加观察元信息列；版本行保留首次披露 |
| A4 [P2] | 未检查 run 一致性 | 校验快照 run 与版本 run 一致；`save_raw_snapshot` 校验来源 |
| A5 [P2] | GLOB 只限制首字符 | CHECK 改为 `NOT GLOB '*[^0-9a-f]*'` 全串校验 |
| A6 [P2] | 100 与 100.0 生成两个内容版本 | `normalize_numeric` 统一数值表示，存储与哈希共用 |

## 完成条件

- 全量测试通过（257 项：193 offline + 11 online + 53 复审修复）
- 迁移可验证（`verify_migration` 返回完整指标；旧草稿库升级或明确拒绝）
- 事务所有权明确（`unit_of_work` 原子单元，嵌套安全）
- 哈希可从存储字段重算；数值表示规范化
- 证据一致性在 Repository 层和 schema 层双重保障；哈希格式 schema 全串校验
