"""
TradeHelper - 股票分析助手 应用入口

启动 Flet 桌面应用，初始化全局服务，管理页面路由。

应用架构：
  ┌──────────────────────────────────────────┐
  │              NavigationBar               │
  │   [分析]   [历史报告]  [我的持仓]  [设置]  │
  ├──────────────────────────────────────────┤
  │              Stack (页面容器)              │
  │  ┌──────────┐ ┌──────────┐ ┌──────────┐ │
  │  │ MainPage │ │ History  │ │ Settings │ │
  │  │  (分析)   │ │  (历史)   │ │  (设置)  │ │
  │  └──────────┘ └──────────┘ └──────────┘ │
  └──────────────────────────────────────────┘

启动流程：
  1. 加载配置文件
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
from ui.portfolio_page import PortfolioPage
from ui.settings_ui import SettingsPage
from utils.logging import setup_logging


def _run_packaged_smoke_test() -> None:
    """导入运行时动态依赖，供 Windows 打包产物启动验收。"""
    import importlib

    modules = (
        "tickflow",
        "baostock",
        "akshare",
        "jsonpath",
        "markdown_it",
        "setuptools._vendor.jaraco.text",
        "transformers",
        "torch",
        "openai",
    )
    for module in modules:
        importlib.import_module(module)


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
    # 设置应用图标（优先使用 PyInstaller 打包的图标）
    page.icon = "assets/tradehelper.png"  # Flet 自动从 assets/ 加载

    # ========== 初始化全局服务 ==========
    config_path = Settings.default_config_path()
    settings = Settings.init(config_path)

    # PyInstaller 打包模式：自动检测内置 FinBERT 模型路径
    # 开发模式：如果 dist_data 下有模型，也自动启用
    import sys as _sys
    if getattr(_sys, 'frozen', False):
        bundle_dir = Path(_sys._MEIPASS)  # type: ignore
        model_path = bundle_dir / 'dist_data' / 'finbert_model'
    else:
        model_path = Path(__file__).resolve().parent / 'dist_data' / 'finbert_model'
    if model_path.exists() and (model_path / 'config.json').exists():
        settings.set("finbert_model_path", str(model_path))

    # 初始化日志（写入工作目录下的 logs/ 文件夹）
    setup_logging(settings.work_dir)

    # 初始化数据库（自动建表）
    Database.init(settings.db_path)

    # ========== 检查是否已完成必要配置 ==========
    _fully_configured = settings.is_fully_configured()

    def _update_tab_state():
        """根据配置状态启用/禁用导航 tab。"""
        nonlocal _fully_configured
        _fully_configured = settings.is_fully_configured()
        for i, dest in enumerate(navigation_bar.destinations):
            if i == 3:  # 设置页永远可用
                continue
            dest.enabled = _fully_configured
        navigation_bar.update()

    # ========== 设置保存回调 ==========
    def on_settings_saved():
        """设置保存后重新初始化日志、数据库路径，刷新 tab 状态。"""
        setup_logging(settings.work_dir)
        Database.init(settings.db_path)
        _update_tab_state()
        main_page.refresh_modes()
        main_page.update_session_indicator()
        # 如果仍未完全配置，切到设置页
        if not settings.is_fully_configured():
            page.snack_bar = ft.SnackBar(
                ft.Text("请填写所有必填配置项（红色 * 标注），完成后即可使用分析功能。"),
                bgcolor=ft.Colors.BLUE_700,
                duration=5000,
            )
            page.snack_bar.open = True
            page.update()

    # ========== 创建三个页面 ==========
    main_page = MainPage()
    history_page = HistoryPage()
    portfolio_page = PortfolioPage()
    settings_page = SettingsPage(on_save_callback=on_settings_saved)

    # 每个页面包裹在 Container 中，通过 visible 控制显示/隐藏
    # 使用 Stack 叠加而非 TabView，每个页面保留自身状态
    main_container = ft.Container(expand=True, content=main_page)
    history_container = ft.Container(expand=True, visible=False, content=history_page)
    portfolio_container = ft.Container(expand=True, visible=False, content=portfolio_page)
    settings_container = ft.Container(expand=True, visible=False, content=settings_page)

    # ========== 页面切换逻辑 ==========
    def switch_page(e):
        index = e.control.selected_index
        # 未完全配置时，只允许访问设置页
        if not _fully_configured and index != 3:
            page.snack_bar = ft.SnackBar(
                ft.Text("请先在「设置」中填写所有必填配置项。"),
                bgcolor=ft.Colors.ORANGE_700,
            )
            page.snack_bar.open = True
            page.update()
            return
        main_container.visible = index == 0
        history_container.visible = index == 1
        portfolio_container.visible = index == 2
        settings_container.visible = index == 3
        main_container.update()
        history_container.update()
        portfolio_container.update()
        settings_container.update()
        if index == 1:
            history_page._load_reports()
            history_page.update()

    # ========== 底部导航栏 ==========
    navigation_bar = ft.NavigationBar(
        selected_index=0 if _fully_configured else 3,  # 未配置时默认跳设置页
        on_change=switch_page,
        destinations=[
            ft.NavigationBarDestination(
                icon=ft.Icons.ANALYTICS,
                label="分析",
                disabled=not _fully_configured,
            ),
            ft.NavigationBarDestination(
                icon=ft.Icons.HISTORY,
                label="历史报告",
                disabled=not _fully_configured,
            ),
            ft.NavigationBarDestination(
                icon=ft.Icons.ACCOUNT_BALANCE_WALLET,
                label="我的持仓",
                disabled=not _fully_configured,
            ),
            ft.NavigationBarDestination(
                icon=ft.Icons.SETTINGS,
                label="设置",
            ),
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
                            portfolio_container,
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
    if not _fully_configured:
        page.run_task(_show_setup_hint, page, settings)


async def _show_setup_hint(page: ft.Page, settings: Settings):
    """首次使用：引导进入设置页。"""
    import asyncio
    await asyncio.sleep(0.5)
    if not settings.is_fully_configured():
        missing = [Settings.FIELD_LABELS.get(f, f) for f in settings.missing_fields()]
        page.snack_bar = ft.SnackBar(
            ft.Text(f"首次使用，请先配置：{'、'.join(missing)}"),
            bgcolor=ft.Colors.BLUE_700,
            duration=5000,
        )
        page.snack_bar.open = True
        page.update()


# ======================== 程序入口 ========================
if __name__ == "__main__":
    if os.environ.get("TRADEHELPER_SMOKE_TEST") == "1":
        _run_packaged_smoke_test()
    else:
        ft.run(main)
