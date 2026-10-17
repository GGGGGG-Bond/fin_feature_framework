# 0.2.0 交付与运行

这是 Python / SQLite / CLI 财务特征框架的流程交付版。主来源负责计算，第二来源单独核验；原体、版本、观察、清洗与计算输入可追溯。严格保留缺失和不可计算，不为了提高覆盖率补造口径或历史日期。整体验收证据见 [acceptance.md](acceptance.md)。

## 从干净 checkout 开始

推荐 Linux CPython 3.12。pyproject 声明 Python>=3.10；本轮实际验收只覆盖3.12.3，不承诺所有平台/版本都已测试。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -c requirements-tested.txt -e '.[dev]'
.venv/bin/python scripts/verify_offline.py --output-dir /tmp/new-fin-acceptance
```

requirements-tested.txt 是本轮 Linux3.12 已验证依赖快照，不是所有 Python/平台通用锁文件。省略约束可按 pyproject 解析依赖，但需重新测试。依赖首次安装需要软件包网络；后面的测试和三个演示不需要财务来源网络。若宿主缺少 ensurepip，可创建 --without-pip 的新 venv，再用已安装的 pip 的 --python 参数引导该隔离环境；不需要修改共享机器系统包。

安装后两种入口等价：fin-feature --help / python -m fin_feature --help。一键验收运行离线测试、人工主流程、人工修订流程、公开金额样例、构造双源差异、只读交付报告、重放及系统/公开时间查询；将预期的“不可比/未知”退出1与真正失败区分，任何不符预期的步骤终止。输出必须是不存在的新目录，不覆盖旧结果。

## 采集与当前结果

先复制 config.yaml 为本地配置并限制公司/报告期；股票发现上限 stock_count 与本地增量股票范围不同，具体见 [2B 操作说明](stage-2/2b-operations.md)。初次真实采集用明确的小范围，勿把示例默认值当全市场调度授权。

```bash
fin-feature --config config.local.yaml crawl
fin-feature --config config.local.yaml incremental
fin-feature --config config.local.yaml refresh
fin-feature --config config.local.yaml delivery-report --output-dir reports/new-delivery
```

crawl / incremental 共享留证协调器。采集失败退出1，非法配置退出2；失败单元不提交业务数据或水位，已有响应文件和审计保留。有限报告期回扫负责发现旧报告修订。refresh 将当前版本清洗和受影响公司全部保存报告期特征放在同一事务；失败退出2，当前版本切换时已在版本事务内立即失效旧输出，避免仍查询旧结果。也保留 clean / features 分步入口，详见 [2C](stage-2/2c-operations.md)。

真实采集是可选刷新，不是离线验收的前提。它的 run/unit/attempt、失败码、原体和批次状态与演示隔离。若网络失败，先查该批次及 audit-raw，不重跑全市场，不把离线成功当在线刷新成功。

## 报告与导出

```bash
fin-feature --config config.local.yaml delivery-report \
  --output-dir reports/new-delivery \
  --review-summary docs/stage-3/review-result-20261018.json
```

只读打开既有库，不初始化/迁移、不改 WAL 设置、特征或证据。输出必须在 raw 根以外，包含 summary.json / summary.md、features.csv、versions.json、inputs.json：

- 请求范围、每个运行/单元状态及失败码、来源和充分证据版本数。
- 各表缺失率、按监控字段槽去重的异常率、drop计数和质量诊断；原始行数与单元新增版本/观察计数。
- 计划特征分母=主来源当前公司/报告期×12，返回数、有值数、缺输出、不可计算原因；空范围覆盖率为null，不是100%。
- 当前版本和逐次观察的 snapshot、原体哈希/路径、行定位；特征输入版本、计算批次、定义及配置哈希。
- 当前清洗有效性、按当前代码复算的特征/输入一致性、原体文件只读对账。未知元信息导致的空值与损坏/缺输出分开报告。
- 可选外部双源报告的固定分母与文件哈希。这里只读取已提供快照，不重新核验其 API / PDF，不把外部结果混入主来源。

退出0表示当前范围输出与证据检查无发现，不表示所有特征有值或全部外部参考已查；1表示空范围、采集失败/未结算、输入不足、失效、缺输出或对账发现；2表示输入、库或导出错误。当前主来源无可信元信息的跨期增长仍是null，应查看 not_computable_reasons。

## 三种可离线复现的输入

```bash
python -m examples.offline_flow --output-dir /tmp/new-artificial
python -m examples.revision_flow --output-dir /tmp/new-revision
python -m examples.public_flow --output-dir /tmp/new-public
```

第一种完全人工；第二种人工修订与模拟UTC时间；第三种使用入Git的32项已核对公开金额（茅台、平安、万科，两年）。公开样例保存官方URL/PDF哈希/物理页/行列，运行前核对参考文件哈希与字段，保留万科负亏损。其 API 信封是重建 fixture，绝非原始供应商响应或本次网络采集；gzip和SQLite用于验证真实存储路径。2023数据是2024年报比较列，不能冒充2023首次披露旧版。原PDF及真实接口原体仍存远端被忽略目录，未入Git，公开样例离线运行不会重新下载PDF。

截至日双视角、日期可信边界、修订传播和重放限制见 [阶段4](stage-4.md)。构造双源样例 examples/reconciliation 展示一致/冲突/不可比/缺失；真实样本覆盖和真实修订案例见 [阶段3](stage-3/README.md)。现有 backtest 只分析后续盈利增长代理，没有价格/交易收益，不是投资收益回测。

## CI 与后续增强

提供 [GitLab CI 模板](../ci/gitlab-ci.example.yml)，待配置runner后启用，当前未执行远端CI。配好可用 runner 后另行启用；本轮已在102新虚拟环境和干净 checkout 执行相同验收命令。精细依赖调度、完整历史软件环境、更多公告人工核查和经验证的供应商修订映射属于后续数据质量/性能扩展，不在报告中伪装为已有能力。

## 受限真实接口冒烟

```bash
python scripts/verify_live.py --output-dir /tmp/new-live-smoke \
  --secucode 600519.SH --start 2024-09-30 --end 2024-12-31
```

仅一家SH/SZ公司、最多两个相邻季度、三张表/每表最多一页、最多3次主请求、2秒间隔、20秒超时、不重试。输出不存在的新库和原体目录，不接触原业务库。实际本轮3单元成功，24条特征、8有值、重放24/24、报告与audit退出0。网络错误单独返回失败并保留证据，不扩大请求范围；这不是11个联网测试全部通过。CLI的默认50家配置无需用来验收本条流程。
