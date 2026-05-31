"""
基本面因子 LLM 兜底 — 当 akshare 不可用时通过大模型估算。
"""

import json
import logging
import re

import numpy as np

logger = logging.getLogger(__name__)

_FINANCIAL_PROMPT = """You are a financial analyst. Provide the following data for {name} ({code}, {market}) from reputable sources:

1. Monthly PE(TTM) and PB for the past 3 years (36 values, newest first).
2. Latest quarterly financials.

Return ONLY this JSON:
{{
  "pe_pb_history": [
    {{"date": "2026-05-30", "pe": 42.5, "pb": 12.3}},
    {{"date": "2026-04-30", "pe": 44.1, "pb": 13.0}}
  ],
  "financials": {{
    "report_date": "2026-04-30",
    "roe": 0.48,
    "gross_margin": 0.75,
    "debt_ratio": 0.18,
    "net_profit_yoy": 0.62,
    "revenue_yoy": 0.55
  }}
}}"""


def fetch_fundamental_factors_llm(
    name: str, code: str, market: str,
    model: str, base_url: str, api_key: str,
) -> dict:
    from alpha.fundamental import _calc_style_factors, score_style_factor, \
        score_fundamental_factor, _empty_result

    market_name = "US stock" if market == "US" else "A-share"
    prompt = _FINANCIAL_PROMPT.format(name=name, code=code, market=market_name)

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=120.0)
        completion = client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": prompt}],
            temperature=0.1, max_tokens=3000,
        )
        response = completion.choices[0].message.content or ""
        logger.info(f"基本面 LLM 返回 {len(response)} 字符")

        data = _parse_json(response)
        if not data:
            logger.warning(f"基本面 JSON 解析为空: {response[:300]}")
            return _empty_result("llm")

    except Exception as e:
        logger.error(f"基本面 LLM 获取失败: {e}", exc_info=True)
        return _empty_result("llm")

    style = _calc_style_factors(data.get("pe_pb_history", []))
    financials = data.get("financials", {})
    logger.info(f"基本面因子(LLM): PE分位={style['pe_percentile']:.1%}, ROE={financials.get('roe', 0):.1%}")
    return {
        "style_factors": style,
        "fundamental_factors": {
            "roe": float(financials.get("roe", 0)),
            "gross_margin": float(financials.get("gross_margin", 0)),
            "debt_ratio": float(financials.get("debt_ratio", 0)),
            "net_profit_yoy": float(financials.get("net_profit_yoy", 0)),
            "revenue_yoy": float(financials.get("revenue_yoy", 0)),
        },
        "source": "llm",
    }


def _parse_json(response: str) -> dict | None:
    response = re.sub(r"```(?:json)?\s*", "", response)
    response = re.sub(r"\s*```", "", response)
    match = re.search(r"\{[\s\S]*\}", response)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    return None
