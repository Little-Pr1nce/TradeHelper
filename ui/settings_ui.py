"""
设置页面 UI

提供应用配置的可视化编辑界面，包括：
  - 工作目录选择（带文件夹浏览器）
  - LLM API 配置（URL、Key、模型名）
  - 数据源选择（免费 / 自定义 API）

配置修改后通过 Settings 单例持久化到 JSON 文件，
并通过 on_save_callback 通知主应用重新初始化相关服务。

【扩展点】新增配置项：
  1. 在 config/settings.py 的 DEFAULT_CONFIG 中添加键
  2. 在本页 build() 中添加对应的输入控件
  3. 在 _save_settings() 中添加保存逻辑
"""

import flet as ft

from config.settings import Settings


class SettingsPage(ft.Container):
    """
    设置页面（通过底部导航栏的"设置"标签进入）。

    页面分区：
      1. 工作目录 — 选择数据存储路径
      2. 大模型 API — 配置 OpenAI 兼容 API
      3. 数据源 — 选择免费/自定义数据源

    属性：
      _on_save: 保存后的回调函数（由 main.py 传入，用于重新初始化服务）
    """

    def __init__(self, on_save_callback=None):
        """
        Args:
            on_save_callback: 保存设置后的回调（无参数）
        """
        super().__init__()
        self._on_save = on_save_callback
        self._status_text = ft.Text("", size=13, color=ft.Colors.GREEN)  # 保存状态提示

    def build(self):
        """构建设置页面的控件树。"""
        settings = Settings()

        # ========== 工作目录 ==========
        self._work_dir_input = ft.TextField(
            label="项目工作目录",
            value=settings.get("work_dir", ""),
            hint_text="数据、报告、日志存放目录",
            expand=True,
        )
        work_dir_row = ft.Row(
            controls=[
                self._work_dir_input,
                ft.IconButton(
                    icon=ft.Icons.FOLDER_OPEN,
                    tooltip="选择目录",
                    on_click=self._pick_work_dir,
                ),
            ]
        )

        # ========== LLM API 配置 ==========
        self._llm_base_url = ft.TextField(
            label="LLM Base URL",
            value=settings.get("llm_base_url", "https://api.openai.com/v1"),
            hint_text="OpenAI 兼容 API 地址",
        )
        self._llm_api_key = ft.TextField(
            label="LLM API Key",
            value=settings.get("llm_api_key", ""),
            password=True,            # 密码模式（掩码显示）
            can_reveal_password=True,  # 可点击眼睛图标查看
            hint_text="请输入 API Key",
        )
        self._llm_model = ft.TextField(
            label="模型名称",
            value=settings.get("llm_model", "gpt-4o"),
            hint_text="如 gpt-4o, gpt-4, deepseek-chat 等",
        )

        # ========== 数据源配置 ==========
        self._stock_data_token = ft.TextField(
            label="股票数据源 Token（如 itick）",
            value=settings.get("stock_data_token", ""),
            password=True,
            can_reveal_password=True,
            hint_text="输入付费股票数据源的 API Token，留空则使用免费数据源",
        )
        self._news_token_us = ft.TextField(
            label="新闻数据源 Token - 美股（如 Finnhub）",
            value=settings.get("news_token_us", ""),
            password=True,
            can_reveal_password=True,
            hint_text="输入美股新闻数据源的 API Token，留空则使用免费数据源",
        )
        self._news_token_a = ft.TextField(
            label="新闻数据源 Token - A 股（如 Tushare）",
            value=settings.get("news_token_a", ""),
            password=True,
            can_reveal_password=True,
            hint_text="输入 A 股新闻数据源的 API Token，留空则使用免费数据源",
        )

        # ========== 代理配置 ==========
        self._proxy_input = ft.TextField(
            label="HTTPS 代理地址",
            value=settings.get("proxy", ""),
            hint_text="如 http://127.0.0.1:8118（MonoProxy）或 7890（Clash）；留空则自动读系统代理",
        )

        # ========== 保存按钮 ==========
        save_btn = ft.Button(
            content=ft.Text("保存设置"),
            icon=ft.Icons.SAVE,
            on_click=self._save_settings,
            style=ft.ButtonStyle(
                bgcolor=ft.Colors.BLUE_700,
                color=ft.Colors.WHITE,
            ),
        )

        # ========== 页面布局 ==========
        self.content = ft.Column(
            scroll=ft.ScrollMode.AUTO,
            spacing=16,
            controls=[
                ft.Text("设置", size=24, weight=ft.FontWeight.BOLD),
                ft.Divider(),

                # 工作目录区域
                ft.Text("工作目录", size=16, weight=ft.FontWeight.W_600),
                work_dir_row,
                ft.Text("此目录将存储数据库、分析报告 PDF、K 线图等所有数据文件。",
                        size=12, color=ft.Colors.GREY_600),

                ft.Divider(),

                # LLM API 区域
                ft.Text("大模型 API 配置", size=16, weight=ft.FontWeight.W_600),
                self._llm_base_url,
                self._llm_api_key,
                self._llm_model,
                ft.Text("使用 OpenAI 兼容 API 格式。\n"
                        "本地 Ollama: URL=http://localhost:11434/v1, Key=ollama, Model=qwen2.5",
                        size=12, color=ft.Colors.GREY_600),

                ft.Divider(),

                # 数据源区域
                ft.Text("数据源配置", size=16, weight=ft.FontWeight.W_600),
                self._stock_data_token,
                self._news_token_us,
                self._news_token_a,
                ft.Text("「股票数据源 Token」用于付费股票行情 API（如 itick）。\n"
                        "「新闻数据源 Token - 美股」用于美股新闻 API（如 Finnhub）。\n"
                        "「新闻数据源 Token - A 股」用于 A 股新闻 API（如 Tushare）。\n"
                        "留空的字段将自动使用免费数据源。",
                        size=12, color=ft.Colors.GREY_600),

                ft.Divider(),

                ft.Text("代理配置", size=16, weight=ft.FontWeight.W_600),
                self._proxy_input,
                ft.Text("用于访问 Yahoo Finance（美股搜索）。\n"
                        "若已开启 MonoProxy 等系统代理，可留空自动检测；否则填本地 HTTP 代理地址。",
                        size=12, color=ft.Colors.GREY_600),

                ft.Divider(),

                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    controls=[
                        save_btn,
                        self._status_text,
                    ],
                ),
            ],
        )
        return self.content

    def _pick_work_dir(self, e):
        """打开系统文件夹选择器选择工作目录。"""
        async def handle():
            picker = ft.FilePicker()
            self.page.overlay.append(picker)
            self.page.update()
            path = await picker.get_directory_path()
            if path:
                self._work_dir_input.value = path
                self._work_dir_input.update()
        self.page.run_task(handle)

    def _save_settings(self, e):
        """保存所有设置到 JSON 配置文件。"""
        settings = Settings()
        settings.set("work_dir", self._work_dir_input.value)
        settings.set("llm_base_url", self._llm_base_url.value)
        settings.set("llm_api_key", self._llm_api_key.value)
        settings.set("llm_model", self._llm_model.value)
        settings.set("stock_data_token", self._stock_data_token.value)
        settings.set("news_token_us", self._news_token_us.value)
        settings.set("news_token_a", self._news_token_a.value)
        settings.set("proxy", self._proxy_input.value)
        settings.save()

        # 显示保存成功提示（2 秒后自动消失）
        self._status_text.value = "设置已保存！"
        self._status_text.color = ft.Colors.GREEN
        self._status_text.update()
        self.page.run_task(self._reset_status)

        # 通知主应用（重新初始化日志和数据库路径）
        if self._on_save:
            self._on_save()

    async def _reset_status(self):
        """延迟 2 秒后清除保存状态提示。"""
        import asyncio
        await asyncio.sleep(2)
        self._status_text.value = ""
        self._status_text.update()
