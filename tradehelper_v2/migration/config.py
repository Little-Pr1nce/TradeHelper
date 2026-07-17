"""V1 配置迁移只补 V2 空字段；不会覆盖用户已有设置或输出 secret。"""
from __future__ import annotations
from typing import Mapping

KNOWN_FIELDS=("work_dir","llm_base_url","llm_api_key","llm_model","stock_token_us","stock_token_a","news_token_us","news_token_a","finbert_model_path","llm_enable_thinking")
ALIASES={"api_key":"llm_api_key","openai_api_key":"llm_api_key","token":"stock_token_us","us_token":"stock_token_us","a_token":"stock_token_a","model":"llm_model"}
def merge_empty_settings(current: Mapping[str,object], legacy: Mapping[str,object]) -> dict[str,object]:
    result=dict(current)
    normalized={ALIASES.get(str(key),str(key)):value for key,value in legacy.items()}
    for key in KNOWN_FIELDS:
        if (result.get(key) is None or result.get(key)=="") and normalized.get(key) not in (None,""):
            result[key]=normalized[key]
    return result
