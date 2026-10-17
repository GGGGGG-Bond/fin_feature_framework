# 2D 最小流程：先跑通，再集中验收

代码 2bf365b，分支 feat/stage2d-flow，基准 main 50ef8a6（MR!9）。本批按用户最新方向控制范围，不新增数据库表、迁移、依赖或历史 Git 环境管理。

## 一条命令演示

```bash
.venv/bin/python -m examples.offline_flow --output-dir data/offline-demo
```

输出目录必须不存在，拒绝覆盖已有业务目录。使用明确标注的人工构造数据、脚本响应，经过真实采集协调器、SQLite/gzip、版本清洗、特征、重放和只读对账。summary.json 为摘要，replay.json 为逐结果比较。已实跑：3 单元成功，24 条特征，24/24 重放一致，对账退出 0；没有网络或真实财务数据。换新目录可重复运行，不删除旧输出。

## 已有结果的只读重放

```bash
.venv/bin/python -m fin_feature.cli --config /path/config.yaml replay --result-id RESULT_ID
.venv/bin/python -m fin_feature.cli --config /path/config.yaml replay --compute-run-id COMPUTE_RUN_ID
```

ID 从 feature_compute_log 查询。两个选项互斥且必须选一。退出 0=业务值和不可计算原因一致，1=发现差异，2=输入缺失/不支持/损坏或读取失败。CLI 使用 mode=ro，不升级库、不写计算日志、不更新 features、不联网。

只读取保存的 config_snapshot.inputs、version_id、clean_ref 和清洗规则；修订或当前投影改变不影响旧输入选择。重建时检查内容哈希、清洗结果及字段依赖身份；普通计算与重放共用 evaluate_feature。比较采用 rel_tol/abs_tol=1e-12，None 必须同时匹配原因；计算时间与新批次 ID 不参与业务等价判断。

## 本版边界与后续集中验收

- 明确输出 saved-inputs/current-code；列出原/现代码标识，dirty/不可核验状态显式标记。不自动 checkout 原 commit 或恢复旧依赖环境，不将业务结果一致说成历史环境完全复原。
- 仅支持 2C 格式的计算日志；旧日志缺少冻结输入时拒绝，不猜测历史版本。
- 原体文件健康沿用 audit-raw；本版业务重放使用已保存的标准化版本，不补抓、不新增来源、不提升旧证据充分性。
- 后续集中验收补更多异常、规模/性能、完整历史代码环境和真实小样本。as_of、双源及生产运行仍单独推进；不提前扩展通用框架。

主流程测试 9 项：tests/test_replay.py；全量 606 passed、11 deselected。输入均为临时真实 SQLite/gzip 与脚本化 HTTP。当前是可演示流程版，待 MR 和集中验收。
