# 截至日查询与修订传播

> 本文保留阶段规则与历史交付记录；当前完成度及未完成项以[当前进展](progress.md)为准。

阶段4基于已保存的 statement_version / statement_observation 实施，无新增迁移。默认主来源仍为东方财富，第二来源只用于核验。公开时间和系统观察时间是两个查询视角，不能互相替代。

## 查询

```bash
python -m fin_feature --config config.yaml as-of --as-of 2025-05-15 --view system --secucode 000001.SZ --features
python -m fin_feature --config config.yaml as-of --as-of 2025-05-15T16:00:00+08:00 --view system
python -m fin_feature --config config.yaml as-of --as-of 2025-05-15 --view public
```

system 接受带时区 ISO 时间戳，日期表示 UTC 日末。只从截至时刻已经收到且有一致 UTC 快照时间的观察中选择；沿用阶段一公告日、entry_time、摘要的确定选值规则，旧公告后到不降级。不会按今日 is_superseded 倒推历史。A→B→A 由每次观察各自的公告信息恢复，不使用版本最初观察时间代替后续观察。旧库无时区或无快照时间不能推断，返回诊断。

public 只接受 ISO 日期，表示公告日末，不保证盘中可得。调用方/可信映射必须对该观察明确声明 revision_type=initial（使用 notice_date），或 revision/error_correction（使用 revision_notice_date）。unknown、非法日期、缺修订日期均排除；不能借首次公告日或采集时间回填。当前生产协调器尚无已验证的修订公告映射，仍写 unknown，因此真实样本可能全部返回未知，这是能力边界。输入日期声明本身不等于系统自动核验了官方公告内容。

返回选中的版本ID、观察ID、实际观察时间、所选公告日期、哈希、证据充分性和逐组诊断；缺旧版本返回 no_saved_eligible_version，无法补造此前内容。已有候选但存在未知公开时间，也保留 unknown_public_time，不把旧候选称为完整市场历史。

--features 在内存中按同一套清洗规则和特征函数计算，输出业务值、空值原因与版本血缘。clean_ref 是 virtual/in-memory 标识，未持久化，不能交给 replay 当计算日志ID。未知跨期口径继续阻断同比/环比。命令只读打开既有数据库，不迁移、不写当前投影或计算日志。退出0表示有选择且无历史选择诊断；1表示范围空或选择诊断；2表示无效输入/库/内容损坏。特征空值单独查看 not_computable_reason，不能由退出0推断所有特征可计算。

## 修订刷新

```bash
python -m fin_feature --config config.yaml incremental
python -m fin_feature --config config.yaml refresh
```

增量仍采用已验收的有限报告期回扫，而非以公告水位裁掉旧报告。完整单元才提交 raw、版本、观察与水位；重试/失败原有事务语义不变。修订切换当前版本时，在同一事务立即删除对应当前 clean 投影/选择，以及该公司全部 stage1-v1 特征；历史 clean_statement、feature_compute_log 和其他定义/legacy 特征保留。失败事务会连失效操作一起回滚。

refresh 用一个外层事务完成清洗和计算。为避免漏掉同比及累计差分的多段依赖，按受影响公司保守重算全部已保存报告期，不做复杂的精细依赖调度。报告 changed_companies、recomputed_features 和清洗统计。无变化且特征齐全时不追加日志；清洗后进程中断、缺特征或配置变化也能恢复。计算或真实 commit 失败时整批回滚，不能留下部分新清洗/旧特征组合。量大后的精细依赖性能优化后置。

## 可复现演示与验证

```bash
python -m examples.revision_flow --output-dir /tmp/new-revision-demo
```

必须使用不存在的新目录。两个批次、合计六个财务单元使用明确人工数据和模拟 UTC 时间，无网络请求；走真实 SQLite、gzip、协调器、刷新与重放。2025Q1 毛利率从0.4修订为0.8，截至日查询分别返回旧/新值；48条不可变结果全部重放一致，各批证据对账0。生产映射保持 unknown，公开时间查询不冒充已证实历史。首次公开日/修订日的显式声明能力由 tests/test_asof.py 的受控真实临时文件用例另行覆盖。

测试还覆盖比较期修订导致同比/跨年环比数值变化（受控 fixture 显式声明可比）、drop 传播、其他公司不重算、legacy保护、立即失效与事务回滚、旧结果重放、嵌套事务、无变化幂等、时间未知拒选及历史哈希损坏拒绝。不是恢复完整历史代码环境或证明真实供应商修订时间可靠。
