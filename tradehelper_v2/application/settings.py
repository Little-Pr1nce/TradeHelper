"""按场景/市场校验能力，不把所有 token 绑成一个全局开关。"""
from __future__ import annotations
from tradehelper_v2.contracts import Market

def _masked(value): return "" if not value else "••••"+value[-4:]
def settings_capabilities(settings, *, market: Market, mode: str):
    if market is Market.A: market_ok=bool(settings.stock_token_a)
    elif mode=="intraday": market_ok=bool(settings.stock_token_us)
    else: market_ok=True
    research_ok=bool(settings.llm_base_url and settings.llm_api_key and settings.llm_model)
    return {"market_data":market_ok,"research":research_ok,"public":{"work_dir":str(settings.work_dir),"llm_base_url":settings.llm_base_url,"llm_model":settings.llm_model,"llm_api_key":_masked(settings.llm_api_key),"stock_token_us":_masked(settings.stock_token_us),"stock_token_a":_masked(settings.stock_token_a)}}
