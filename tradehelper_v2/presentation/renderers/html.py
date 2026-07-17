"""Semantic, self-contained HTML renderer for ReportDocument."""
from __future__ import annotations

from html import escape
from tradehelper_v2.contracts import ReportBlockKind


_COLORS = ("#1769aa", "#15803d", "#b45309", "#b91c1c", "#6d28d9")


def _chart_svg(chart):
    point_sets = [points for _, points in chart.series if points]
    if not point_sets:
        return f'<div class="empty">{escape(chart.empty_state or "暂无可绘制数据")}</div>'
    labels = []
    for points in point_sets:
        for label, _ in points:
            if label not in labels:
                labels.append(label)
    values = [float(value) for points in point_sets for _, value in points] + [float(value) for _, value in chart.baseline]
    low, high = min(values), max(values)
    if low == high:
        low -= 1.0; high += 1.0
    left, right, top, bottom = 58.0, 710.0, 20.0, 220.0
    x_of = lambda label: left if len(labels) == 1 else left + labels.index(label) * (right - left) / (len(labels) - 1)
    y_of = lambda value: bottom - (float(value) - low) * (bottom - top) / (high - low)
    content = [
        '<svg class="chart" viewBox="0 0 740 270" role="img" aria-label="'+escape(chart.title)+'">',
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{bottom}" class="axis"/>',
        f'<line x1="{left}" y1="{bottom}" x2="{right}" y2="{bottom}" class="axis"/>',
        f'<text x="8" y="{top + 5}" class="tick">{high:.3g}</text>',
        f'<text x="8" y="{bottom + 5}" class="tick">{low:.3g}</text>',
    ]
    if chart.baseline:
        baseline = " ".join(f"{x_of(label):.2f},{y_of(value):.2f}" for label, value in chart.baseline if label in labels)
        content.append(f'<polyline points="{baseline}" class="baseline"/>')
    for index, (name, points) in enumerate(chart.series):
        color = _COLORS[index % len(_COLORS)]
        path = " ".join(f"{x_of(label):.2f},{y_of(value):.2f}" for label, value in points)
        content.append(f'<polyline points="{path}" fill="none" stroke="{color}" stroke-width="2.5"/>')
        content.append(f'<text x="{left + index * 130}" y="252" fill="{color}" class="legend">{escape(name)}</text>')
    content.append(f'<text x="{left}" y="238" class="tick">{escape(labels[0])}</text>')
    if len(labels) > 1:
        content.append(f'<text x="{right}" y="238" text-anchor="end" class="tick">{escape(labels[-1])}</text>')
    content.append('</svg>')
    return "".join(content)


def _table(table):
    rows = "".join(
        "<tr>" + "".join(f"<td>{escape(cell)}</td>" for cell in row.cells) + "</tr>"
        for row in table.rows
    )
    if not rows:
        rows = f'<tr><td colspan="{len(table.columns)}" class="empty">{escape(table.empty_state or "暂无数据")}</td></tr>'
    note = f'<p class="interpretation">{escape(table.interpretation)}</p>' if table.interpretation else ""
    return (
        f'<div class="table-wrap"><table><caption>{escape(table.title)} · {len(table.rows)} 行</caption>'
        '<thead><tr>' + "".join(f'<th scope="col">{escape(column)}</th>' for column in table.columns) +
        f'</tr></thead><tbody>{rows}</tbody></table></div>{note}'
    )


def render_html(document):
    sections = []
    for section in document.sections:
        blocks = []
        for block in section.blocks:
            if block.kind is ReportBlockKind.TEXT:
                blocks.append(f'<p>{escape(str(block.payload))}</p>')
            elif block.kind is ReportBlockKind.CALLOUT:
                blocks.append(f'<div class="callout">{escape(str(block.payload))}</div>')
            elif block.kind is ReportBlockKind.TABLE:
                blocks.append(_table(block.payload))
            elif block.kind is ReportBlockKind.CHART:
                chart = block.payload
                blocks.append(f'<figure><figcaption>{escape(chart.title)} · 样本 {chart.sample_count}</figcaption>{_chart_svg(chart)}<p class="interpretation">{escape(chart.interpretation)}</p></figure>')
            elif block.kind is ReportBlockKind.DIVIDER:
                blocks.append('<hr>')
            else:
                blocks.append(f'<p>{escape(str(block.payload))}</p>')
        sections.append(f'<section id="{escape(section.section_id)}"><h2>{escape(section.title)}</h2><p class="purpose">{escape(section.purpose)}</p>{"".join(blocks)}</section>')
    style = """
    :root{color-scheme:light;--ink:#162033;--muted:#5d6878;--line:#d9dee7;--panel:#f6f8fb;--accent:#1769aa}
    *{box-sizing:border-box}body{font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;margin:0;color:var(--ink);background:#fff;line-height:1.55;letter-spacing:0}
    main{max-width:1180px;margin:auto;padding:24px}header{border-bottom:2px solid var(--ink);padding-bottom:16px}h1{font-size:28px;margin:0 0 10px}h2{font-size:20px;margin:0 0 4px}section{padding:22px 0;border-bottom:1px solid var(--line)}.purpose,.interpretation,.meta{color:var(--muted);font-size:14px}.callout{border-left:4px solid var(--accent);background:#eef6fd;padding:14px 16px;font-weight:600;margin:12px 0}.table-wrap{width:100%;overflow-x:auto;margin:12px 0}
    table{width:100%;border-collapse:collapse;min-width:680px;font-size:13px}caption{text-align:left;font-weight:700;padding:8px 0}th,td{border:1px solid var(--line);padding:8px 10px;text-align:left;vertical-align:top;overflow-wrap:anywhere}th{background:var(--panel);position:sticky;top:0}.empty{color:var(--muted);text-align:center}figure{margin:14px 0;border:1px solid var(--line);padding:12px}figcaption{font-weight:700}.chart{width:100%;height:auto;min-height:220px}.axis{stroke:#64748b;stroke-width:1}.baseline{fill:none;stroke:#94a3b8;stroke-width:1.5;stroke-dasharray:5 4}.tick,.legend{font-size:11px;fill:#475569}
    @media(max-width:480px){main{padding:12px}h1{font-size:22px}h2{font-size:18px}.callout{padding:10px 12px}table{min-width:620px}.chart{min-height:180px}}
    """
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{escape(document.title)}</title><style>{style}</style></head><body><main><header><h1>{escape(document.title)}</h1><p>{escape(document.summary)}</p><p class="meta">数据时点：{escape(document.as_of.isoformat())}</p></header>{"".join(sections)}</main></body></html>'
    )
