"""Self-contained HTML from executed results; no CDN, server or financial calls."""
from html import escape
from pathlib import Path
from fin_feature.demo_report import LABELS, display_value


def write_showcase(output, summary, features, comparisons, backtest, complete):
    output = Path(output)
    names = {c['secucode']: c['name'] for c in summary['companies']}
    e = lambda value: escape(str(value))
    def percent(value): return '缺失' if value is None else f'{value:.2%}'
    def amount(value): return '缺失' if value is None else f'{float(value):,.2f}'
    def options(values): return ''.join(f'<option value="{e(value)}">{e(label)}</option>' for value, label in values)
    company_options = options((code, f'{name} {code}') for code, name in names.items())
    periods = sorted({r['report_date'] for r in features}, reverse=True)
    selector = '<label>公司 <select id="company"><option value="">全部8家公司</option>'+company_options+'</select></label> '
    selector += '<label>报告期 <select id="period"><option value="">全部8个季度</option>'+options((p,p) for p in periods)+'</select></label>'
    rows = []
    for row in comparisons:
        doc = row.get('document')
        reference = (f'<a href="{e(doc["url"])}#page={doc["page"]}">原年报 PDF p.{doc["page"]}</a>'
                     f'<br><small>SHA-256 {e(doc["sha256"][:16])}…</small>') if doc else '未以异义科目补齐'
        later = row.get('later_comparative')
        if later:
            reference += (f'<br><a href="{e(row["later_document"]["url"])}#page={later["page"]}">后续年报比较列 p.{later["page"]}</a>'
                          f'<br>后续值 {amount(later["value"])}；双源均相符：{row["matches_later_comparative"]}')
        label = {'matches_report':'与2024年报一致','differs_from_report':'与2024年报不同','missing_or_definition':'缺值 / 定义不齐','not_reviewed':'待核查'}[row['official_status']]
        rows.append(f'<tr data-company="{e(row["secucode"])}" data-period="{e(row["report_date"])}" data-status="{e(row["official_status"])}">'
            f'<td>{e(row["name"])}<br>{e(row["report_date"])}</td><td>{e(row["field"])}</td>'
            f'<td>{amount(row["eastmoney"])}</td><td>{amount(row["sina"])}</td><td>{amount(row["numeric_delta"])}</td>'
            f'<td>{amount(row["official_value"])}</td><td class="{ "warn" if row["official_status"] != "matches_report" else "ok"}">{label}</td><td>{reference}</td></tr>')
    feature_rows = ''.join(f'<tr data-company="{e(r["secucode"])}" data-period="{e(r["report_date"])}">'
        f'<td>{e(names[r["secucode"]])}</td><td>{e(r["report_date"])}</td><td>{e(LABELS[r["feature_name"]][0])}<br><small>{e(r["feature_name"])}</small></td>'
        f'<td>{e(display_value(r["feature_name"],r["feature_value"]))}</td><td>{e(r["not_computable_reason"] or "—")}</td></tr>' for r in features)
    groups = ''.join(f'<tr><td>G{r["group"]}</td><td>{r["size"]}</td><td>{e(", ".join(names[c] for c in r["members"]))}</td>'
                     f'<td>{percent(r["gross_return"])}</td><td>{percent(r["net_return"])}</td><td>{percent(r["excess_net_return"])}</td><td>{percent(r["max_drawdown"])}</td></tr>' for r in backtest['groups'])
    series = {}
    for point in backtest['nav']: series.setdefault(point['series'], []).append(point)
    values = [p['nav'] for points in series.values() for p in points]
    low, high = min(values), max(values)
    span = high-low or 1
    colors = ['#64748b','#007f86','#2563eb','#a855f7','#e67e22','#bf315c']
    paths, legend = [], []
    for (name, points), color in zip(series.items(), colors):
        coords = ' '.join(f'{45+i/(len(points)-1)*900:.1f},{260-(p["nav"]-low)/span*220:.1f}' for i,p in enumerate(points))
        paths.append(f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{coords}"><title>{e(name)}: {points[-1]["nav"]:.4f}</title></polyline>')
        legend.append(f'<span style="color:{color}">● {e(name)} {points[-1]["nav"]:.3f}</span>')
    chart = f'<svg role="img" aria-label="五组组合与沪深300ETF净值曲线" viewBox="0 0 980 310"><text x="5" y="35">{high:.2f}</text><text x="5" y="265">{low:.2f}</text><path d="M45 30 V270 H945" fill="none" stroke="#cbd5e1"/>'+''.join(paths)+f'<text x="45" y="300">2025-05-05</text><text x="830" y="300">2025-12-31</text></svg><p>{" · ".join(legend)}</p>'
    complete_rows = ''.join(f'<tr><td>{e(LABELS[r["feature_name"]][0])}</td><td>{e(display_value(r["feature_name"],r["feature_value"]))}</td></tr>' for r in complete['target_features'])
    statuses = summary['dual_statuses']
    text = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>八公司财务特征 · 双源核验 · 收益回测</title><style>
*{box-sizing:border-box}body{font:15px/1.65 system-ui,sans-serif;margin:0;background:#f3f6fa;color:#172b40}main{max-width:1420px;margin:auto;padding:24px}
header,section{background:white;border:1px solid #dde5ee;border-radius:14px;padding:26px;margin:18px 0}h1,h2{line-height:1.3}h1{font-size:32px}.eyebrow{color:#007f86;letter-spacing:.1em;font-size:12px}nav,a{color:#1766a8}.stats{display:flex;gap:12px;flex-wrap:wrap}.stat{flex:1;min-width:150px;background:#eff5fb;padding:16px;border-radius:8px}.stat strong{font-size:26px;display:block}.muted,small{color:#5d6f80}.warn{color:#a34216}.ok{color:#057060}select{padding:8px;border:1px solid #b8c8d7;border-radius:6px;background:white;margin:5px}table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}th,td{padding:10px 12px;text-align:left;border-bottom:1px solid #dfe7ef}th{background:#edf3fa;position:sticky;top:0}td{vertical-align:top}code,small{overflow-wrap:anywhere}.scroll{overflow:auto;max-height:620px}svg{width:100%;max-height:330px}nav a{margin-right:18px}summary{cursor:pointer}footer{padding:20px;color:#5d6f80}@media(max-width:600px){main{padding:8px}header,section{padding:16px}h1{font-size:25px}td,th{padding:8px;font-size:12px}}
</style><main><header><p class="eyebrow">FINANCIAL FEATURE FRAMEWORK / EXECUTED OFFLINE SHOWCASE</p>
<h1>从真实财务采样到可核验的特征与收益</h1>
<p>消费、制造、新能源、装备、地产、银行、保险、券商。已固定真实采集的选定字段，可离线重复运行；本次演示金融接口请求数为0。</p>
<nav><a href="#dual">双源与财报</a><a href="#features">多公司特征</a><a href="#returns">价格收益</a><a href="#complete">12项完整样例</a><a href="#evidence">证据与限制</a></nav>
'''
    text += f'<div class="stats"><div class="stat"><strong>8 × 8</strong>公司 × 季度 / 192份报表版本</div><div class="stat"><strong>{summary["features"]}</strong>特征结果 / {summary["non_null_features"]}有值</div><div class="stat"><strong>{summary["replay_matches"]}/{summary["replay_count"]}</strong>冻结输入重放一致</div><div class="stat"><strong>{len(comparisons)}</strong>双源字段位置 / {summary["dual_numeric_agreements"]}数值一致</div></div>'
    text += '<p class="muted">采集：2026-10-18；报表覆盖2023Q1—2024Q4。Git仅保存选定字段、原体摘要和来源信息；离线采集信封为重建，原供应商HTTP原体不在仓库。</p></header>'
    text += f'<section><h2>浏览范围</h2>{selector}<p id="visible"></p></section>'
    text += f'<section id="dual"><h2>双源数值对照与正式年报核验</h2><p>{statuses.get("matches_report",0)}处与2024年报一致；{statuses.get("differs_from_report",0)}处不同；{statuses.get("missing_or_definition",0)}处缺值或定义不齐。绝对容差0.01元，无相对容差放大。</p>'
    text += '<p class="warn">双源数值相同不证明上游独立，也不证明单位、币种、范围与修订时点完全一致。金融业营业收入/成本不强行映射到异义科目；第二来源不进入主数据计算。</p><label>核验状态 <select id="status"><option value="">全部</option><option value="matches_report">与年报一致</option><option value="differs_from_report">与年报不同</option><option value="missing_or_definition">缺值/定义不齐</option></select></label>'
    text += '<p>三一2024年三个字段：双源均与后续2025年报比较列相符，却与最初2024年报不同。差异保留，不能扩大容差或覆盖原年报来“消除”。</p><div class="scroll"><table id="dual-table"><thead><tr><th>公司 / 期间</th><th>字段</th><th>东方财富（元）</th><th>新浪（元）</th><th>东财−新浪</th><th>2024年报（元）</th><th>核验</th><th>出处 / 后续比较列</th></tr></thead><tbody>'+''.join(rows)+'</tbody></table></div><p><a href="dual-source.csv">完整对照CSV</a> · <a href="dual-source.json">原始语义、来源请求和PDF摘要</a></p></section>'
    text += '<section id="features"><h2>八公司 × 八季度 × 十二项特征</h2><p>有值与拒算原因同时展示。未知跨期口径、行业不适用及比较期缺失保留空值；重放一致包含空值及原因一致。</p><div class="scroll"><table id="features-table"><thead><tr><th>公司</th><th>期间</th><th>特征</th><th>结果</th><th>空值原因</th></tr></thead><tbody>'+feature_rows+'</tbody></table></div><p><a href="captured/report/features.csv">完整CSV</a> · <a href="captured/report/summary.md">质量与证据报告</a> · <a href="captured/replay.json">重放明细</a></p></section>'
    text += '<section id="returns"><h2>真实价格收益：按资产负债率升序分五组</h2><p>独立使用2024正式年报中的资产/负债，经正常版本、清洗、特征链计算信号；公开可用时间保守声明为2025-04-30。2025-05-05收盘后形成组合，下一交易日5月6日前复权开盘价入场，12月31日前复权收盘价出场；等初始权重、固定持仓、单边10bps成本。</p><p class="warn">八家手选跨行业公司，组大小2/2/2/1/1；这验证收益计算流程，不构成有效策略证据。前复权口径来自供应商，不另建现金分红再投资账；缺价不删样本或重排，整组收益置缺失。</p>'
    text += chart+'<div class="scroll"><table><thead><tr><th>组</th><th>数量</th><th>成员</th><th>毛收益</th><th>净收益</th><th>相对基准净收益</th><th>最大回撤</th></tr></thead><tbody>'+groups+'</tbody></table></div>'
    text += f'<p>基准：沪深300ETF（510300.SH），同样计成本，净收益 {percent(backtest["benchmark_metrics"]["net_return"])}。高低组净收益差 {percent(backtest["high_minus_low_net_return"])}，该差值不是已实现多空策略收益。</p>'
    text += f'<p>前视检查：若误用当前供应商记录的2026公告日，八家公司均在2025形成日被拒绝（{summary["historical_current_provider_rejected"]}/8）。<a href="lookahead-rejections.json">检查结果</a>。报告公开日期声明也不等于历史系统已知或原始字节归档证明。</p><p><a href="backtest/summary.md">收益统计</a> · <a href="backtest/groups.csv">分组CSV</a> · <a href="backtest/members.csv">成员价格/成本CSV</a> · <a href="backtest/nav.csv">每日净值CSV</a> · <a href="signals.json">信号与公开时间依据</a></p></section>'
    text += '<section id="complete"><h2>十二项特征完整样例：贵州茅台 / 2024年末</h2><p>另用三份正式财报的27项已核对事实，显式绑定口径，完整演示12/12有值；不将这一声明套用到上面的真实抓取数据。</p><table><thead><tr><th>特征</th><th>结果</th></tr></thead><tbody>'+complete_rows+'</tbody></table><p><a href="required-features/report/features.csv">完整样例CSV</a> · <a href="required-features/fixture-provenance.json">事实、来源及限制</a></p></section>'
    text += '<section id="evidence"><h2>来源、证据与边界</h2><p>真实采样：主来源24次财务请求、双源32次请求、行情9次请求。原体摘要标识采集母证据；离线重建信封会生成自己的快照与摘要，二者身份不可互换。固定字段文件以规范JSON内容哈希绑定，跨平台换行不会改变身份。</p><p><a href="fixture-provenance.json">输入清单、84项正式财报事实与PDF位置</a> · <a href="summary.json">本次摘要</a> · <a href="report-signals/report/inputs.json">回测信号输入血缘</a></p><ul>'+''.join('<li>'+e(x)+'</li>' for x in summary['limitations'])+'</ul></section><footer>所有表格、图表来自本次执行结果；页面无需服务端、外部脚本或金融网络。</footer></main>'
    text += '''<script>
function filter(){let company=document.getElementById('company').value,period=document.getElementById('period').value,status=document.getElementById('status').value;let counts=[];for(let id of ['dual-table','features-table']){let count=0;for(let row of document.querySelectorAll('#'+id+' tbody tr')){let show=(!company||row.dataset.company===company)&&(!period||row.dataset.period===period)&&(id!=='dual-table'||!status||row.dataset.status===status);row.hidden=!show;if(show)count++;}counts.push(count);}document.getElementById('visible').textContent='当前范围：双源 '+counts[0]+' 行；特征 '+counts[1]+' 行。双源仅覆盖两个年末，季度筛选可为0。';}for(let id of ['company','period','status'])document.getElementById(id).addEventListener('change',filter);filter();
</script></html>'''
    (output / 'index.html').write_text(text, encoding='utf-8')
    return output / 'index.html'
