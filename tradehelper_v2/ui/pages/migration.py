"""首次运行迁移页：预检、跳过和显式确认都由用户操作。"""
from __future__ import annotations
import flet as ft
from tradehelper_v2.migration import LegacyReader, MigrationExecutor, MigrationPlanner

class MigrationPage:
    def __init__(self, repository, source): self.repository=repository; self.source=source; self.message=""; self.plan=None
    def _preflight(self, _=None):
        self.plan=MigrationPlanner(LegacyReader(self.source)).build(); self.message=f"源文件：{self.source}\n迁移项目：{len(self.plan.items)}；请确认后开始。"; self._update()
    def _execute(self, _=None):
        if self.plan is None: self._preflight(); return
        try:
            run=MigrationExecutor(LegacyReader(self.source),self.repository).execute(self.plan,confirm=True); self.message=f"迁移完成：{run.status.value}"
        except Exception as exc: self.message=f"迁移失败：{exc}"
        self._update()
    def _skip(self, _=None): self.message="已跳过迁移；V2 仍以空账户启动。"; self._update()
    def _update(self):
        if getattr(self,"_root",None) is not None: self._root.content=self.build(); self._root.update()
    def build(self):
        return ft.Column([ft.Text("V1 数据迁移",theme_style=ft.TextThemeStyle.HEADLINE_SMALL),ft.Text(self.message or "尚未发现迁移结果。"),ft.Row([ft.Button("导出预检/开始预检",on_click=self._preflight),ft.Button("开始迁移",on_click=self._execute),ft.Button("跳过",on_click=self._skip)])],expand=True)
