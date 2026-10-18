# 采集与原始证据测试矩阵

公开版保留生产代码对应的全部回归测试，本文件按行为导航，不分发内部逐轮审阅记录或依赖私有脚本的命令。

| 范围 | 主要覆盖 | 测试入口 |
|---|---|---|
| B01–B08 | 原体字节哈希、不可变发布、竞争、fsync/存储失败、损坏与路径边界 | [RawStore](../../tests/test_raw_store.py) |
| B09–B12 | 迁移、备份、索引与结构完整性、事务回滚 | [迁移](../../tests/test_ingestion_migration.py) |
| B13–B15 | 单元与尝试身份、最终页、充分证据、页信封与行数核对 | [单元](../../tests/test_ingestion_unit.py) |
| B16–B33 | HTTP尝试留证、分页、单元事务、版本/观察/当前投影/水位、接收UTC与提交失败 | [分页](../../tests/test_capture_pagination.py)、[协调器](../../tests/test_ingestion_coordinator.py) |
| B34–B46 | 两CLI入口、空范围、部分/全失败、退出码、股票发现、失败续跑与原体对账 | [CLI](../../tests/test_ingestion_cli.py)、[对账](../../tests/test_raw_audit.py) |

其他版本、清洗、特征、截至日、修订和重放测试见 tests/。每个用例使用受控输入或临时真实SQLite/gzip；标记requires_network的测试单独执行。

```bash
.venv/bin/python -m pytest tests/ -q -p no:cacheprovider -m 'not requires_network'
```

当前基础实现为770通过、11联网未执行。公开候选验证另见[验收记录](../acceptance.md)，不把历史测试数当作新的云端CI执行结果。
