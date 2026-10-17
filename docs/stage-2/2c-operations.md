# 2C 版本清洗、计算与血缘查看

对应代码 `3b3c5d8` + `9f9a26d`。实现与离线自验证完成，待 MR；规则见 [契约](2c-contract.md)，当前进展见 [progress](../progress.md)。本批没有真实财务请求或真实数据库升级。

## 运行顺序与兼容

确认目标数据库、配置与备份后，在项目根目录运行：

```bash
.venv/bin/python -m fin_feature.cli --config /path/config.yaml clean
.venv/bin/python -m fin_feature.cli --config /path/config.yaml features
```

CLI clean 读取 eastmoney 唯一当前 statement_version，不从矛盾 raw 重新选值；没有版本不会静默回退。CLI features 只读版本清洗结果，先检查当前版本/配置/引用，过期时以 stale_clean 明确拒绝，须使用同一配置先重跑 clean。不存在的表类型、重复当前版本、非规范季度末或内容哈希不一致也拒绝。

兼容 clean 原数值列单位不变（如 ROE 16.75），标准化 sidecar 对应 0.1675；新适配器使用后者，不二次转换。直接 Python API 默认 legacy 以兼容旧调用，需明确 input_mode="versions" 才走新链路；禁止将两种单位的记录混入一次语义合并。旧 raw-only 写入未形成版本时不能靠新 CLI 自动补血缘。

clean 配置仍用原 cleaning 段。有效配置从固定默认规则副本解析，不继承旧批次或单位 fixture 的运行时覆盖；非法清洗配置在 CLI 初始化数据库前拒绝。未知单位不能用 zero 策略补成已知值；阈值超标只诊断，核心缺失 drop 保留不可变结果。财务公式和 stage1-v1 定义不变，未知跨期口径继续拒算。

## v5 与当前结果

v5 新增 clean_statement/clean_selection，三张 clean 表增加 record_ref/clean_ref。日常初始化发现已有库需升级，仍先生成一致备份再迁移；失败停止，不跳过 v4/v5 结构核验。升级仅建结构，旧 clean 引用保持 NULL，不猜测历史血缘；首次 clean 重建当前投影，先确认目标库与范围。真实迁移需按实际业务运行安排，不能把本批临时库测试当作已升级。

同版本、同有效规则的清洗结果复用；规则变化新建 clean_id，drop 也记录当前选择。当前选择变化会保守失效该公司所有报告期 stage1-v1 特征投影；旧不可变日志、legacy 和其他定义保留。features 计算在一个事务中保存日志和投影；重新计算产生新的 compute_run_id，旧日志不覆盖。code_commit 来自实际 Git HEAD，dirty 单列；安装环境无法获取 Git 时为 NULL。

## 查看计算及原始输入

```sql
SELECT compute_run_id, secucode, report_date, feature_name, feature_value,
       not_computable_reason, input_version_ids, code_commit, config_snapshot
FROM feature_compute_log ORDER BY computed_at DESC;

SELECT f.result_id, v.version_id, v.table_type, v.evidence_sufficient,
       o.snapshot_id, o.row_index, s.file_path, s.raw_content_hash
FROM feature_compute_log f
JOIN json_each(f.input_version_ids) i
JOIN statement_version v ON v.version_id = i.value
LEFT JOIN statement_observation o ON o.version_id = v.version_id
LEFT JOIN raw_snapshot s ON s.snapshot_id = o.snapshot_id
WHERE f.result_id = :result_id;
```

input_version_ids 是该特征依赖范围的版本集合；config_snapshot.inputs 进一步标注 report_date、字段、权威来源/回退候选/行业依据、clean_ref、缺失或 dropped、证据充分性。回退候选是选择诊断依据，不代表已作为数值回退。Q1 环比包含上年 Q4 和 Q3；H1 环比使用 Q1 本身，不加入无关上年 Q4。

snapshot 解压后应按 row_index（从 0 开始）定位原页，使用 RawStore.verify 核对原字节哈希/大小。无 snapshot 的迁移版本可计算合法当期值，但仍 evidence_sufficient=0，不能称为已取得原始响应。需要文件健康检查时使用 [audit-raw](2b-operations.md)；计算链本身不会补抓或把旧数据标成充分证据。

None 结果具有 not_computable_reason，例如 unknown_unit、incomparable_basis、missing_comparison_period、missing_input、missing_drop、non_positive_base、non_positive_comparison_base、not_applicable、invalid_value 或 calculation_error。有限数值的原因为空。legacy/其他定义占用同一投影键时保留原行，新计算只追加日志，写入计数因此可能少于日志数。

## 验证边界

597 项离线通过，新增 45 项用例覆盖 v5 升级、只转换一次、配置幂等、过期拒算、失败回滚、当前/同比/环比血缘及真实原体定位。C01—C15 映射见契约附录。2D replay 与历史 as_of 尚未实现，as_of_date 保持 NULL；双源核验仍是后续阶段。
