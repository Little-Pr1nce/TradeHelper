from __future__ import annotations

import flet as ft
import flet.canvas as cv

from contracts import ReportBlockKind
from ..theme import BORDER, PRIMARY, PRIMARY_SOFT, SURFACE_MUTED, TEXT, TEXT_MUTED


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
    def cell(value, *, heading=False):
        text = str(value)
        if len(text) > 38:
            return ft.Container(ft.Text(text, selectable=not heading), width=300)
        return ft.Text(text, weight=ft.FontWeight.BOLD if heading else None, selectable=not heading, no_wrap=True)

    data = ft.DataTable(
        columns=[ft.DataColumn(cell(column, heading=True)) for column in table.columns],
        rows=[ft.DataRow(cells=[ft.DataCell(cell(value)) for value in row.cells]) for row in table.rows],
        column_spacing=22,
        heading_row_color=SURFACE_MUTED,
        heading_row_height=46,
        data_row_min_height=44,
        data_row_max_height=72,
        border=ft.Border.all(1, BORDER),
        border_radius=6,
        horizontal_lines=ft.BorderSide(1, ft.Colors.BLUE_GREY_100),
    )
    body = ft.Row([data], scroll=ft.ScrollMode.AUTO)
    controls = [ft.Text(f"{table.title} · {len(table.rows)} 行", weight=ft.FontWeight.BOLD), body]
    if not table.rows:
        controls.insert(1, ft.Text(table.empty_state or "暂无数据", color=ft.Colors.BLUE_GREY_600))
    if table.interpretation:
        controls.append(ft.Text(table.interpretation, size=12, color=ft.Colors.BLUE_GREY_700))
    return ft.Column(controls, spacing=5)


def _section_control(section):
    blocks = []
    for block in section.blocks:
        if block.kind is ReportBlockKind.TEXT:
            blocks.append(ft.Text(str(block.payload), selectable=True, color=TEXT))
        elif block.kind is ReportBlockKind.CALLOUT:
            blocks.append(ft.Container(
                content=ft.Text(str(block.payload), selectable=True, weight=ft.FontWeight.W_600, color=TEXT),
                padding=14,
                bgcolor=PRIMARY_SOFT,
                border=ft.Border(left=ft.BorderSide(4, PRIMARY)),
                border_radius=6,
            ))
        elif block.kind is ReportBlockKind.TABLE:
            blocks.append(table_control(block.payload))
        elif block.kind is ReportBlockKind.CHART:
            blocks.append(chart_control(block.payload))
        elif block.kind is ReportBlockKind.DIVIDER:
            blocks.append(ft.Divider(height=1, color=BORDER))
        else:
            blocks.append(ft.Text(str(block.payload), selectable=True, color=TEXT))
    return ft.Column([
        ft.Text(section.title, size=19, weight=ft.FontWeight.BOLD, color=TEXT),
        ft.Text(section.purpose, size=12, color=TEXT_MUTED),
        *blocks,
        ft.Divider(height=1, color=BORDER),
    ], spacing=10)


def report_view(document):
    return ft.Column([
        ft.Text(document.title, size=25, weight=ft.FontWeight.BOLD, color=TEXT),
        ft.Text(document.summary, size=14, color=TEXT),
        ft.Text(f"数据时点：{document.as_of.isoformat()}", size=11, color=TEXT_MUTED),
        ft.Divider(height=1, color=BORDER),
        *(_section_control(section) for section in document.sections),
    ], spacing=12)
