"""
TradeHelper - 股票分析助手 应用入口

启动 Flet 桌面应用，初始化全局服务，管理页面路由。

应用架构：
  ┌──────────────────────────────────────────┐
  │              NavigationBar               │
  │    [分析]      [历史报告]      [设置]      │
  ├──────────────────────────────────────────┤
  │              Stack (页面容器)              │
  │  ┌──────────┐ ┌──────────┐ ┌──────────┐ │
  │  │ MainPage │ │ History  │ │ Settings │ │
  │  │  (分析)   │ │  (历史)   │ │  (设置)  │ │
  │  └──────────┘ └──────────┘ └──────────┘ │
  └──────────────────────────────────────────┘

启动流程：
  1. 加载配置文件 (~/.tradehelper/config.json)
  2. 初始化日志系统
  3. 初始化数据库连接并建表
  4. 创建三个页面，通过 NavigationBar 切换
  5. 首次运行时提示用户配置 API

运行方式：
  python main.py                 # 桌面窗口模式
  flet run main.py --web         # Web 模式（实验性）
"""

import os
from pathlib import Path

import flet as ft

from config.settings import Settings
from data.database import Database
from ui.main_page import MainPage
from ui.history_page import HistoryPage
from ui.settings_ui import SettingsPage
from utils.helpers import setup_logging


def main(page: ft.Page):
    """
    Flet 应用主函数。

    负责：
      - 窗口属性配置（标题、尺寸、主题）
      - 全局服务初始化（配置、日志、数据库）
      - 页面路由和导航管理
      - 首次使用提示

    Args:
        page: Flet Page 实例
    """
    # ========== 窗口配置 ==========
    page.title = "TradeHelper - 股票分析助手"
    page.theme_mode = ft.ThemeMode.LIGHT     # 浅色主题
    page.window.width = 1300
    page.window.height = 850
    page.window.min_width = 1000
    page.window.min_height = 650
    page.padding = 20        # 页面内边距

    # ========== 初始化全局服务 ==========
    # 配置文件路径: ~/.tradehelper/config.json
    config_path = Path.home() / ".tradehelper" / "config.json"
    settings = Settings.init(config_path)

    # 初始化日志（写入工作目录下的 logs/ 文件夹）
    setup_logging(settings.work_dir)

    # 初始化数据库（自动建表）
    Database.init(settings.db_path)

    # ========== 设置保存回调 ==========
    def on_settings_saved():
        """设置保存后重新初始化日志和数据库路径。"""
        setup_logging(settings.work_dir)
        Database.init(settings.db_path)
        # 如果仍未配置 API，提示用户
        if not settings.is_configured():
            page.snack_bar = ft.SnackBar(
                ft.Text("提示：建议配置大模型 API 以获得更完整的分析报告。"),
                bgcolor=ft.Colors.ORANGE_700,
            )
            page.snack_bar.open = True
            page.update()

    # ========== 创建三个页面 ==========
    main_page = MainPage()
    history_page = HistoryPage()
    settings_page = SettingsPage(on_save_callback=on_settings_saved)

    # 每个页面包裹在 Container 中，通过 visible 控制显示/隐藏
    # 使用 Stack 叠加而非 TabView，每个页面保留自身状态
    main_container = ft.Container(expand=True, content=main_page)
    history_container = ft.Container(expand=True, visible=False, content=history_page)
    settings_container = ft.Container(expand=True, visible=False, content=settings_page)

    # ========== 页面切换逻辑 ==========
    def switch_page(e):
        """
        底部导航栏切换事件处理。

        使用 visible 切换替代页面销毁/重建：
          - 优势：保持页面状态（如分析结果、历史列表位置）
          - 切换到"历史报告"时自动刷新列表
        """
        index = e.control.selected_index
        main_container.visible = index == 0
        history_container.visible = index == 1
        settings_container.visible = index == 2
        main_container.update()
        history_container.update()
        settings_container.update()
        # 切换到历史页时刷新数据
        if index == 1:
            history_page._load_reports()
            history_page.update()

    # ========== 底部导航栏 ==========
    navigation_bar = ft.NavigationBar(
        selected_index=0,
        on_change=switch_page,
        destinations=[
            ft.NavigationBarDestination(icon=ft.Icons.ANALYTICS, label="分析"),
            ft.NavigationBarDestination(icon=ft.Icons.HISTORY, label="历史报告"),
            ft.NavigationBarDestination(icon=ft.Icons.SETTINGS, label="设置"),
        ],
    )

    # ========== 整体布局 ==========
    page.add(
        ft.Column(
            expand=True,
            spacing=0,
            controls=[
                # 主内容区（占据剩余空间）
                ft.Container(
                    expand=True,
                    content=ft.Stack(
                        controls=[
                            main_container,
                            history_container,
                            settings_container,
                        ],
                    ),
                ),
                # 底部导航栏
                navigation_bar,
            ],
        )
    )

    page.update()

    # ========== 首次使用提示 ==========
    # 如果用户未配置 LLM API，延迟显示提示
    if not settings.is_configured():
        page.run_task(_show_setup_hint, page)


async def _show_setup_hint(page: ft.Page):
    """
    首次使用提示：引导用户进入设置页配置 API。

    延迟 0.5 秒显示（等窗口渲染完毕）。
    """
    import asyncio
    await asyncio.sleep(0.5)
    if not Settings().is_configured():
        page.snack_bar = ft.SnackBar(
            ft.Text("首次使用，请先在「设置」中配置工作目录和大模型 API。"),
            bgcolor=ft.Colors.BLUE_700,
            duration=5000,  # 显示 5 秒
        )
        page.snack_bar.open = True
        page.update()


# ======================== 程序入口 ========================
if __name__ == "__main__":
    ft.run(main)
