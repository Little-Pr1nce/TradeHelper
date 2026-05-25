"""
UI 可复用组件模块

提供应用中多处使用的 UI 控件：
  - StarRating:   星级评分组件（1-5 星，支持只读和交互模式）
  - ProgressOverlay: 全屏加载遮罩（带旋转动画和状态文字）
  - InfoCard:     信息卡片（标题 + 内容，带主题色边框）

所有组件继承自 ft.Container，可在任何 Flet 页面中复用。

【扩展点】添加新的通用组件：
  1. 继承 ft.Container
  2. 实现 build() 方法返回控件树
  3. 如需自定义属性，使用 @property 暴露
"""

import flet as ft


class StarRating(ft.Container):
    """
    星级评分组件（可交互 / 只读两种模式）。

    使用示例：
        # 交互模式：用户点击评分
        rating = StarRating(on_change=lambda r: print(r))
        # 只读模式：只展示评分
        rating = StarRating(initial_rating=3, readonly=True)
        # 动态设置评分
        rating.rating = 4

    【扩展点】可新增属性：
      - star_size: 调整星星大小
      - allow_half: 支持半星评分
    """

    def __init__(self, max_stars: int = 5, initial_rating: int = 0,
                 on_change=None, readonly: bool = False):
        super().__init__()
        self.max_stars = max_stars
        self._rating = initial_rating or 0
        self._on_change = on_change  # 评分变化回调
        self._readonly = readonly    # 只读模式：星星不可点击
        self.stars: list[ft.IconButton] = []  # 星星按钮列表

    @property
    def rating(self) -> int:
        """当前评分值（1-5）。"""
        return self._rating

    @rating.setter
    def rating(self, value: int):
        """设置评分并刷新 UI。"""
        self._rating = value
        if self.stars:
            self._update_stars()
            self.update()

    def build(self):
        """构建一行星星按钮。"""
        self.stars = []
        row = ft.Row(spacing=2, controls=[])
        for i in range(1, self.max_stars + 1):
            # 已评分位置显示实心星，其余显示空心星
            star = ft.IconButton(
                icon=ft.Icons.STAR if i <= self._rating else ft.Icons.STAR_BORDER,
                icon_color="amber" if i <= self._rating else "grey400",
                icon_size=28,
                data=i,  # 存储星星序号（用于点击回调）
                on_click=None if self._readonly else self._on_star_click,
            )
            self.stars.append(star)
            row.controls.append(star)
        self.content = row
        return self.content

    def _on_star_click(self, e):
        """点击星星：更新评分并触发回调。"""
        self._rating = e.control.data
        self._update_stars()
        if self._on_change:
            self._on_change(self._rating)
        self.update()

    def _update_stars(self):
        """遍历所有星星，根据当前评分切换实心/空心图标。"""
        for star in self.stars:
            star.icon = ft.Icons.STAR if star.data <= self._rating else ft.Icons.STAR_BORDER
            star.icon_color = "amber" if star.data <= self._rating else "grey400"


class ProgressOverlay(ft.Container):
    """
    全屏加载遮罩组件。

    使用场景：
      - 股票分析进行中（数据获取、指标计算、报告生成）

    用法：
        overlay = ProgressOverlay()
        overlay.visible = True          # 显示遮罩
        overlay.set_status("正在分析...") # 更新状态文字
        overlay.visible = False         # 隐藏遮罩
    """

    def __init__(self):
        super().__init__()
        self._visible = False
        # 状态文字（白色，显示在转圈动画下方）
        self._status_text = ft.Text("", size=14, color="white")

    @property
    def visible(self) -> bool:
        return self._visible

    @visible.setter
    def visible(self, value: bool):
        self._visible = value
        if self.content is not None:
            self.content.visible = value
            self.content.update()

    def set_status(self, text: str):
        """更新进度提示文字。"""
        self._status_text.value = text
        self._status_text.update()

    def build(self):
        self.content = ft.Container(
            visible=self._visible,
            bgcolor=ft.Colors.with_opacity(0.6, "black"),
            left=0, top=0, right=0, bottom=0,
            alignment=ft.Alignment.CENTER,
            content=ft.Column(
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=20,
                controls=[
                    ft.ProgressRing(width=60, height=60, stroke_width=4, color="white"),
                    self._status_text,
                ],
            ),
        )
        return self.content


class InfoCard(ft.Container):
    """
    信息卡片组件。

    用法：
        InfoCard(
            title="股票简介",
            content="贵州茅台是中国白酒行业的龙头企业...",
            color="#2196F3"
        )

    视觉效果：
      - 浅色背景 + 主题色边框
      - 圆角卡片样式
    """

    def __init__(self, title: str, content: str = "", color: str = "#2196F3"):
        super().__init__()
        self._title = title
        self._content = content
        self._color = color

    def build(self):
        self.content = ft.Container(
            # 半透明主题色背景
            bgcolor=ft.Colors.with_opacity(0.08, self._color),
            border=ft.Border(
                top=ft.BorderSide(1, ft.Colors.with_opacity(0.2, self._color)),
                right=ft.BorderSide(1, ft.Colors.with_opacity(0.2, self._color)),
                bottom=ft.BorderSide(1, ft.Colors.with_opacity(0.2, self._color)),
                left=ft.BorderSide(1, ft.Colors.with_opacity(0.2, self._color)),
            ),
            border_radius=8,
            padding=16,
            content=ft.Column(
                spacing=8,
                controls=[
                    ft.Text(self._title, size=16, weight=ft.FontWeight.BOLD,
                            color=self._color),
                    ft.Text(self._content, size=13,
                            color=ft.Colors.GREY_800),
                ],
            ),
        )
        return self.content
