"""
新闻获取模块。

流程：
  1. 查数据库 → 今天新闻 >= 5 条 → 直接用缓存
  2. 否则 → 调 LLM 获取 → 写入数据库 → 交给 FinBERT 分析
"""

import json
import logging
import re
from datetime import date

from data.models import NewsItem
from data.database import Database

logger = logging.getLogger(__name__)

_NEWS_PROMPT_EN = """You are a professional financial news editor. Search for the latest real news about {name} ({code}) from reputable financial websites (Reuters, Bloomberg, CNBC, etc.).

Return {limit} news items sorted by date descending (newest first). Each item must include: date (YYYY-MM-DD), title, full content, source name.
Output MUST be in English. Output ONLY the JSON array below, nothing else:

[
  {{"date": "2026-05-27", "title": "News Title", "content": "Full news content here", "source": "Reuters"}},
  {{"date": "2026-05-26", "title": "News Title", "content": "Full news content here", "source": "Bloomberg"}}
]"""

_NEWS_PROMPT_CN = """你是一位专业的财经新闻编辑。请从正规财经网站（东方财富、财联社、证券时报、 Reuters、Bloomberg 等）获取关于 {name}（{code}）近一周的真实新闻。

返回 {limit} 条新闻，按日期从新到旧排列。每条包含：日期(YYYY-MM-DD)、标题、完整内容、来源名称。
请用中文输出。只输出以下 JSON 数组，不要其他内容：

[
  {{"date": "2026-05-27", "title": "新闻标题", "content": "完整新闻内容", "source": "东方财富"}},
  {{"date": "2026-05-26", "title": "新闻标题", "content": "完整新闻内容", "source": "财联社"}}
]"""


def fetch_news(
    name: str, code: str, market: str,
    model: str, base_url: str, api_key: str,
    limit: int = 5,
) -> list[NewsItem]:
    """
    获取股票新闻（缓存优先，LLM 兜底）。

    Args:
        name: 股票名称
        code: 股票代码
        market: 市场 (A/US)
        model/base_url/api_key: LLM 配置
        limit: 最大条数

    Returns:
        NewsItem 列表（不含情感标签，需 FinBERT 分析）
    """
    # 1. 查缓存：今天至少有 5 条新闻就直接用
    today_str = date.today().isoformat()
    cached = Database().get_news(code, limit=50)
    today_cached = [n for n in cached if str(n.date)[:10] == today_str]
    if len(today_cached) >= limit:
        recent = sorted(cached, key=lambda n: str(n.date), reverse=True)[:limit]
        logger.info(f"新闻缓存命中: {len(recent)} 条 (今日 {len(today_cached)} 条)")
        return recent

    # 2. 调 LLM 获取
    logger.info(f"缓存不足 (今日 {len(today_cached)} 条)，调用 LLM...")

    prompt_template = _NEWS_PROMPT_EN if market == "US" else _NEWS_PROMPT_CN
    prompt = prompt_template.format(name=name, code=code, limit=limit)

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=120.0)
        completion = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=2000,
        )
        response = completion.choices[0].message.content or ""
        logger.info(f"LLM 返回 {len(response)} 字符")

        items = _parse_llm_json(response, code, limit)
        logger.info(f"LLM 新闻: 解析出 {len(items)} 条")
        if items:
            Database().insert_news(items)
        else:
            logger.warning(f"LLM 新闻解析为空，原始响应前 300 字符: {response[:300]}")
        return items

    except Exception as e:
        logger.error(f"LLM 新闻获取失败: {e}", exc_info=True)
        return []


def _parse_llm_json(response: str, code: str, limit: int) -> list[NewsItem]:
    """解析 LLM 返回的 JSON 新闻列表。"""
    # 去掉 ```json ... ``` 包裹
    response = re.sub(r"```(?:json)?\s*", "", response)
    response = re.sub(r"\s*```", "", response)
    response = response.strip()

    # 找到 JSON 数组
    match = re.search(r"\[\s*\{[\s\S]*\}\s*\]", response)
    json_str = match.group(0) if match else response

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        logger.warning(f"JSON 解析失败: {json_str[:300]}")
        return []

    items = []
    for item in data[:limit]:
        try:
            items.append(NewsItem(
                code=code,
                date=str(item.get("date", ""))[:10],
                title=str(item.get("title", "")),
                source=str(item.get("source", "")),
            ))
        except (KeyError, ValueError, TypeError):
            continue
    # 确保按日期倒序（代码层兜底，即使 LLM 返回顺序有误也纠正）
    items.sort(key=lambda n: str(n.date), reverse=True)
    return items
