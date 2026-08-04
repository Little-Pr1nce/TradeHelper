from __future__ import annotations

import flet as ft
import flet.canvas as cv
import re

from contracts import ReportBlockKind
from presentation.formatting import format_datetime
from ..theme import BORDER, PRIMARY, PRIMARY_SOFT, SURFACE_MUTED, TEXT, TEXT_MUTED


_COLORS = (ft.Colors.BLUE_700, ft.Colors.GREEN_700, ft.Colors.ORANGE_800, ft.Colors.RED_700, ft.Colors.PURPLE_700)


def _segments(value):
    return tuple(item.strip(" /") for item in re.split(r"[；\n]+", str(value)) if item.strip(" /"))


def _segment_control(value, *, numbered=False, color=TEXT, size=13):
    values = _segments(value)
    if len(values) <= 1:
        return ft.Text(str(value), selectable=True, color=color, size=size)
    return ft.Column([
        ft.Row([
            ft.Text(f"{index}." if numbered else "•", size=size, color=TEXT_MUTED, width=18),
            ft.Text(item, selectable=True, color=color, size=size, expand=True),
        ], spacing=3, vertical_alignment=ft.CrossAxisAlignment.START)
        for index, item in enumerate(values, 1)
    ], spacing=4)


def _forecast_control(value):
    matched = re.match(r"^(上涨|震荡|下跌)\s+([\d.]+)%；收益中位\s+([^；]+)；目标\s+(.+)$", str(value))
    if not matched:
        return _segment_control(value)
    direction, probability_text, median, target = matched.groups()
    color = {"上涨": ft.Colors.GREEN_700, "震荡": ft.Colors.ORANGE_800, "下跌": ft.Colors.RED_700}[direction]
    probability = min(max(float(probability_text) / 100.0, 0.0), 1.0)
    return ft.Container(
        ft.Column([
            ft.Row([
                ft.Text(direction, size=12, weight=ft.FontWeight.BOLD, color=color),
                ft.Text(f"{probability_text}%", size=12, weight=ft.FontWeight.BOLD, color=TEXT),
            ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            ft.ProgressBar(value=probability, bar_height=6, color=color, bgcolor=ft.Colors.BLUE_GREY_100, border_radius=3),
            ft.Text(f"收益中位 {median}", size=11, color=TEXT_MUTED),
            ft.Text(f"目标 {target}", size=11, color=TEXT_MUTED),
        ], spacing=3),
        width=180,
    )


def _quick_action_cards(table):
    controls = []
    for row in table.rows:
        stock, identity, action, next_step, profiles = row.cells
        action_name = action.split("；", 1)[0]
        color = (
            ft.Colors.RED_700 if action_name in {"卖出", "减仓"}
            else ft.Colors.GREEN_700 if action_name in {"买入", "加仓"}
            else ft.Colors.ORANGE_800
        )
        action_details = "；".join(_segments(action)[1:]) or action
        next_summary = (_segments(next_step) or ("查看详细操作报告",))[0]
        controls.append(ft.Container(
            content=ft.Column([
                ft.Row([
                    ft.Column([
                        ft.Text(stock, size=18, weight=ft.FontWeight.BOLD, color=TEXT),
                        ft.Text(identity, size=11, color=TEXT_MUTED),
                    ], spacing=1, expand=True),
                    ft.Container(
                        ft.Text(action_name, size=11, weight=ft.FontWeight.BOLD, color=color),
                        padding=ft.Padding.symmetric(horizontal=8, vertical=3),
                        bgcolor=ft.Colors.with_opacity(0.09, color),
                        border_radius=4,
                    ),
                ]),
                ft.Text(action_details, selectable=True, size=12, weight=ft.FontWeight.BOLD, color=TEXT),
                ft.Row([
                    ft.Text("下一条件", size=11, color=TEXT_MUTED),
                    ft.Text(next_summary, selectable=True, size=11, color=TEXT, expand=True),
                ], spacing=7, vertical_alignment=ft.CrossAxisAlignment.START),
            ], spacing=8),
            padding=13,
            border=ft.Border(left=ft.BorderSide(4, color), top=ft.BorderSide(1, BORDER), right=ft.BorderSide(1, BORDER), bottom=ft.BorderSide(1, BORDER)),
            border_radius=6,
            bgcolor=ft.Colors.WHITE,
            col={"sm": 12, "lg": 6},
        ))
    content = [
        ft.Row([
            ft.Text(table.title, size=16, weight=ft.FontWeight.BOLD, color=TEXT),
            ft.Text(f"{len(table.rows)} 项", size=11, color=TEXT_MUTED),
        ], spacing=8),
        ft.ResponsiveRow(controls, spacing=12, run_spacing=12),
    ]
    if table.interpretation:
        content.append(ft.Container(
            ft.Text(table.interpretation, size=12, color=TEXT_MUTED),
            padding=ft.Padding.only(left=10),
            border=ft.Border(left=ft.BorderSide(2, ft.Colors.BLUE_GREY_200)),
        ))
    return ft.Column(content, spacing=10)


def _signal_cards(table):
    controls = []
    for row in table.rows:
        stock, identity, price, judgment, steps, profiles = row.cells
        action_match = re.search(r"(卖出|减仓|买入|加仓|持有|观察)", judgment)
        action = action_match.group(1) if action_match else "观察"
        color = (
            ft.Colors.RED_700 if action in {"卖出", "减仓"}
            else ft.Colors.GREEN_700 if action in {"买入", "加仓"}
            else ft.Colors.ORANGE_800
        )
        profile_controls = []
        for item in re.split(r"\n(?=(?:保守|激进)：)", profiles):
            if not item.strip():
                continue
            name, detail = item.split("：", 1) if "：" in item else ("方案", item)
            profile_controls.append(ft.Column([
                ft.Text(f"{name}方案", size=11, weight=ft.FontWeight.BOLD, color=PRIMARY),
                _segment_control(detail, size=12),
            ], spacing=3, col={"sm": 12, "md": 6}))
        controls.append(ft.Container(
            content=ft.Column([
                ft.Row([
                    ft.Row([
                        ft.Text(stock, size=18, weight=ft.FontWeight.BOLD, color=TEXT),
                        ft.Container(
                            ft.Text(identity, size=11, color=TEXT_MUTED),
                            padding=ft.Padding.symmetric(horizontal=7, vertical=2),
                            bgcolor=SURFACE_MUTED,
                            border_radius=4,
                        ),
                        ft.Text(f"分析价 {price}", size=12, weight=ft.FontWeight.BOLD, color=TEXT),
                    ], spacing=9, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    ft.Container(
                        ft.Text(action, size=11, weight=ft.FontWeight.BOLD, color=color),
                        padding=ft.Padding.symmetric(horizontal=9, vertical=3),
                        bgcolor=ft.Colors.with_opacity(0.09, color),
                        border_radius=4,
                    ),
                ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ft.ResponsiveRow([
                    ft.Column([
                        ft.Text("系统判断", size=11, weight=ft.FontWeight.BOLD, color=TEXT_MUTED),
                        ft.Text(judgment, selectable=True, size=13, color=TEXT),
                    ], spacing=4, col={"sm": 12, "lg": 5}),
                    ft.Container(
                        ft.Column([
                            ft.Text("达到以下条件后再行动", size=11, weight=ft.FontWeight.BOLD, color=TEXT_MUTED),
                            _segment_control(steps, numbered=True, size=12),
                        ], spacing=5),
                        padding=11,
                        bgcolor=SURFACE_MUTED,
                        border_radius=4,
                        col={"sm": 12, "lg": 7},
                    ),
                ], spacing=14, run_spacing=10, vertical_alignment=ft.CrossAxisAlignment.START),
                ft.Divider(height=1, color=ft.Colors.BLUE_GREY_100),
                ft.Column([
                    ft.Text("条件满足后的执行方案", size=11, weight=ft.FontWeight.BOLD, color=TEXT_MUTED),
                    ft.ResponsiveRow(profile_controls, spacing=14, run_spacing=8),
                ], spacing=6),
            ], spacing=12),
            padding=15,
            border=ft.Border(left=ft.BorderSide(4, color), top=ft.BorderSide(1, BORDER), right=ft.BorderSide(1, BORDER), bottom=ft.BorderSide(1, BORDER)),
            border_radius=6,
            bgcolor=ft.Colors.WHITE,
        ))
    body = [
        ft.Row([
            ft.Text(table.title, size=16, weight=ft.FontWeight.BOLD, color=TEXT),
            ft.Text(f"{len(table.rows)} 只", size=11, color=TEXT_MUTED),
        ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
    ]
    if table.interpretation:
        body.append(ft.Container(
            ft.Text(table.interpretation, size=12, color=TEXT_MUTED),
            padding=ft.Padding.only(left=10),
            border=ft.Border(left=ft.BorderSide(2, ft.Colors.BLUE_GREY_200)),
        ))
    if controls:
        body.append(ft.Column(controls, spacing=12))
    else:
        body.append(ft.Container(
            ft.Text(table.empty_state or "暂无数据", color=TEXT_MUTED, text_align=ft.TextAlign.CENTER),
            alignment=ft.Alignment.CENTER,
            padding=18,
        ))
    return ft.Column(body, spacing=10)


def _editorial_cards(table):
    controls = []
    for row in table.rows:
        stock, identity, action, headline, reasons, risk_note, source = row.cells
        color = (
            ft.Colors.RED_700 if action in {"卖出", "减仓"}
            else ft.Colors.GREEN_700 if action in {"买入", "加仓"}
            else ft.Colors.ORANGE_800
        )
        controls.append(ft.Container(
            ft.Column([
                ft.Row([
                    ft.Column([
                        ft.Text(stock, size=17, weight=ft.FontWeight.BOLD, color=TEXT),
                        ft.Text(identity, size=11, color=TEXT_MUTED),
                    ], spacing=1, expand=True),
                    ft.Container(
                        ft.Text(action, size=11, weight=ft.FontWeight.BOLD, color=color),
                        padding=ft.Padding.symmetric(horizontal=9, vertical=3),
                        bgcolor=ft.Colors.with_opacity(0.09, color),
                        border_radius=4,
                    ),
                ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ft.Text(headline, size=14, weight=ft.FontWeight.BOLD, color=TEXT),
                ft.Column([
                    ft.Text("为什么", size=11, weight=ft.FontWeight.BOLD, color=TEXT_MUTED),
                    _segment_control(reasons, size=12),
                ], spacing=4),
                ft.Container(
                    ft.Row([
                        ft.Text("风险提醒", size=11, weight=ft.FontWeight.BOLD, color=color),
                        ft.Text(risk_note, size=12, color=TEXT, expand=True),
                    ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.START),
                    padding=10,
                    bgcolor=ft.Colors.with_opacity(0.06, color),
                    border_radius=4,
                ),
                ft.Text(source, size=10, color=TEXT_MUTED),
            ], spacing=9),
            padding=14,
            border=ft.Border(left=ft.BorderSide(4, color), top=ft.BorderSide(1, BORDER), right=ft.BorderSide(1, BORDER), bottom=ft.BorderSide(1, BORDER)),
            border_radius=6,
            bgcolor=ft.Colors.WHITE,
            col={"sm": 12, "lg": 6},
        ))
    body = [
        ft.Row([
            ft.Text(table.title, size=16, weight=ft.FontWeight.BOLD, color=TEXT),
            ft.Text(f"{len(table.rows)} 只", size=11, color=TEXT_MUTED),
        ], spacing=8),
    ]
    if table.interpretation:
        body.append(ft.Container(
            ft.Text(table.interpretation, size=12, color=TEXT_MUTED),
            padding=ft.Padding.only(left=10),
            border=ft.Border(left=ft.BorderSide(2, ft.Colors.BLUE_GREY_200)),
        ))
    body.append(ft.ResponsiveRow(controls, spacing=12, run_spacing=12) if controls else ft.Text(table.empty_state or "暂无数据", color=TEXT_MUTED))
    return ft.Column(body, spacing=10)


def _probability_bars_control(value):
    matches = re.findall(r"(上涨|震荡|下跌)\s+([\d.]+)%", str(value))
    if not matches:
        return _segment_control(value)
    controls = []
    for direction, probability_text in matches:
        color = {"上涨": ft.Colors.GREEN_700, "震荡": ft.Colors.ORANGE_800, "下跌": ft.Colors.RED_700}[direction]
        probability = min(max(float(probability_text) / 100.0, 0.0), 1.0)
        controls.append(ft.Row([
            ft.Text(direction, size=11, width=34, color=TEXT),
            ft.ProgressBar(value=probability, bar_height=6, color=color, bgcolor=ft.Colors.BLUE_GREY_100, border_radius=3, expand=True),
            ft.Text(f"{probability_text}%", size=11, width=42, text_align=ft.TextAlign.RIGHT),
        ], spacing=7))
    return ft.Container(ft.Column(controls, spacing=4), padding=10, bgcolor=SURFACE_MUTED, border_radius=4)


def _single_forecast_cards(table):
    controls = []
    for row in table.rows:
        horizon, target, reference, likely, probabilities, return_range, reason, history, eligibility = row.cells
        controls.append(ft.Container(
            ft.Column([
                ft.Row([
                    ft.Column([
                        ft.Text(horizon, size=17, weight=ft.FontWeight.BOLD, color=TEXT),
                        ft.Text(f"目标 {target}", size=11, color=TEXT_MUTED),
                    ], spacing=1, expand=True),
                    ft.Text(reference, size=13, weight=ft.FontWeight.BOLD, color=TEXT),
                ]),
                ft.Text(likely, size=14, weight=ft.FontWeight.BOLD, color=TEXT),
                _probability_bars_control(probabilities),
                ft.Column([
                    ft.Text("预计收益", size=11, color=TEXT_MUTED),
                    _segment_control(return_range, size=12),
                    ft.Text("主要依据", size=11, color=TEXT_MUTED),
                    _segment_control(reason, size=12),
                    ft.Text("历史可信度", size=11, color=TEXT_MUTED),
                    _segment_control(history, size=12),
                ], spacing=4),
                ft.Text(eligibility, size=12, weight=ft.FontWeight.BOLD, color=PRIMARY),
            ], spacing=9),
            padding=14,
            border=ft.Border.all(1, BORDER),
            border_radius=6,
            bgcolor=ft.Colors.WHITE,
            col={"sm": 12, "lg": 6},
        ))
    return ft.Column([
        ft.Row([
            ft.Text(table.title, size=16, weight=ft.FontWeight.BOLD, color=TEXT),
            ft.Text(f"{len(table.rows)} 个周期", size=11, color=TEXT_MUTED),
        ], spacing=8),
        ft.ResponsiveRow(controls, spacing=12, run_spacing=12),
        ft.Text(table.interpretation or "", size=12, color=TEXT_MUTED),
    ], spacing=10)


def _operation_cards(table):
    controls = []
    for row in table.rows:
        profile, action, quantity, condition, stop, target, risk, validity = row.cells
        color = (
            ft.Colors.RED_700 if action in {"卖出", "减仓"}
            else ft.Colors.GREEN_700 if action in {"买入", "加仓"}
            else ft.Colors.ORANGE_800
        )
        controls.append(ft.Container(
            ft.Column([
                ft.Row([
                    ft.Column([
                        ft.Text(f"{profile}方案", size=17, weight=ft.FontWeight.BOLD, color=TEXT),
                        ft.Text(validity, size=11, color=TEXT_MUTED),
                    ], spacing=1, expand=True),
                    ft.Container(
                        ft.Text(action, size=11, weight=ft.FontWeight.BOLD, color=color),
                        padding=ft.Padding.symmetric(horizontal=8, vertical=3),
                        bgcolor=ft.Colors.with_opacity(0.09, color),
                        border_radius=4,
                    ),
                ]),
                ft.ResponsiveRow([
                    ft.Column([ft.Text("数量", size=11, color=TEXT_MUTED), ft.Text(quantity, weight=ft.FontWeight.BOLD)], col=6),
                    ft.Column([ft.Text("最大计划亏损", size=11, color=TEXT_MUTED), ft.Text(risk, weight=ft.FontWeight.BOLD)], col=6),
                ], spacing=8),
                ft.Container(
                    ft.Column([
                        ft.Text("达到以下条件执行", size=11, weight=ft.FontWeight.BOLD, color=TEXT_MUTED),
                        _segment_control(condition, numbered=True, size=12),
                    ], spacing=5),
                    padding=10,
                    bgcolor=SURFACE_MUTED,
                    border_radius=4,
                ),
                ft.ResponsiveRow([
                    ft.Column([ft.Text("判断错了", size=11, color=TEXT_MUTED), _segment_control(stop, size=12)], col={"sm": 12, "md": 6}),
                    ft.Column([ft.Text("盈利后处理", size=11, color=TEXT_MUTED), _segment_control(target, size=12)], col={"sm": 12, "md": 6}),
                ], spacing=8, run_spacing=8),
            ], spacing=10),
            padding=14,
            border=ft.Border(left=ft.BorderSide(4, color), top=ft.BorderSide(1, BORDER), right=ft.BorderSide(1, BORDER), bottom=ft.BorderSide(1, BORDER)),
            border_radius=6,
            bgcolor=ft.Colors.WHITE,
            col={"sm": 12, "lg": 6},
        ))
    return ft.Column([
        ft.Row([
            ft.Text(table.title, size=16, weight=ft.FontWeight.BOLD, color=TEXT),
            ft.Text(f"{len(table.rows)} 个方案", size=11, color=TEXT_MUTED),
        ], spacing=8),
        ft.ResponsiveRow(controls, spacing=12, run_spacing=12),
        ft.Text(table.interpretation or "", size=12, color=TEXT_MUTED),
    ], spacing=10)


def _stock_detail_panel(table):
    rows = {row.cells[0]: row.cells[1] for row in table.rows}
    groups = (
        ("股票概况", ft.Icons.BADGE_OUTLINED, (
            ("股票与价格", PRIMARY, 6),
            ("持仓情况", PRIMARY, 6),
        )),
        ("预测如何转成方案", ft.Icons.ACCOUNT_TREE_OUTLINED, (
            ("未来走势", ft.Colors.BLUE_700, 6),
            ("采用策略", ft.Colors.BLUE_700, 6),
            ("当前应对", PRIMARY, 12),
        )),
        ("达到什么条件行动", ft.Icons.RULE_OUTLINED, (
            ("买入或加仓", ft.Colors.GREEN_700, 12),
            ("卖出或减仓", ft.Colors.RED_700, 12),
            ("持有与失效", ft.Colors.ORANGE_800, 12),
        )),
        ("风险与历史依据", ft.Icons.VERIFIED_USER_OUTLINED, (
            ("风险方案", ft.Colors.RED_700, 6),
            ("历史可信度", ft.Colors.BLUE_700, 6),
        )),
    )
    sections = []
    rendered = set()
    for group_title, icon, items in groups:
        controls = []
        for label, color, columns in items:
            value = rows.get(label)
            if value is None:
                continue
            rendered.add(label)
            controls.append(ft.Container(
                ft.Column([
                    ft.Text(label, size=11, weight=ft.FontWeight.BOLD, color=color),
                    _segment_control(value, size=12),
                ], spacing=5),
                padding=ft.Padding(11, 9, 11, 10),
                bgcolor=ft.Colors.with_opacity(0.045, color),
                border=ft.Border(left=ft.BorderSide(3, color)),
                col={"xs": 12, "md": columns},
            ))
        if not controls:
            continue
        sections.append(ft.Column([
            ft.Row([
                ft.Icon(icon, size=17, color=PRIMARY),
                ft.Text(group_title, size=13, weight=ft.FontWeight.BOLD, color=TEXT),
            ], spacing=7),
            ft.ResponsiveRow(controls, spacing=10, run_spacing=10),
        ], spacing=8))

    remaining = tuple((label, value) for label, value in rows.items() if label not in rendered)
    if remaining:
        sections.append(ft.Column([
            ft.Text("其他说明", size=13, weight=ft.FontWeight.BOLD, color=TEXT),
            *(
                ft.Container(
                    ft.Column([
                        ft.Text(label, size=11, weight=ft.FontWeight.BOLD, color=TEXT_MUTED),
                        _segment_control(value, size=12),
                    ], spacing=4),
                    padding=ft.Padding.symmetric(horizontal=11, vertical=8),
                    bgcolor=SURFACE_MUTED,
                )
                for label, value in remaining
            ),
        ], spacing=7))

    expanded_controls = []
    if table.interpretation:
        expanded_controls.append(ft.Container(
            ft.Text(table.interpretation, size=12, color=TEXT_MUTED),
            padding=ft.Padding.only(left=10),
            border=ft.Border(left=ft.BorderSide(2, ft.Colors.BLUE_GREY_200)),
        ))
    expanded_controls.extend(sections)
    if not table.rows:
        expanded_controls.append(ft.Text(table.empty_state or "暂无数据", color=TEXT_MUTED))
    return ft.ExpansionTile(
        title=ft.Text(table.title, size=15, weight=ft.FontWeight.BOLD, color=TEXT),
        subtitle=ft.Text("展开查看预测、策略、交易条件和历史依据", size=11, color=TEXT_MUTED),
        controls=[ft.Container(
            ft.Column(expanded_controls, spacing=16),
            padding=ft.Padding.only(left=12, right=12, top=2, bottom=16),
        )],
        tile_padding=ft.Padding.symmetric(horizontal=12, vertical=4),
        maintain_state=True,
        expanded=False,
        collapsed_bgcolor=ft.Colors.WHITE,
        bgcolor=ft.Colors.WHITE,
        shape=ft.RoundedRectangleBorder(radius=6, side=ft.BorderSide(1, BORDER)),
        collapsed_shape=ft.RoundedRectangleBorder(radius=6, side=ft.BorderSide(1, BORDER)),
    )


def _estimated_data_row_max_height(table):
    """Give multiline cells enough intrinsic height without making short tables sparse."""
    if not table.rows:
        return 72
    longest_row_lines = 1
    for row in table.rows:
        row_lines = 1
        for cell in row.cells:
            parts = _segments(cell) or (str(cell),)
            line_count = sum(max(1, (len(part) + 23) // 24) for part in parts)
            row_lines = max(row_lines, line_count)
        longest_row_lines = max(longest_row_lines, row_lines)
    return min(560, max(88, 34 + longest_row_lines * 22))


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
    if table.table_id in {"portfolio_quick_actions", "single_quick_action"}:
        return _quick_action_cards(table)
    if table.table_id == "operation_editorial":
        return _editorial_cards(table)
    if table.table_id in {"portfolio_exit_signals", "portfolio_entry_signals", "portfolio_hold_signals"}:
        return _signal_cards(table)
    if table.table_id == "forecast_table":
        return _single_forecast_cards(table)
    if table.table_id == "operation_table":
        return _operation_cards(table)
    if table.table_id.startswith("stock_detail_"):
        return _stock_detail_panel(table)

    def cell(value, *, heading=False, column=""):
        text = str(value)
        if not heading and table.table_id == "portfolio_forecasts" and column.startswith("未来"):
            return _forecast_control(text)
        if not heading and len(text) >= 48 and (
            table.table_id in {
                "portfolio_exit_signals", "portfolio_entry_signals", "portfolio_hold_signals",
                "portfolio_plans", "portfolio_research",
            }
            or column in {"下一步条件", "当前判断", "保守与激进"}
        ):
            return ft.Container(_segment_control(text), width=320)
        width = 300 if len(text) > 38 else 180 if len(text) > 18 else None
        control = ft.Text(
            text,
            weight=ft.FontWeight.BOLD if heading else None,
            selectable=not heading,
            no_wrap=heading,
            size=12 if heading else 13,
            color=ft.Colors.WHITE if heading else TEXT,
        )
        return ft.Container(control, width=width) if width else control

    data = ft.DataTable(
        columns=[ft.DataColumn(cell(column, heading=True, column=column)) for column in table.columns],
        rows=[
            ft.DataRow(cells=[
                ft.DataCell(cell(value, column=table.columns[index]))
                for index, value in enumerate(row.cells)
            ])
            for row in table.rows
        ],
        column_spacing=26,
        heading_row_color=PRIMARY,
        heading_row_height=46,
        data_row_min_height=56,
        data_row_max_height=_estimated_data_row_max_height(table),
        border_radius=6,
        horizontal_lines=ft.BorderSide(1, ft.Colors.BLUE_GREY_100),
    )
    body = ft.Row([data], scroll=ft.ScrollMode.AUTO)
    controls = [
        ft.Row([
            ft.Text(table.title, size=15, weight=ft.FontWeight.BOLD, color=TEXT),
            ft.Container(
                ft.Text(f"{len(table.rows)} 行", size=11, color=TEXT_MUTED),
                padding=ft.Padding.symmetric(horizontal=8, vertical=3),
                bgcolor=SURFACE_MUTED,
                border_radius=4,
            ),
        ], spacing=8),
    ]
    if table.interpretation:
        controls.append(ft.Container(
            ft.Text(table.interpretation, size=12, color=TEXT_MUTED),
            padding=ft.Padding.only(left=10),
            border=ft.Border(left=ft.BorderSide(2, ft.Colors.BLUE_GREY_200)),
        ))
    controls.append(body)
    if not table.rows:
        controls.insert(-1, ft.Text(table.empty_state or "暂无数据", color=ft.Colors.BLUE_GREY_600))
    return ft.Column(controls, spacing=8)


def _section_control(section, index):
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
    return ft.Container(
        content=ft.Column([
            ft.Row([
                ft.Container(
                    ft.Text(f"{index:02d}", size=12, weight=ft.FontWeight.BOLD, color=ft.Colors.WHITE),
                    width=34, height=34, bgcolor=PRIMARY, alignment=ft.Alignment.CENTER,
                ),
                ft.Column([
                    ft.Text(section.title, size=20, weight=ft.FontWeight.BOLD, color=TEXT),
                    ft.Text(section.purpose, size=12, color=TEXT_MUTED),
                ], spacing=1, expand=True),
            ], vertical_alignment=ft.CrossAxisAlignment.START),
            *blocks,
        ], spacing=14),
        padding=ft.Padding.only(top=8, bottom=28),
        border=ft.Border(bottom=ft.BorderSide(1, BORDER)),
    )


def report_view(document):
    return ft.Column([
        ft.Text(document.title, size=25, weight=ft.FontWeight.BOLD, color=TEXT),
        ft.Container(
            ft.Text(document.summary, size=14, color=TEXT, selectable=True),
            padding=16,
            bgcolor=PRIMARY_SOFT,
            border=ft.Border(left=ft.BorderSide(4, PRIMARY)),
        ),
        ft.Text(
            f"数据时点：{format_datetime(document.as_of, document.market, seconds=True)}",
            size=11, color=TEXT_MUTED,
        ),
        *(_section_control(section, index) for index, section in enumerate(document.sections, 1)),
    ], spacing=18)
