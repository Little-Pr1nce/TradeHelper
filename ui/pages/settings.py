from __future__ import annotations

import asyncio

import flet as ft

from application.settings import settings_capabilities
from config.settings import V2Settings
from contracts import Market
from ..theme import PRIMARY_SOFT, TEXT_MUTED, configure_field, page_heading, panel, primary_button, secondary_button


def settings_page(settings, market=Market.US, mode="eod", *, save_port=None, live_test_port=None):
    """Build an editable settings page; network tests run only on explicit click."""
    state = {"settings": settings, "market": market, "mode": mode, "message": ""}
    work_dir = ft.TextField(label="工作目录", value=str(settings.work_dir), expand=True)
    base_url = ft.TextField(label="LLM Base URL", value=settings.llm_base_url, expand=True)
    model = ft.TextField(label="LLM 模型", value=settings.llm_model, expand=True)
    llm_key = ft.TextField(label="LLM API Key", password=True, can_reveal_password=True, hint_text="已配置时留空表示不修改", expand=True)
    us_token = ft.TextField(label="美股 TickFlow Token", password=True, can_reveal_password=True, hint_text="已配置时留空表示不修改", expand=True)
    a_token = ft.TextField(label="A股 TickFlow Token", password=True, can_reveal_password=True, hint_text="已配置时留空表示不修改", expand=True)
    us_news = ft.TextField(label="美股新闻 Token", password=True, can_reveal_password=True, hint_text="已配置时留空表示不修改", expand=True)
    a_news = ft.TextField(label="A股新闻 Token", password=True, can_reveal_password=True, hint_text="已配置时留空表示不修改", expand=True)
    finbert_path = ft.TextField(label="FinBERT 模型目录（可选）", value=settings.finbert_model_path, expand=True)
    for control in (work_dir, base_url, model, llm_key, us_token, a_token, us_news, a_news, finbert_path):
        configure_field(control)
    thinking = ft.Switch(label="允许模型思考模式", value=settings.llm_enable_thinking)
    capability = ft.Text()
    message = ft.Text()

    def refresh_capability():
        data = settings_capabilities(state["settings"], market=state["market"], mode=state["mode"])
        capability.value = f"行情能力：{'可用' if data['market_data'] else '不可用'}；研究能力：{'可用' if data['research'] else '不可用'}"

    def current_mapping():
        original = state["settings"]
        return {
            "work_dir": work_dir.value,
            "llm_base_url": base_url.value,
            "llm_model": model.value,
            "llm_api_key": llm_key.value or original.llm_api_key,
            "stock_token_us": us_token.value or original.stock_token_us,
            "stock_token_a": a_token.value or original.stock_token_a,
            "news_token_us": us_news.value or original.news_token_us,
            "news_token_a": a_news.value or original.news_token_a,
            "finbert_model_path": finbert_path.value,
            "llm_enable_thinking": thinking.value,
        }

    def save(_event=None):
        try:
            updated = V2Settings.from_mapping(current_mapping())
            changed_directory = updated.work_dir != state["settings"].work_dir
            (save_port or (lambda value: value.save()))(updated)
            state["settings"] = updated
            llm_key.value = us_token.value = a_token.value = us_news.value = a_news.value = ""
            message.value = "设置已保存，需要重启程序后应用到数据源和模型。"
            message.color = ft.Colors.ORANGE_800
            refresh_capability()
            if message.page: message.update(); capability.update()
        except Exception as exc:
            message.value = f"保存失败：{exc}"; message.color = ft.Colors.RED_700
            if message.page: message.update()

    async def live_test(_event=None):
        live_test_button.disabled = True
        live_test_button.content = "测试中"
        message.value = "正在连接真实数据源，请稍候。"
        message.color = ft.Colors.BLUE_GREY_700
        if live_test_button.page:
            live_test_button.update(); message.update()
        if live_test_port is None:
            message.value = "未配置联网测试服务。"
        else:
            try:
                result = await asyncio.to_thread(
                    live_test_port,
                    state["settings"],
                    market=state["market"],
                    mode=state["mode"],
                )
                message.value = f"联网测试：{result}"
                message.color = ft.Colors.GREEN_700
            except Exception as exc:
                message.value = f"联网测试失败：{exc}"
                message.color = ft.Colors.RED_700
        live_test_button.disabled = False
        live_test_button.content = "联网测试"
        if message.page:
            message.update(); live_test_button.update()

    def change_market(event):
        state["market"] = Market(next(iter(event.control.selected))); refresh_capability()
        if capability.page: capability.update()
    def change_mode(event):
        state["mode"] = event.control.value; refresh_capability()
        if capability.page: capability.update()
    market_control = ft.SegmentedButton(selected=[market.value], segments=[ft.Segment(value="US", label=ft.Text("美股")), ft.Segment(value="A", label=ft.Text("A股"))], on_change=change_market)
    mode_control = configure_field(ft.Dropdown(label="能力场景", value=mode, options=[ft.dropdown.Option("pre", "盘前"), ft.dropdown.Option("intraday", "盘中"), ft.dropdown.Option("eod", "盘后")], on_select=change_mode, width=160))
    refresh_capability()
    public = settings_capabilities(settings, market=market, mode=mode)["public"]
    save_button = primary_button("保存设置", ft.Icons.SAVE_OUTLINED, save)
    live_test_button = secondary_button("联网测试", ft.Icons.WIFI_FIND, live_test)
    content = ft.Column([
        page_heading("设置", "数据源、研究模型与本地运行目录", [
            save_button,
            live_test_button,
        ]),
        panel(ft.Column([
            ft.Text("本地与研究模型", size=14, weight=ft.FontWeight.BOLD),
            ft.ResponsiveRow([work_dir]),
            ft.ResponsiveRow([base_url, model]),
            ft.ResponsiveRow([llm_key, finbert_path]),
            thinking,
        ], spacing=12)),
        panel(ft.Column([
            ft.Text("行情与新闻数据源", size=14, weight=ft.FontWeight.BOLD),
            ft.ResponsiveRow([us_token, a_token]),
            ft.ResponsiveRow([us_news, a_news]),
            ft.Text(f"当前密钥：LLM {public['llm_api_key'] or '未配置'}；美股 {public['stock_token_us'] or '未配置'}；A股 {public['stock_token_a'] or '未配置'}", size=11, color=TEXT_MUTED),
        ], spacing=12)),
        panel(ft.Column([
            ft.Row([ft.Text("能力检查", size=14, weight=ft.FontWeight.BOLD), ft.Row([market_control, mode_control], wrap=True)], alignment=ft.MainAxisAlignment.SPACE_BETWEEN, wrap=True),
            ft.Container(content=capability, padding=12, bgcolor=PRIMARY_SOFT, border_radius=6),
            message,
        ], spacing=10)),
    ], expand=True, scroll=ft.ScrollMode.AUTO, spacing=16)
    return ft.Container(content=content, expand=True, padding=24)
