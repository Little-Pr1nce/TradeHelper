"""Paginated Chinese PDF renderer with real tables and charts."""
from __future__ import annotations

from html import escape
from io import BytesIO

from reportlab.graphics.shapes import Drawing, Line, PolyLine, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from contracts import ReportBlockKind
from presentation.formatting import format_datetime


def _styles():
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("ChineseTitle", parent=base["Title"], fontName="STSong-Light", fontSize=20, leading=26, spaceAfter=10),
        "h2": ParagraphStyle("ChineseH2", parent=base["Heading2"], fontName="STSong-Light", fontSize=14, leading=19, spaceBefore=10, spaceAfter=5),
        "body": ParagraphStyle("ChineseBody", parent=base["BodyText"], fontName="STSong-Light", fontSize=9, leading=13, wordWrap="CJK", spaceAfter=5),
        "small": ParagraphStyle("ChineseSmall", parent=base["BodyText"], fontName="STSong-Light", fontSize=7, leading=9, wordWrap="CJK"),
        "callout": ParagraphStyle("ChineseCallout", parent=base["BodyText"], fontName="STSong-Light", fontSize=10, leading=15, leftIndent=8, borderColor=colors.HexColor("#1769aa"), borderWidth=1, borderPadding=8, backColor=colors.HexColor("#eef6fd"), spaceAfter=8),
    }


def _paragraph(value, style):
    return Paragraph(escape(str(value)).replace("\n", "<br/>"), style)


def _readable_cell(value):
    text = str(value)
    return text.replace("；", "\n") if len(text) >= 48 else text


def _numbered_lines(value):
    return "\n".join(
        f"{index}. {item.strip()}"
        for index, item in enumerate(str(value).splitlines(), 1)
        if item.strip()
    )


def _table(value, styles, available_width):
    if value.table_id in {"portfolio_quick_actions", "single_quick_action"}:
        data = [[_paragraph("股票与当前动作", styles["small"]), _paragraph("下一步与风险方案", styles["small"])]]
        data.extend([
            [
                _paragraph(f"{row.cells[0]}\n{row.cells[1]}\n{_readable_cell(row.cells[2])}", styles["small"]),
                _paragraph(f"下一步：\n{_readable_cell(row.cells[3])}\n\n{_readable_cell(row.cells[4])}", styles["small"]),
            ]
            for row in value.rows
        ])
        widths = [available_width * 0.30, available_width * 0.70]
    elif value.table_id == "operation_editorial":
        data = [[_paragraph("股票与结论", styles["small"]), _paragraph("理由与风险提醒", styles["small"])]]
        data.extend([
            [
                _paragraph(f"{row.cells[0]}  ·  {row.cells[2]}\n{row.cells[1]}\n\n{row.cells[3]}", styles["small"]),
                _paragraph(f"为什么：\n{row.cells[4]}\n\n风险提醒：{row.cells[5]}\n{row.cells[6]}", styles["small"]),
            ]
            for row in value.rows
        ])
        widths = [available_width * 0.38, available_width * 0.62]
    elif value.table_id == "forecast_table":
        data = [[_paragraph("预测周期", styles["small"]), _paragraph("预测内容与可信度", styles["small"])]]
        data.extend([
            [
                _paragraph(f"{row.cells[0]}\n目标 {row.cells[1]}\n参考价 {row.cells[2]}", styles["small"]),
                _paragraph(
                    f"{row.cells[3]}\n{_readable_cell(row.cells[4])}\n"
                    f"预计收益：{_readable_cell(row.cells[5])}\n依据：{_readable_cell(row.cells[6])}\n"
                    f"历史：{_readable_cell(row.cells[7])}\n{row.cells[8]}",
                    styles["small"],
                ),
            ]
            for row in value.rows
        ])
        widths = [available_width * 0.24, available_width * 0.76]
    elif value.table_id == "operation_table":
        data = [[_paragraph("方案与动作", styles["small"]), _paragraph("执行步骤与风险", styles["small"])]]
        data.extend([
            [
                _paragraph(f"{row.cells[0]}方案\n{row.cells[1]} {row.cells[2]}\n最大计划亏损 {row.cells[6]}", styles["small"]),
                _paragraph(
                    f"执行条件：\n{_readable_cell(row.cells[3])}\n\n"
                    f"判断错了：{_readable_cell(row.cells[4])}\n"
                    f"盈利后处理：{_readable_cell(row.cells[5])}\n有效期：{row.cells[7]}",
                    styles["small"],
                ),
            ]
            for row in value.rows
        ])
        widths = [available_width * 0.28, available_width * 0.72]
    elif value.table_id in {"portfolio_exit_signals", "portfolio_entry_signals", "portfolio_hold_signals"}:
        data = [[_paragraph("股票与系统判断", styles["small"]), _paragraph("触发步骤与执行方案", styles["small"])]]
        data.extend([
            [
                _paragraph(
                    f"{row.cells[0]}  ·  {row.cells[1]}\n分析价 {row.cells[2]}\n\n{row.cells[3]}",
                    styles["small"],
                ),
                _paragraph(
                    f"达到以下条件后再行动：\n{_numbered_lines(row.cells[4])}\n\n条件满足后的执行方案：\n{row.cells[5]}",
                    styles["small"],
                ),
            ]
            for row in value.rows
        ])
        widths = [available_width * 0.35, available_width * 0.65]
    else:
        data = [[_paragraph(column, styles["small"]) for column in value.columns]]
        data.extend([[_paragraph(_readable_cell(cell), styles["small"]) for cell in row.cells] for row in value.rows])
        widths = [available_width / len(value.columns)] * len(value.columns)
    if not value.rows:
        data.append([_paragraph(value.empty_state or "暂无数据", styles["small"])] + [""] * (len(data[0]) - 1))
    table = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "STSong-Light"),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef1f5")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#162033")),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#cbd2dc")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("SPAN", (0, 1), (-1, 1)) if not value.rows else ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return table


def _chart(chart, width=740, height=245):
    drawing = Drawing(width, height)
    point_sets = [points for _, points in chart.series if points]
    if not point_sets:
        drawing.add(String(20, height / 2, chart.empty_state or "暂无可绘制数据", fontName="STSong-Light", fontSize=10))
        return drawing
    labels = []
    for points in point_sets:
        for label, _ in points:
            if label not in labels:
                labels.append(label)
    values = [float(value) for points in point_sets for _, value in points] + [float(value) for _, value in chart.baseline]
    low, high = min(values), max(values)
    if low == high:
        low -= 1; high += 1
    left, right, bottom, top = 55, width - 20, 35, height - 22
    x_of = lambda label: left if len(labels) == 1 else left + labels.index(label) * (right - left) / (len(labels) - 1)
    y_of = lambda value: bottom + (float(value) - low) * (top - bottom) / (high - low)
    drawing.add(Line(left, bottom, left, top, strokeColor=colors.grey))
    drawing.add(Line(left, bottom, right, bottom, strokeColor=colors.grey))
    drawing.add(String(4, top - 3, f"{high:.3g}", fontName="STSong-Light", fontSize=7))
    drawing.add(String(4, bottom - 3, f"{low:.3g}", fontName="STSong-Light", fontSize=7))
    if chart.baseline:
        points = [(x_of(label), y_of(value)) for label, value in chart.baseline if label in labels]
        if len(points) >= 2:
            drawing.add(PolyLine(points, strokeColor=colors.HexColor("#94a3b8"), strokeDashArray=(4, 3)))
    palette = ("#1769aa", "#15803d", "#b45309", "#b91c1c", "#6d28d9")
    for index, (name, points) in enumerate(chart.series):
        color = colors.HexColor(palette[index % len(palette)])
        coordinates = [(x_of(label), y_of(value)) for label, value in points]
        if len(coordinates) >= 2:
            drawing.add(PolyLine(coordinates, strokeColor=color, strokeWidth=1.8))
        elif coordinates:
            x, y = coordinates[0]; drawing.add(Line(x - 2, y, x + 2, y, strokeColor=color, strokeWidth=2))
        drawing.add(String(left + index * 120, 10, name, fontName="STSong-Light", fontSize=7, fillColor=color))
    drawing.add(String(left, 23, labels[0][:24], fontName="STSong-Light", fontSize=6))
    if len(labels) > 1:
        drawing.add(String(right - 120, 23, labels[-1][:24], fontName="STSong-Light", fontSize=6))
    return drawing


def _footer(canvas, document):
    canvas.saveState(); canvas.setFont("STSong-Light", 8); canvas.setFillColor(colors.grey)
    canvas.drawRightString(landscape(A4)[0] - 12 * mm, 8 * mm, f"第 {canvas.getPageNumber()} 页")
    canvas.restoreState()


def render_pdf(document):
    buffer = BytesIO()
    page_size = landscape(A4)
    margin = 12 * mm
    pdf = SimpleDocTemplate(buffer, pagesize=page_size, leftMargin=margin, rightMargin=margin, topMargin=margin, bottomMargin=14 * mm, title=document.title, author="TradeHelper")
    styles = _styles()
    story = [_paragraph(document.title, styles["title"]), _paragraph(document.summary, styles["callout"]), _paragraph(f"数据时点：{format_datetime(document.as_of, document.market, seconds=True)}", styles["small"]), Spacer(1, 5)]
    available_width = page_size[0] - 2 * margin
    for section in document.sections:
        story.append(_paragraph(section.title, styles["h2"]))
        story.append(_paragraph(section.purpose, styles["small"]))
        for block in section.blocks:
            if block.kind is ReportBlockKind.TEXT:
                story.append(_paragraph(block.payload, styles["body"]))
            elif block.kind is ReportBlockKind.CALLOUT:
                story.append(_paragraph(block.payload, styles["callout"]))
            elif block.kind is ReportBlockKind.TABLE:
                story.append(_paragraph(f"{block.payload.title} · {len(block.payload.rows)} 行", styles["body"]))
                story.append(_table(block.payload, styles, available_width))
                if block.payload.interpretation:
                    story.append(_paragraph(block.payload.interpretation, styles["small"]))
            elif block.kind is ReportBlockKind.CHART:
                story.append(KeepTogether([_paragraph(f"{block.payload.title} · 样本 {block.payload.sample_count}", styles["body"]), _chart(block.payload, width=available_width, height=245), _paragraph(block.payload.interpretation, styles["small"])]))
            story.append(Spacer(1, 5))
    pdf.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buffer.getvalue()
