"""
UI 可复用组件模块

提供应用中多处使用的 UI 控件：
  - StarRating:   星级评分组件（1-5 星，支持只读和交互模式）

所有组件继承自 ft.Container，可在任何 Flet 页面中复用。
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
    """

    def __init__(self, max_stars: int = 5, initial_rating: int = 0,
                 on_change=None, readonly: bool = False):
        super().__init__()
        self.max_stars = max_stars
        self._rating = initial_rating or 0
        self._on_change = on_change
        self._readonly = readonly
        self.stars: list[ft.IconButton] = []

    @property
    def rating(self) -> int:
        return self._rating

    @rating.setter
    def rating(self, value: int):
        self._rating = value
        if self.stars:
            self._update_stars()
            self.update()

    def build(self):
        self.stars = []
        row = ft.Row(spacing=2, controls=[])
        for i in range(1, self.max_stars + 1):
            star = ft.IconButton(
                icon=ft.Icons.STAR if i <= self._rating else ft.Icons.STAR_BORDER,
                icon_color="amber" if i <= self._rating else "grey400",
                icon_size=28,
                data=i,
                on_click=None if self._readonly else self._on_star_click,
            )
            self.stars.append(star)
            row.controls.append(star)
        self.content = row
        return self.content

    def _on_star_click(self, e):
        self._rating = e.control.data
        self._update_stars()
        if self._on_change:
            self._on_change(self._rating)
        self.update()

    def _update_stars(self):
        for star in self.stars:
            star.icon = ft.Icons.STAR if star.data <= self._rating else ft.Icons.STAR_BORDER
            star.icon_color = "amber" if star.data <= self._rating else "grey400"
