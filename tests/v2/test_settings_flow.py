"""UX56：设置按能力检查且永不泄露 secret。"""
from application.settings import settings_capabilities
from config.settings import V2Settings
from contracts import Market
def test_ux56_settings_masks_secret(tmp_path):
 s=V2Settings(tmp_path,llm_api_key="very-secret",llm_base_url="https://x",llm_model="m")
 result=settings_capabilities(s,market=Market.US,mode="eod");assert "very-secret" not in result["public"]["llm_api_key"] and result["research"]
 assert settings_capabilities(s,market=Market.US,mode="pre")["market_data"]
 assert not settings_capabilities(s,market=Market.US,mode="intraday")["market_data"]
 assert not settings_capabilities(s,market=Market.A,mode="eod")["market_data"]
