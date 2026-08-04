"""Semantic, self-contained HTML renderer for ReportDocument."""
from __future__ import annotations

from html import escape
import re

from contracts import ReportBlockKind
from presentation.formatting import format_datetime


_COLORS = ("#1769aa", "#15803d", "#b45309", "#b91c1c", "#6d28d9")
_LONG_TEXT_TABLES = frozenset((
    "portfolio_exit_signals", "portfolio_entry_signals", "portfolio_hold_signals",
    "portfolio_plans", "portfolio_research",
))


def _segments(value):
    return tuple(item.strip(" /") for item in re.split(r"[；\n]+", str(value)) if item.strip(" /"))


def _segment_list(value, *, ordered=False):
    items = _segments(value)
    if len(items) <= 1:
        return escape(str(value))
    tag = "ol" if ordered else "ul"
    return f'<{tag} class="cell-list">' + "".join(f"<li>{escape(item)}</li>" for item in items) + f"</{tag}>"


def _forecast_cell(value):
    matched = re.match(r"^(上涨|震荡|下跌)\s+([\d.]+)%；收益中位\s+([^；]+)；目标\s+(.+)$", str(value))
    if not matched:
        return _segment_list(value)
    direction, probability_text, median, target = matched.groups()
    probability = min(max(float(probability_text), 0.0), 100.0)
    css = {"上涨": "up", "震荡": "flat", "下跌": "down"}[direction]
    return (
        f'<div class="forecast-cell"><div><strong class="direction {css}">{direction}</strong>'
        f'<b>{escape(probability_text)}%</b></div><div class="probability-track">'
        f'<i class="{css}" style="width:{probability:.2f}%"></i></div>'
        f'<small>收益中位 {escape(median)}</small><small>目标 {escape(target)}</small></div>'
    )


def _cell_html(table, column, value):
    if table.table_id == "portfolio_forecasts" and column.startswith("未来"):
        return _forecast_cell(value)
    if (
        table.table_id in _LONG_TEXT_TABLES
        or table.table_id.startswith("stock_detail_")
        or column in {"下一步条件", "当前判断", "保守与激进"}
    ) and len(str(value)) >= 48:
        return _segment_list(value)
    return escape(str(value))


def _action_cards(table):
    cards = []
    for row in table.rows:
        stock, identity, action, next_step, profiles = row.cells
        action_name = action.split("；", 1)[0]
        css = "exit" if action_name in {"卖出", "减仓"} else "entry" if action_name in {"买入", "加仓"} else "hold"
        action_details = "；".join(_segments(action)[1:]) or action
        next_summary = (_segments(next_step) or ("查看详细操作报告",))[0]
        cards.append(
            f'<article class="action-card action-{css}"><header><div><h3>{escape(stock)}</h3>'
            f'<p>{escape(identity)}</p></div><span class="action-badge">{escape(action_name)}</span></header>'
            f'<div class="action-reason">{_segment_list(action_details)}</div>'
            f'<div class="action-next"><strong>下一条件</strong><span>{escape(next_summary)}</span></div></article>'
        )
    if not cards:
        return f'<div class="empty">{escape(table.empty_state or "暂无数据")}</div>'
    note = f'<p class="interpretation">{escape(table.interpretation)}</p>' if table.interpretation else ""
    return (
        f'<div class="visual-table"><div class="visual-table-title"><strong>{escape(table.title)}</strong>'
        f'<small>{len(table.rows)} 项</small></div><div class="action-grid">{"".join(cards)}</div></div>{note}'
    )


def _probability_bars(value):
    matches = re.findall(r"(上涨|震荡|下跌)\s+([\d.]+)%", str(value))
    if not matches:
        return escape(str(value))
    rows = []
    for direction, probability_text in matches:
        css = {"上涨": "up", "震荡": "flat", "下跌": "down"}[direction]
        probability = min(max(float(probability_text), 0.0), 100.0)
        rows.append(
            f'<div class="probability-row"><span>{direction}</span><div class="probability-track">'
            f'<i class="{css}" style="width:{probability:.2f}%"></i></div><b>{escape(probability_text)}%</b></div>'
        )
    return "".join(rows)


def _single_forecast_cards(table):
    cards = []
    for row in table.rows:
        horizon, target, reference, likely, probabilities, return_range, reason, history, eligibility = row.cells
        cards.append(
            f'<article class="forecast-card"><header><div><h3>{escape(horizon)}</h3>'
            f'<p>目标 {escape(target)}</p></div><span>{escape(reference)}</span></header>'
            f'<div class="forecast-likely">{escape(likely)}</div>'
            f'<div class="probability-bars">{_probability_bars(probabilities)}</div>'
            f'<dl><div><dt>预计收益</dt><dd>{escape(return_range)}</dd></div>'
            f'<div><dt>主要依据</dt><dd>{_segment_list(reason)}</dd></div>'
            f'<div><dt>历史可信度</dt><dd>{_segment_list(history)}</dd></div></dl>'
            f'<footer>{escape(eligibility)}</footer></article>'
        )
    if not cards:
        return f'<div class="empty">{escape(table.empty_state or "暂无数据")}</div>'
    note = f'<p class="interpretation">{escape(table.interpretation)}</p>' if table.interpretation else ""
    return (
        f'<div class="visual-table"><div class="visual-table-title"><strong>{escape(table.title)}</strong>'
        f'<small>{len(table.rows)} 个周期</small></div><div class="forecast-grid">{"".join(cards)}</div></div>{note}'
    )


def _operation_cards(table):
    cards = []
    for row in table.rows:
        profile, action, quantity, condition, stop, target, risk, validity = row.cells
        css = "exit" if action in {"卖出", "减仓"} else "entry" if action in {"买入", "加仓"} else "hold"
        cards.append(
            f'<article class="operation-card action-{css}"><header><div><h3>{escape(profile)}方案</h3>'
            f'<p>{escape(validity)}</p></div><span class="action-badge">{escape(action)}</span></header>'
            f'<div class="operation-metrics"><div><small>数量</small><strong>{escape(quantity)}</strong></div>'
            f'<div><small>最大计划亏损</small><strong>{escape(risk)}</strong></div></div>'
            f'<div class="action-steps"><h4>达到以下条件执行</h4>{_segment_list(condition, ordered=True)}</div>'
            f'<div class="exit-grid"><div><small>判断错了</small><p>{_segment_list(stop)}</p></div>'
            f'<div><small>盈利后处理</small><p>{_segment_list(target)}</p></div></div></article>'
        )
    if not cards:
        return f'<div class="empty">{escape(table.empty_state or "暂无数据")}</div>'
    note = f'<p class="interpretation">{escape(table.interpretation)}</p>' if table.interpretation else ""
    return (
        f'<div class="visual-table"><div class="visual-table-title"><strong>{escape(table.title)}</strong>'
        f'<small>{len(table.rows)} 个方案</small></div><div class="action-grid">{"".join(cards)}</div></div>{note}'
    )


def _signal_cards(table):
    cards = []
    for row in table.rows:
        stock, identity, price, judgment, steps, profiles = row.cells
        matched = re.search(r"(卖出|减仓|买入|加仓|持有|观察)", judgment)
        action = matched.group(1) if matched else "观察"
        css = "exit" if action in {"卖出", "减仓"} else "entry" if action in {"买入", "加仓"} else "hold"
        profile_items = re.split(r"\n(?=(?:保守|激进)：)", profiles)
        profile_html = "".join(
            f'<div><strong>{escape(item.split("：", 1)[0])}方案</strong>'
            f'<span>{_segment_list(item.split("：", 1)[1] if "：" in item else item)}</span></div>'
            for item in profile_items if item.strip()
        )
        cards.append(
            f'<article class="signal-card action-{css}"><header><div class="signal-identity">'
            f'<h3>{escape(stock)}</h3><span>{escape(identity)}</span><b>分析价 {escape(price)}</b></div>'
            f'<span class="action-badge">{escape(action)}</span></header>'
            f'<div class="signal-grid"><section><h4>系统判断</h4><p>{escape(judgment)}</p></section>'
            f'<section class="signal-steps"><h4>达到以下条件后再行动</h4>{_segment_list(steps, ordered=True)}</section></div>'
            f'<div class="signal-profiles"><h4>条件满足后的执行方案</h4>'
            f'<div>{profile_html}</div></div></article>'
        )
    if not cards:
        return f'<div class="empty">{escape(table.empty_state or "暂无数据")}</div>'
    note = f'<p class="interpretation">{escape(table.interpretation)}</p>' if table.interpretation else ""
    return (
        f'<div class="visual-table"><div class="visual-table-title"><strong>{escape(table.title)}</strong>'
        f'<small>{len(table.rows)} 只</small></div><div class="signal-cards">{"".join(cards)}</div></div>{note}'
    )


def _editorial_cards(table):
    cards = []
    for row in table.rows:
        stock, identity, action, headline, reasons, risk_note, source = row.cells
        css = "exit" if action in {"卖出", "减仓"} else "entry" if action in {"买入", "加仓"} else "hold"
        cards.append(
            f'<article class="editorial-card action-{css}"><header><div><h3>{escape(stock)}</h3>'
            f'<p>{escape(identity)}</p></div><span class="action-badge">{escape(action)}</span></header>'
            f'<div class="editorial-headline">{escape(headline)}</div>'
            f'<div class="editorial-reasons"><h4>为什么</h4>{_segment_list(reasons)}</div>'
            f'<div class="editorial-risk"><strong>风险提醒</strong><span>{escape(risk_note)}</span></div>'
            f'<footer>{escape(source)}</footer></article>'
        )
    if not cards:
        return f'<div class="empty">{escape(table.empty_state or "暂无数据")}</div>'
    note = f'<p class="interpretation">{escape(table.interpretation)}</p>' if table.interpretation else ""
    return (
        f'<div class="visual-table"><div class="visual-table-title"><strong>{escape(table.title)}</strong>'
        f'<small>{len(table.rows)} 只</small></div><div class="editorial-grid">{"".join(cards)}</div></div>{note}'
    )


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
    if table.table_id in {"portfolio_quick_actions", "single_quick_action"}:
        return _action_cards(table)
    if table.table_id == "operation_editorial":
        return _editorial_cards(table)
    if table.table_id == "forecast_table":
        return _single_forecast_cards(table)
    if table.table_id == "operation_table":
        return _operation_cards(table)
    if table.table_id in {"portfolio_exit_signals", "portfolio_entry_signals", "portfolio_hold_signals"}:
        return _signal_cards(table)
    rows = "".join(
        '<tr class="' + (f"severity-{escape(row.severity.value)}" if row.severity else "") + '">' +
        "".join(
            f'<td data-label="{escape(table.columns[index])}">{_cell_html(table, table.columns[index], cell)}</td>'
            for index, cell in enumerate(row.cells)
        ) + "</tr>"
        for row in table.rows
    )
    if not rows:
        rows = f'<tr><td colspan="{len(table.columns)}" class="empty">{escape(table.empty_state or "暂无数据")}</td></tr>'
    note = f'<p class="interpretation">{escape(table.interpretation)}</p>' if table.interpretation else ""
    rendered = (
        f'<div class="table-wrap table-{escape(table.table_id)}"><table><caption><span>{escape(table.title)}</span>'
        f'<small>{len(table.rows)} 行</small></caption>'
        '<thead><tr>' + "".join(f'<th scope="col">{escape(column)}</th>' for column in table.columns) +
        f'</tr></thead><tbody>{rows}</tbody></table></div>{note}'
    )
    if table.table_id.startswith("stock_detail_"):
        return (
            f'<details class="stock-detail"><summary>{escape(table.title)}'
            '<span>展开详细解释</span></summary>'
            f'{rendered}</details>'
        )
    return rendered


def render_html(document):
    sections = []
    navigation = []
    for index, section in enumerate(document.sections, 1):
        blocks = []
        for block in section.blocks:
            if block.kind is ReportBlockKind.TEXT:
                blocks.append(f'<p>{escape(str(block.payload)).replace(chr(10), "<br>")}</p>')
            elif block.kind is ReportBlockKind.CALLOUT:
                blocks.append(f'<div class="callout">{escape(str(block.payload)).replace(chr(10), "<br>")}</div>')
            elif block.kind is ReportBlockKind.TABLE:
                blocks.append(_table(block.payload))
            elif block.kind is ReportBlockKind.CHART:
                chart = block.payload
                blocks.append(f'<figure><figcaption>{escape(chart.title)} · 样本 {chart.sample_count}</figcaption>{_chart_svg(chart)}<p class="interpretation">{escape(chart.interpretation)}</p></figure>')
            elif block.kind is ReportBlockKind.DIVIDER:
                blocks.append('<hr>')
            else:
                blocks.append(f'<p>{escape(str(block.payload))}</p>')
        section_id = escape(section.section_id)
        navigation.append(f'<a href="#{section_id}"><span>{index:02d}</span>{escape(section.title)}</a>')
        sections.append(
            f'<section id="{section_id}" class="report-section section-{section_id}">'
            f'<div class="section-heading"><span class="section-number">{index:02d}</span>'
            f'<div><h2>{escape(section.title)}</h2><p class="purpose">{escape(section.purpose)}</p></div></div>'
            f'{"".join(blocks)}</section>'
        )
    style = """
    :root{color-scheme:light;--ink:#172033;--muted:#647184;--line:#dce2e9;--soft:#f4f7fa;--canvas:#eef2f6;--accent:#1f5f8b;--accent-soft:#edf5fa;--success:#287a55;--warning:#a85d12;--danger:#b23b3b}
    *{box-sizing:border-box}html{scroll-behavior:smooth}body{font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;margin:0;color:var(--ink);background:var(--canvas);line-height:1.7;letter-spacing:0}
    main{max-width:1500px;margin:0 auto;background:#fff;min-height:100vh;padding:42px 48px 80px}main>header{max-width:1100px;border-bottom:3px solid var(--accent);padding-bottom:24px;margin-bottom:28px}.kicker{color:var(--accent);font-size:13px;font-weight:700;margin:0 0 7px}h1{font-size:32px;line-height:1.25;margin:0 0 14px}main>header .summary{font-size:16px;margin:0 0 10px;max-width:1000px;white-space:pre-line}.meta{color:var(--muted);font-size:13px;margin:0}
    .report-shell{display:grid;grid-template-columns:210px minmax(0,1fr);gap:42px;align-items:start}.contents{position:sticky;top:20px;border-top:3px solid var(--accent);padding-top:12px}.contents strong{display:block;font-size:13px;margin-bottom:8px}.contents a{display:grid;grid-template-columns:28px 1fr;gap:5px;color:var(--muted);text-decoration:none;font-size:12px;line-height:1.35;padding:7px 4px;border-bottom:1px solid #edf0f3}.contents a span{color:var(--accent);font-weight:700}.contents a:hover{color:var(--accent);background:var(--accent-soft)}
    article{min-width:0}.report-section{padding:10px 0 42px;margin-bottom:34px;border-bottom:1px solid var(--line);scroll-margin-top:20px}.section-heading{display:grid;grid-template-columns:44px 1fr;gap:12px;align-items:start;margin-bottom:20px}.section-number{display:flex;align-items:center;justify-content:center;width:38px;height:38px;background:var(--accent);color:#fff;font-size:13px;font-weight:700}.section-heading h2{font-size:23px;line-height:1.3;margin:0 0 3px}.purpose,.interpretation{color:var(--muted);font-size:13px}.purpose{margin:0}.interpretation{margin:9px 2px 24px;padding-left:12px;border-left:2px solid #b9c9d6}
    .callout{border-left:5px solid var(--accent);background:var(--accent-soft);padding:16px 19px;font-weight:600;margin:18px 0 24px}.table-wrap{width:100%;overflow:auto;margin:16px 0 8px;border:1px solid var(--line);border-radius:6px;background:#fff;box-shadow:0 2px 10px rgba(26,46,66,.04)}
    table{width:100%;border-collapse:separate;border-spacing:0;min-width:820px;font-size:14px}caption{text-align:left;padding:14px 16px;background:#fff;border-bottom:1px solid var(--line)}caption span{font-weight:700;font-size:15px}caption small{margin-left:10px;color:var(--muted);font-weight:400}th,td{padding:12px 13px;text-align:left;vertical-align:top;overflow-wrap:anywhere;border-bottom:1px solid #e7ebef}th{background:var(--accent);color:#fff;font-size:12px;font-weight:650;position:sticky;top:0;z-index:2}tbody tr:nth-child(even){background:#f8fafc}tbody tr:hover{background:#edf5fa}tbody tr:last-child td{border-bottom:0}td:first-child{font-weight:650;white-space:nowrap}.empty{color:var(--muted);text-align:center;padding:28px}
    .cell-list{margin:0;padding-left:18px}.cell-list li{margin:0 0 5px}.cell-list li:last-child{margin-bottom:0}.visual-table{margin:16px 0 8px}.visual-table-title{display:flex;align-items:center;gap:10px;margin-bottom:11px}.visual-table-title strong{font-size:16px}.visual-table-title small{color:var(--muted)}.action-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.action-card{border:1px solid var(--line);border-left:5px solid var(--accent);border-radius:6px;padding:14px 16px;background:#fff;min-width:0}.action-card.action-exit{border-left-color:var(--danger)}.action-card.action-entry{border-left-color:var(--success)}.action-card.action-hold{border-left-color:var(--warning)}.action-card header{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin:0 0 8px;padding:0;border:0;max-width:none}.action-card h3{font-size:18px;margin:0}.action-card header p{font-size:12px;color:var(--muted);margin:2px 0 0}.action-badge{flex:none;padding:3px 9px;border-radius:4px;background:var(--accent-soft);color:var(--accent);font-size:12px;font-weight:700}.action-exit .action-badge{background:#fff0f0;color:var(--danger)}.action-entry .action-badge{background:#eaf7f0;color:var(--success)}.action-hold .action-badge{background:#fff7e8;color:var(--warning)}.action-reason{font-size:13px;font-weight:600;margin-bottom:8px}.action-next{display:grid;grid-template-columns:58px 1fr;gap:8px;padding-top:7px;border-top:1px solid var(--line);font-size:11px}.action-next strong{color:var(--muted)}
    .forecast-cell{min-width:170px}.forecast-cell>div:first-child{display:flex;align-items:center;justify-content:space-between;gap:8px}.forecast-cell small{display:block;color:var(--muted);font-size:11px;margin-top:3px}.direction{font-size:12px}.direction.up{color:var(--success)}.direction.flat{color:var(--warning)}.direction.down{color:var(--danger)}.probability-track{height:6px;background:#e7ebef;border-radius:3px;overflow:hidden;margin:7px 0}.probability-track i{display:block;height:100%}.probability-track i.up{background:var(--success)}.probability-track i.flat{background:var(--warning)}.probability-track i.down{background:var(--danger)}
    .forecast-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.forecast-card,.operation-card{border:1px solid var(--line);border-radius:6px;padding:16px;background:#fff;min-width:0}.forecast-card header,.operation-card header{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin:0 0 10px;padding:0;border:0;max-width:none}.forecast-card h3,.operation-card h3{font-size:17px;margin:0}.forecast-card header p,.operation-card header p{font-size:11px;color:var(--muted);margin:2px 0 0}.forecast-card header>span{font-weight:700}.forecast-likely{font-size:15px;font-weight:700;margin-bottom:10px}.probability-bars{padding:10px 12px;background:var(--soft);border-radius:4px}.probability-row{display:grid;grid-template-columns:34px 1fr 44px;gap:8px;align-items:center;font-size:12px}.probability-row .probability-track{margin:5px 0}.probability-row b{text-align:right}.forecast-card dl{margin:12px 0}.forecast-card dl>div{display:grid;grid-template-columns:82px 1fr;gap:8px;padding:7px 0;border-bottom:1px solid #edf0f3}.forecast-card dt,.exit-grid small,.operation-metrics small{color:var(--muted);font-size:11px}.forecast-card dd{margin:0;font-size:12px}.forecast-card footer{padding-top:8px;color:var(--accent);font-size:12px;font-weight:700}.operation-card{border-left:5px solid var(--accent)}.operation-card.action-exit{border-left-color:var(--danger)}.operation-card.action-entry{border-left-color:var(--success)}.operation-card.action-hold{border-left-color:var(--warning)}.operation-metrics{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px}.operation-metrics>div{padding:9px 11px;background:var(--soft);border-radius:4px}.operation-metrics small,.operation-metrics strong{display:block}.exit-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:11px}.exit-grid>div{border-top:2px solid var(--line);padding-top:8px}.exit-grid p{font-size:12px;margin:3px 0 0}
    .signal-cards{display:grid;gap:13px}.signal-card{border:1px solid var(--line);border-left:5px solid var(--accent);border-radius:6px;padding:16px;background:#fff;min-width:0}.signal-card.action-exit{border-left-color:var(--danger)}.signal-card.action-entry{border-left-color:var(--success)}.signal-card.action-hold{border-left-color:var(--warning)}.signal-card>header{display:flex;align-items:center;justify-content:space-between;gap:14px;margin:0 0 13px;padding:0;border:0;max-width:none}.signal-identity{display:flex;align-items:center;gap:10px;min-width:0}.signal-identity h3{font-size:18px;margin:0}.signal-identity span{padding:2px 7px;border-radius:4px;background:var(--soft);color:var(--muted);font-size:11px}.signal-identity b{font-size:12px}.signal-grid{display:grid;grid-template-columns:minmax(240px,.8fr) minmax(0,1.2fr);gap:14px;align-items:start}.signal-grid>section{min-width:0}.signal-grid h4,.signal-profiles h4{font-size:11px;color:var(--muted);margin:0 0 5px}.signal-grid p{font-size:13px;font-weight:600;margin:0}.signal-steps{padding:11px 13px;background:var(--soft);border-radius:4px}.signal-steps .cell-list{font-size:12px}.signal-profiles{margin-top:12px;padding-top:10px;border-top:1px solid var(--line)}.signal-profiles>div{display:grid;grid-template-columns:1fr 1fr;gap:14px}.signal-profiles>div>div{min-width:0}.signal-profiles strong{display:block;color:var(--accent);font-size:11px;margin-bottom:2px}.signal-profiles span{display:block;font-size:12px}.signal-profiles .cell-list{padding-left:16px}
    .editorial-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.editorial-card{border:1px solid var(--line);border-left:5px solid var(--accent);border-radius:6px;padding:15px;background:#fff;min-width:0}.editorial-card.action-exit{border-left-color:var(--danger)}.editorial-card.action-entry{border-left-color:var(--success)}.editorial-card.action-hold{border-left-color:var(--warning)}.editorial-card>header{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:0 0 9px;padding:0;border:0;max-width:none}.editorial-card h3{font-size:17px;margin:0}.editorial-card header p{font-size:11px;color:var(--muted);margin:1px 0 0}.editorial-headline{font-size:14px;font-weight:700;margin-bottom:9px}.editorial-reasons h4{font-size:11px;color:var(--muted);margin:0 0 4px}.editorial-reasons{font-size:12px}.editorial-risk{display:grid;grid-template-columns:58px 1fr;gap:8px;padding:9px 10px;margin-top:9px;background:var(--soft);border-radius:4px;font-size:12px}.editorial-risk strong{color:var(--warning);font-size:11px}.editorial-card footer{margin-top:7px;color:var(--muted);font-size:10px}
    .table-portfolio_forecasts table{min-width:1420px}.table-portfolio_plans table{min-width:1500px}.table-portfolio_facts table,.table-portfolio_quality table{min-width:1260px}.table-portfolio_strategy_history table{min-width:1120px}.table-portfolio_research table{min-width:1050px}.table-portfolio_exit_signals table,.table-portfolio_entry_signals table,.table-portfolio_hold_signals table{min-width:1180px}
    .stock-detail{margin:14px 0;border:1px solid var(--line);border-radius:6px;background:#fff}.stock-detail>summary{display:flex;align-items:center;justify-content:space-between;gap:16px;cursor:pointer;padding:15px 17px;font-weight:700;color:var(--ink);list-style:none}.stock-detail>summary::-webkit-details-marker{display:none}.stock-detail>summary:after{content:"+";font-size:21px;color:var(--accent);line-height:1}.stock-detail[open]>summary:after{content:"−"}.stock-detail>summary span{margin-left:auto;color:var(--muted);font-size:12px;font-weight:500}.stock-detail .table-wrap{margin:0;border-width:1px 0 0;border-radius:0;box-shadow:none}.stock-detail .interpretation{margin:10px 16px 18px}.stock-detail [class*="table-stock_detail_"] table{min-width:0}.stock-detail [class*="table-stock_detail_"] th:first-child,.stock-detail [class*="table-stock_detail_"] td:first-child{width:145px}
    figure{margin:20px 0 28px;border:1px solid var(--line);padding:18px;border-radius:6px;background:#fff}figcaption{font-weight:700}.chart{width:100%;height:auto;min-height:240px}.axis{stroke:#64748b;stroke-width:1}.baseline{fill:none;stroke:#94a3b8;stroke-width:1.5;stroke-dasharray:5 4}.tick,.legend{font-size:11px;fill:#475569}
    @media(max-width:1050px){main{padding:28px 24px 60px}.report-shell{grid-template-columns:1fr}.contents{position:static;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:0 12px}.contents strong{grid-column:1/-1}.report-section{padding-top:6px}}
    @media(max-width:720px){.action-grid,.forecast-grid,.signal-grid,.editorial-grid{grid-template-columns:1fr}.profile-lines,.exit-grid,.signal-profiles>div{grid-template-columns:1fr}.action-card,.forecast-card,.operation-card,.signal-card,.editorial-card{padding:14px}.signal-identity{flex-wrap:wrap}}
    @media(max-width:600px){main{padding:20px 14px 48px}h1{font-size:25px}.contents{grid-template-columns:repeat(2,minmax(0,1fr))}.section-heading{grid-template-columns:34px 1fr;gap:9px}.section-number{width:30px;height:30px}.section-heading h2{font-size:20px}.callout{padding:13px 14px}table{min-width:720px;font-size:13px}th,td{padding:10px}.stock-detail [class*="table-stock_detail_"] table{min-width:0}.stock-detail>summary span{display:none}.chart{min-height:190px}}
    @media print{body{background:#fff}main{max-width:none;padding:0}.contents{display:none}.report-shell{display:block}.report-section{break-inside:auto}.table-wrap{box-shadow:none;overflow:visible}table{font-size:9px;min-width:0!important}th,td{padding:5px 6px}main>header{max-width:none}}
    """
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{escape(document.title)}</title><style>{style}</style></head><body><main><header>'
        f'<p class="kicker">TRADEHELPER · 可审计交易决策报告</p><h1>{escape(document.title)}</h1>'
        f'<p class="summary">{escape(document.summary)}</p>'
        f'<p class="meta">数据时点：{escape(format_datetime(document.as_of, document.market, seconds=True))}</p>'
        f'</header><div class="report-shell"><nav class="contents" aria-label="报告目录"><strong>报告目录</strong>{"".join(navigation)}</nav>'
        f'<article>{"".join(sections)}</article></div></main></body></html>'
    )
