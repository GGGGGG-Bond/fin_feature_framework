# 阶段 1 数据字典

版本：stage1-v1。本表定义运行时标准语义；本阶段可保留 raw/clean 表的原始计量以兼容现有库。所有转换在唯一标准化入口进行，每次装载只对原计量输入转换一次，不能对已标准化对象再次除以 100。清洗阈值使用本表标准单位，流程先标准化再判断；落库保留原计量及处理状态，不能把新标准数值写入仍声明原计量的旧列。

## 身份与语义元信息

| 字段 | 定义与约束 |
|---|---|
| secucode | 来源证券标识，如 600519.SH；保留原值，映射需显式，不能擅改后缀 |
| report_date | 规范日期 YYYY-MM-DD；只支持合法季度末、半年末、年末；不以公告日代替 |
| notice_date | 该来源记录公告日；未知为空，不补成报告期或抓取日 |
| observed_at | 本系统实际观察时间；与公告日分别记录 |
| source_table / record_ref | 数据源、表类型及原输入标识；阶段 1 使用已有 raw 主键与内容摘要引用，不声称拥有完整历史 |
| currency / amount_unit | 币种与金额计量；元、万元等须由来源证据确定，未知不能跨来源比对或任意转换 |
| scope | consolidated / parent_only / unknown；报告期类型不等于合并范围 |
| period_basis | flow_ytd / flow_single_quarter / stock_end / unknown；分别表示累计流量、单季流量、期末余额、未知 |
| industry_type | industrial / financial / unknown；金融企业不套用未经定义的工业企业毛利和流动分类 |
| comparison_basis_id / alignment | 可比口径标识及依据；跨期修订对齐未知时为 unknown，不能仅因同一公司或公告日期相同认定可比 |
| mapping_version / evidence_ref | 来源映射版本及字段单位、类型定义的文档或样本依据 |

标准化层负责从来源字段和已核验来源映射构造这些信息。来源原码必须保留；不能套用 Tushare 代码含义解释东方财富 REPORT_TYPE_CODE 或 DATA_STATE。无法确认的元信息标 unknown。只有来源明确声明可比、已核对报表比较数字或受控 fixture 明确标注同口径时，才能标为跨期可比。

## 核心数值字段

FI=主要财务指标，IS=利润表，BS=资产负债表。字段映射为候选入口，不替代单位和口径证据。

| 标准字段 | 中文定义 | 类型/单位 | 候选原字段 | 权威来源 |
|---|---|---|---|---|
| total_operate_income | 营业总收入；不是随意替换的营业收入 | 累计流量，统一金额单位 | TOTAL_OPERATE_INCOME | IS；FI 仅可比回退 |
| parent_netprofit | 归属于母公司股东的净利润 | 累计流量，同营收单位 | PARENT_NETPROFIT | IS；FI 仅可比回退 |
| operate_cost | 与营业收入口径对应的营业成本 | 累计流量，金额 | OPERATE_COST | IS；本阶段不默认用于自算毛利率 |
| total_assets | 总资产期末余额 | 时点金额 | TOTAL_ASSETS | BS |
| total_liabilities | 总负债期末余额 | 时点金额 | TOTAL_LIABILITIES | BS |
| total_equity | 所有者权益合计，含少数股东权益的口径需确认 | 时点金额 | TOTAL_EQUITY | BS；不能用归母权益代替 |
| weightavg_roe | 同一来源定义的累计加权平均 ROE | 标准输入小数，0.12=12% | WEIGHTAVG_ROE | FI；原值百分数只转换一次 |
| gross_margin | 同一来源定义的累计销售毛利率 | 标准输入小数 | XSMLL | FI；金融行业先标不适用 |
| debt_asset_ratio | 总负债/总资产 | 标准输入小数 | TOTAL_LIABILITIES、TOTAL_ASSETS | BS 自算；原 DEBT_ASSET_RATIO 只对照 |
| current_ratio | 流动资产/流动负债 | 倍数，2.5=2.5 倍 | CURRENT_RATIO 或经核验的流动余额字段 | 仅单位与定义已验证的 BS 输入 |
| basic_eps / bps | 基本每股收益/每股净资产 | 金额/股；负值不自动视为错误 | BASIC_EPS、BPS | FI；辅助字段 |
| provider_*_growth | 来源提供的同比/环比指标 | 原百分值和原标签保留 | YSTZ、SJLTZ、YSHZ、SJLHZ | 仅诊断；不默认等同自算特征 |

毛利率不使用归母净利润自算；总负债缺失时不能用资产减未经确认的权益替代。流动余额字段在当前 BS 探测中未返回，阶段 1 不新增猜测字段。CURRENT_RATIO 单位未知时保留原值、输出 unknown_unit；单位验证为 percent_ratio 时除以 100，验证为 times 时原样使用。

## 同期比值的可算条件

同一原始记录中，经过验证的共享计量和报表范围可支持无量纲比值，即使尚不能换算为人民币元；结果必须保留来源原币种和范围标签，不能宣称是已确认合并口径。共享计量/范围也未知时拒算。不同原记录拼接的分子和分母要求明确一致的币种、单位、范围及版本口径，不能凭同公司同日期认定可比。跨期计算采用更严格的 comparison_basis 规则，不继承同期比值的宽限。

## 有效值与清洗

合法数字包括有限浮点数及可严格转换的数字字符串。null、空白、明确缺失标记、NaN、Inf 标为缺失或无效；布尔值不作为金额。异常处理记录原值和原因。

亏损、负 ROE、负权益或负每股净资产不因负号自动清除。负债/资产可超过 1，不以 100% 上限认定错误。资产与负债为负、非法日期和明确单位错误应标记异常；具体阈值由经过校验的配置决定。YAML 和 rules.py 的默认阈值统一采用标准单位：例如 gross_margin 原百分阈值 [-100,100] 对应标准 [-1,1]，weightavg_roe 原 [-200,200] 对应 [-2,2]。配置版本及兼容变化写入运行摘要；未知单位字段跳过数值阈值并诊断，不能靠阈值猜单位。默认负债率不得保留错误的 100% 硬上限。

缺失策略按表的实际字段应用，不能因某表不含另一张表的核心字段而整条丢弃。零填充仅用于显式配置且业务含义允许的字段；身份、公告时间、用于分母的金额和未知单位不得自动补零。

## 资料依据与能力边界

实际原字段依据项目 field_mapping.py 及当天小样本探测；[AKShare 财务数据说明](https://akshare.akfamily.xyz/data/stock/stock.html)可作为字段单位核验入口。[Tushare 利润表说明](https://tushare.pro/document/2?doc_id=33)展示了合并、单季、调整前后等不同类型，仅用于提醒模型必须区分，不能用于解释另一来源的原码。

实现 agent 应把新增单位或代码映射的证据链接、样本范围与结果写入 progress 和映射测试。未知信息不得通过 fixture 的已知口径倒推为真实来源已经验证。
