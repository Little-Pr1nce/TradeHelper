"""
新闻获取模块。

流程：
  1. 查数据库 → 24h 内已有 >= limit 条带情感标签的新闻 → 直接用缓存
  2. 否则 → 真实新闻 API（A 股东财 / 美股 Finnhub）→ 不足时用 LLM 补充
  3. LLM 也失败 → 降级使用历史缓存
"""

import json
import logging
import re
from datetime import date

from data.models import NewsItem
from data.database import Database
from data.news_providers import fetch_from_providers, get_news_providers

logger = logging.getLogger(__name__)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CACHE_HOURS = 24

_NEWS_PROMPT_EN = """You are a financial news editor. Provide the most recent real news about {name} ({code}) from reputable sources (Reuters, Bloomberg, CNBC, etc.).

Return {need} news items sorted by date descending (newest first). Each item must include: date (YYYY-MM-DD), title, full content, source name.
Output MUST be in English. Output ONLY the JSON array below, nothing else:

[
  {{"date": "2026-05-27", "title": "News Title", "content": "Full news content here", "source": "Reuters"}},
  {{"date": "2026-05-26", "title": "News Title", "content": "Full news content here", "source": "Bloomberg"}}
]"""

_NEWS_PROMPT_CN = """你是一位财经新闻编辑。请提供关于 {name}（{code}）的最新真实新闻，来源需为正规渠道（东方财富、财联社、证券时报、Reuters、Bloomberg 等）。

返回 {need} 条新闻，按日期从新到旧排列。每条包含：日期(YYYY-MM-DD)、标题、完整内容、来源名称。
请用中文输出。只输出以下 JSON 数组，不要其他内容：

[
  {{"date": "2026-05-27", "title": "新闻标题", "content": "完整新闻内容", "source": "东方财富"}},
  {{"date": "2026-05-26", "title": "新闻标题", "content": "完整新闻内容", "source": "财联社"}}
]"""


def fetch_news(
    name: str, code: str, market: str,
    model: str, base_url: str, api_key: str,
    limit: int = 5,
    news_token_us: str = "",
    news_token_a: str = "",
) -> list[NewsItem]:
    """
    获取股票新闻（缓存优先 → 真实 API → LLM 补充 → 历史降级）。

    Args:
        name: 股票名称
        code: 股票代码
        market: 市场 (A/US)
        model/base_url/api_key: LLM 配置
        limit: 最大条数
        news_token_us: 美股新闻数据源 Token（如 Finnhub）
        news_token_a: A 股新闻数据源 Token（如 Tushare）

    Returns:
        NewsItem 列表（情感标签由上层 analyze() 填充后入库）
    """
    db = Database()

    # 1. 优先使用 24h 内已完成情感分析的缓存
    cached = db.get_recent_news_with_sentiment(code, hours=_CACHE_HOURS, limit=limit)
    if len(cached) >= limit:
        logger.info(f"新闻缓存命中: {len(cached)} 条 (24h 内已分析)")
        return cached[:limit]

    # 2. 真实新闻 API
    providers = get_news_providers(market, news_token_us, news_token_a)
    items = fetch_from_providers(providers, code, name, market, limit)
    if items:
        logger.info(f"真实新闻 API 合计: {len(items)} 条")

    # 3. 条数不足时用 LLM 补充（不覆盖已有真实新闻）
    if len(items) < limit and api_key:
        need = limit - len(items)
        logger.info(f"真实新闻 {len(items)}/{limit} 条，LLM 补充 {need} 条...")
        llm_items = _fetch_llm_news(name, code, market, model, base_url, api_key, need)
        items = _merge_news(items, llm_items, limit)
    elif len(items) < limit:
        logger.warning(f"真实新闻仅 {len(items)} 条，且未配置 LLM API Key，无法补充")

    if items:
        return items

    # 4. 全部失败 → 降级缓存
    return _fallback_cache(db, code, limit, cached)


def _fetch_llm_news(
    name: str, code: str, market: str,
    model: str, base_url: str, api_key: str,
    limit: int,
) -> list[NewsItem]:
    prompt_template = _NEWS_PROMPT_EN if market == "US" else _NEWS_PROMPT_CN
    prompt = prompt_template.format(name=name, code=code, need=limit)

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
        return items
    except Exception as e:
        logger.error(f"LLM 新闻获取失败: {e}", exc_info=True)
        return []


def _merge_news(
    primary: list[NewsItem], extra: list[NewsItem], limit: int,
) -> list[NewsItem]:
    """合并真实新闻与 LLM 补充，按 (date, title) 去重，真实新闻优先。"""
    merged = list(primary)
    seen = {(n.date, n.title) for n in merged}
    for item in extra:
        key = (item.date, item.title)
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
        if len(merged) >= limit:
            break
    merged.sort(key=lambda n: n.date, reverse=True)
    return merged[:limit]


def _fallback_cache(
    db: Database, code: str, limit: int, partial: list[NewsItem],
) -> list[NewsItem]:
    """LLM 不可用或解析失败时，合并已有缓存与历史新闻。"""
    if partial:
        return partial[:limit]

    stale = db.get_news(code, limit=limit)
    if stale:
        logger.info(f"降级使用历史缓存: {len(stale)} 条")
    return stale


def _parse_llm_json(response: str, code: str, limit: int) -> list[NewsItem]:
    """解析 LLM 返回的 JSON 新闻列表。"""
    response = re.sub(r"```(?:json)?\s*", "", response)
    response = re.sub(r"\s*```", "", response)
    response = response.strip()

    match = re.search(r"\[\s*\{[\s\S]*\}\s*\]", response)
    json_str = match.group(0) if match else response

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        logger.warning(f"JSON 解析失败: {json_str[:300]}")
        return []

    if not isinstance(data, list):
        logger.warning(f"JSON 顶层不是数组: {type(data).__name__}")
        return []

    items: list[NewsItem] = []
    seen: set[tuple[str, str]] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip()
        news_date = str(item.get("date", ""))[:10]
        if not title or not _DATE_RE.match(news_date):
            continue
        key = (news_date, title)
        if key in seen:
            continue
        seen.add(key)
        try:
            items.append(NewsItem(
                code=code,
                date=news_date,
                title=title,
                source=str(item.get("source", "")).strip(),
                content=str(item.get("content", "")).strip()[:500],
            ))
        except (KeyError, ValueError, TypeError):
            continue
        if len(items) >= limit:
            break

    items.sort(key=lambda n: n.date, reverse=True)
    return items
