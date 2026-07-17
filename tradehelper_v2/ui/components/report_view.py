from __future__ import annotations

import flet as ft
import flet.canvas as cv

from tradehelper_v2.contracts import ReportBlockKind


_COLORS = (ft.Colors.BLUE_700, ft.Colors.GREEN_700, ft.Colors.ORANGE_800, ft.Colors.RED_700, ft.Colors.PURPLE_700)


def chart_control(chart):
    width, height = 720.0, 250.0
    point_sets = [points for _, points in chart.series if points]
    if not point_sets:
        return ft.Container(content=ft.Text(chart.empty_state or "暂无可绘制数据"), padding=16, bgcolor=ft.Colors.GREY_100)
    labels = []
    for points in point_sets:
        for label, _ in points:
            if label not in labels:
                labels.append(label)
    values = [float(value) for points in point_sets for _, value in points] + [float(value) for _, value in chart.baseline]
    low, high = min(values), max(values)
    if low == high:
        low -= 1; high += 1
    left, right, top, bottom = 58.0, 700.0, 18.0, 205.0
    x_of = lambda label: left if len(labels) == 1 else left + labels.index(label) * (right - left) / (len(labels) - 1)
    y_of = lambda value: bottom - (float(value) - low) * (bottom - top) / (high - low)
    shapes = [
        cv.Line(left, top, left, bottom, ft.Paint(color=ft.Colors.BLUE_GREY_500, stroke_width=1)),
        cv.Line(left, bottom, right, bottom, ft.Paint(color=ft.Colors.BLUE_GREY_500, stroke_width=1)),
        cv.Text(4, top, f"{high:.3g}", style=ft.TextStyle(size=10, color=ft.Colors.BLUE_GREY_700)),
        cv.Text(4, bottom - 5, f"{low:.3g}", style=ft.TextStyle(size=10, color=ft.Colors.BLUE_GREY_700)),
    ]
    if chart.baseline:
        baseline = [(x_of(label), y_of(value)) for label, value in chart.baseline if label in labels]
        for first, second in zip(baseline, baseline[1:]):
            shapes.append(cv.Line(*first, *second, ft.Paint(color=ft.Colors.BLUE_GREY_300, stroke_width=1)))
    for index, (name, points) in enumerate(chart.series):
        color = _COLORS[index % len(_COLORS)]
        coordinates = [(x_of(label), y_of(value)) for label, value in points]
        for first, second in zip(coordinates, coordinates[1:]):
            shapes.append(cv.Line(*first, *second, ft.Paint(color=color, stroke_width=2)))
        for x, y in coordinates:
            shapes.append(cv.Circle(x, y, 2.5, ft.Paint(color=color)))
        shapes.append(cv.Text(left + index * 125, 228, name, style=ft.TextStyle(size=10, color=color)))
    shapes.append(cv.Text(left, 211, labels[0][:28], style=ft.TextStyle(size=9, color=ft.Colors.BLUE_GREY_700)))
    if len(labels) > 1:
        shapes.append(cv.Text(right - 130, 211, labels[-1][:28], style=ft.TextStyle(size=9, color=ft.Colors.BLUE_GREY_700)))
    canvas = cv.Canvas(shapes=shapes, width=width, height=height)
    return ft.Column([
        ft.Text(f"{chart.title} · 样本 {chart.sample_count}", weight=ft.FontWeight.BOLD),
        ft.Row([canvas], scroll=ft.ScrollMode.AUTO),
        ft.Text(chart.interpretation, size=12, color=ft.Colors.BLUE_GREY_700),
    ], spacing=6)


def table_control(table):
    data = ft.DataTable(
        columns=[ft.DataColumn(ft.Text(column, weight=ft.FontWeight.BOLD)) for column in table.columns],
        rows=[ft.DataRow(cells=[ft.DataCell(ft.Text(cell, selectable=True)) for cell in row.cells]) for row in table.rows],
        column_spacing=18,
        horizontal_lines=ft.BorderSide(1, ft.Colors.BLUE_GREY_100),
    )
    body = ft.Row([data], scroll=ft.ScrollMode.AUTO)
    controls = [ft.Text(f"{table.title} · {len(table.rows)} 行", weight=ft.FontWeight.BOLD), body]
    if not table.rows:
        controls.insert(1, ft.Text(table.empty_state or "暂无数据", color=ft.Colors.BLUE_GREY_600))
    if table.interpretation:
        controls.append(ft.Text(table.interpretation, size=12, color=ft.Colors.BLUE_GREY_700))
    return ft.Column(controls, spacing=5)


def report_view(document):
    controls = [
        ft.Text(document.title, theme_style=ft.TextThemeStyle.HEADLINE_MEDIUM),
        ft.Container(content=ft.Text(document.summary, selectable=True, weight=ft.FontWeight.W_600), padding=12, bgcolor=ft.Colors.BLUE_50, border=ft.Border(left=ft.BorderSide(4, ft.Colors.BLUE_700))),
        ft.Text(f"数据时点：{document.as_of.isoformat()}", size=12, color=ft.Colors.BLUE_GREY_700),
    ]
    for section in document.sections:
        controls.extend((ft.Divider(), ft.Text(section.title, theme_style=ft.TextThemeStyle.TITLE_LARGE), ft.Text(section.purpose, size=12, color=ft.Colors.BLUE_GREY_700)))
        for block in section.blocks:
            if block.kind is ReportBlockKind.TEXT:
                controls.append(ft.Text(str(block.payload), selectable=True))
            elif block.kind is ReportBlockKind.CALLOUT:
                controls.append(ft.Container(content=ft.Text(str(block.payload), selectable=True, weight=ft.FontWeight.BOLD), padding=12, bgcolor=ft.Colors.BLUE_50))
            elif block.kind is ReportBlockKind.TABLE:
                controls.append(table_control(block.payload))
            elif block.kind is ReportBlockKind.CHART:
                controls.append(chart_control(block.payload))
    return ft.Column(controls, expand=True, scroll=ft.ScrollMode.AUTO, spacing=8)
