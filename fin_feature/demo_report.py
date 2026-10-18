"""Dependency-free, local HTML overview of executed demo results."""
from html import escape
from pathlib import Path

LABELS = {
    'net_profit_margin': ('归母净利率', '比例'),
    'gross_margin_feat': ('销售毛利率', '比例'),
    'roe_change': ('ROE变化', '百分点'),
    'roe_change_rate': ('ROE变化率', '增长率'),
    'revenue_yoy': ('营业总收入同比', '增长率'),
    'revenue_qoq': ('单季营业总收入环比', '增长率'),
    'profit_yoy': ('归母净利润同比', '增长率'),
    'profit_qoq': ('单季归母净利润环比', '增长率'),
    'debt_asset_ratio_change': ('资产负债率变化', '百分点'),
    'current_ratio_change': ('流动比率变化', '倍数差'),
    'equity_ratio': ('权益比例', '比例'),
    'debt_asset_ratio': ('资产负债率', '比例'),
}


def display_value(name, value):
    if value is None or value == '':
        return '空值'
    value = float(value)
    unit = LABELS.get(name, ('', '数值'))[1]
    if unit in ('比例', '增长率'):
        return f'{value:.2%}'
    return f'{value:.4f} {unit}'


def write_overview(output, status):
    output = Path(output)
    sections = []
    for step in status['steps']:
        name, summary = step['name'], step.get('summary', {})
        content = [f'<h2>{escape(name)} · 退出码 {step["exit_code"]}</h2>']
        if step['exit_code']:
            content.append(f'<p class="warning">运行失败，查看 <a href="{name}.log">日志</a>。</p>')
        if summary:
            content.append('<p>特征结果数：{features}；有值：{non_null}；重放一致：{matches}/{count}。</p>'.format(
                features=summary.get('features', '见摘要'), non_null=summary.get('non_null_features', '见摘要'),
                matches=summary.get('replay_matches', 0), count=summary.get('replay_count', 0)))
            if name == 'interview':
                content.append('<p>正式财报27项固定事实，重建输入信封。目标：600519.SH / 2024-12-31，'
                               '12/12特征有值；其余期间可能因缺少比较期为空。</p>')
                content.append('<p>毛利率使用营业收入与营业成本；净利率、增速使用营业总收入。'
                               '流动比率来自披露的流动资产/流动负债，单位为倍数。</p>')
                content.append('<table><thead><tr><th>特征</th><th>结果</th><th>原始小数值</th></tr></thead><tbody>')
                for row in summary['target_features']:
                    feature, value = row['feature_name'], row['feature_value']
                    content.append(f'<tr><td>{escape(LABELS[feature][0])}<br><code>{escape(feature)}</code></td>'
                                   f'<td>{escape(display_value(feature, value))}</td><td>{value:.12g}</td></tr>')
                content.append('</tbody></table><h3>拒算规则示例（人工输入）</h3><ul>')
                for case in summary['boundary_cases']:
                    content.append(f'<li>{escape(case["case"])} → 空值：<code>{escape(case["reason"])}</code></li>')
                content.append('</ul>')
            elif name == 'public':
                content.append('<p>三家公司32项已核对正式财报事实；重建信封，不是本次网络采集。</p>')
            else:
                content.append('<p>人工构造样本，用于展示流程或修订行为。</p>')
            content.append(f'<p><a href="{name}/summary.json">运行摘要</a> · '
                           f'<a href="{name}/replay.json">重放明细</a> · <a href="{name}.log">日志</a></p>')
            if (output / name / 'report/features.csv').is_file():
                content.append(f'<p><a href="{name}/report/features.csv">完整特征CSV</a> · '
                               f'<a href="{name}/report/summary.md">质量与证据报告</a> · '
                               f'<a href="{name}/fixture-provenance.json">输入来源与限制</a></p>')
            limits = summary.get('limitations', [])
            if isinstance(limits, str):
                limits = [limits]
            if limits:
                content.append('<ul>' + ''.join(f'<li>{escape(item)}</li>' for item in limits) + '</ul>')
        sections.append('<section>' + '\n'.join(content) + '</section>')
    html = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>财务特征框架 · 演示结果</title>
<style>
body{font:16px/1.65 system-ui,sans-serif;background:#f4f6f8;color:#172b40;max-width:1080px;margin:36px auto;padding:0 20px}
header,section{background:white;border:1px solid #dce3e9;border-radius:12px;padding:24px;margin:20px 0}
h1,h2{line-height:1.3}a{color:#145db6}code{font-size:12px;overflow-wrap:anywhere}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}td,th{padding:12px;text-align:left;border-bottom:1px solid #dce3e9}
th{background:#edf3f9}.warning{color:#a33b17}p,li{overflow-wrap:anywhere}
@media(max-width:600px){body{padding:0 8px}header,section{padding:14px}td,th{padding:8px;font-size:13px}}
</style><header><p>FINANCIAL FEATURE FRAMEWORK</p><h1>财务数据 → 清洗 → 特征 → 核验</h1>
<p>本页由刚才执行的本地离线演示生成。金融接口请求数：0。无需服务端或外部资源即可打开。</p>
<p class="warning">重放一致包括空值与原因一致，不等于全部特征有值；示例不构成投资收益回测。</p></header>
'''
    target = output / 'index.html'
    target.write_text(html + '\n'.join(sections) + '</html>\n', encoding='utf-8')
    return target
