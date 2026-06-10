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

    @staticmethod
    def _label(text: str, required: bool = False) -> str:
        """必填字段加红色星号标注。"""
        return f"{text} *" if required else text

    def build(self):
        """构建设置页面的控件树。"""
        settings = Settings()

        # ========== 工作目录 ==========
        self._work_dir_input = ft.TextField(
            label=self._label("项目工作目录", required=True),
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
            label=self._label("LLM Base URL", required=True),
            value=settings.get("llm_base_url", ""),
            hint_text="OpenAI 兼容 API 地址（必填）",
        )
        self._llm_api_key = ft.TextField(
            label=self._label("LLM API Key", required=True),
            value=settings.get("llm_api_key", ""),
            password=True,
            can_reveal_password=True,
            hint_text="请输入 API Key（必填）",
        )
        self._llm_model = ft.TextField(
            label=self._label("模型名称", required=True),
            value=settings.get("llm_model", ""),
            hint_text="如 gpt-4o, deepseek-chat 等（必填）",
        )

        # ========== 数据源配置 ==========
        self._stock_token_us = ft.TextField(
            label="美股数据源 Token（TickFlow API Key）",
            value=settings.get("stock_token_us", ""),
            password=True,
            can_reveal_password=True,
            hint_text="TickFlow API Key（tickflow.org 免费注册获取）",
        )
        self._stock_token_a = ft.TextField(
            label="A 股数据源 Token（TickFlow API Key）",
            value=settings.get("stock_token_a", ""),
            password=True,
            can_reveal_password=True,
            hint_text="TickFlow API Key（tickflow.org 免费注册获取）",
        )
        self._news_token_us = ft.TextField(
            label="新闻数据源 Token - 美股（如 Finnhub）",
            value=settings.get("news_token_us", ""),
            password=True,
            can_reveal_password=True,
            hint_text="美股新闻；留空则尝试免费数据源",
        )
        self._news_token_a = ft.TextField(
            label="新闻数据源 Token - A 股（如 Tushare）",
            value=settings.get("news_token_a", ""),
            password=True,
            can_reveal_password=True,
            hint_text="输入 A 股新闻数据源的 API Token，留空则使用免费数据源",
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
                self._stock_token_us,
                self._stock_token_a,
                self._news_token_us,
                self._news_token_a,
                ft.Text("「股票数据源 Token」填写 TickFlow API Key（tickflow.org 免费注册即可获取实时行情）。\n"
                        "「新闻数据源 Token - 美股」用于美股新闻 API（如 Finnhub 免费 Key）。\n"
                        "「新闻数据源 Token - A 股」用于 A 股新闻 API（如 Tushare）。\n"
                        "留空的字段将自动使用免费数据源。",
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
        """保存所有设置到 JSON 配置文件，含必填验证。"""
        settings = Settings()
        settings.set("work_dir", self._work_dir_input.value)
        settings.set("llm_base_url", self._llm_base_url.value)
        settings.set("llm_api_key", self._llm_api_key.value)
        settings.set("llm_model", self._llm_model.value)
        settings.set("stock_token_us", self._stock_token_us.value)
        settings.set("stock_token_a", self._stock_token_a.value)
        settings.set("news_token_us", self._news_token_us.value)
        settings.set("news_token_a", self._news_token_a.value)
        settings.save()

        # 检查必填项
        missing = settings.missing_fields()
        if missing:
            labels = [Settings.FIELD_LABELS.get(f, f) for f in missing]
            self._status_text.value = f"⚠️ 请填写：{'、'.join(labels)}"
            self._status_text.color = ft.Colors.RED_400
            self._status_text.update()
            return

        # 显示保存成功提示
        self._status_text.value = "设置已保存！"
        self._status_text.color = ft.Colors.GREEN
        self._status_text.update()
        self.page.run_task(self._reset_status)

        if self._on_save:
            self._on_save()

    async def _reset_status(self):
        """延迟 2 秒后清除保存状态提示。"""
        import asyncio
        await asyncio.sleep(2)
        self._status_text.value = ""
        self._status_text.update()
