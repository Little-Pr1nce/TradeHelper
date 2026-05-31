"""
新闻获取模块单元测试。
"""

import sys
import tempfile
import os
from pathlib import Path
from datetime import date, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.models import NewsItem
from data.database import Database
from data import news_fetcher


def test_parse_llm_json_markdown():
    sample = '''```json
[{"date": "2026-05-27", "title": "Test News", "content": "Good growth", "source": "Reuters"}]
```'''
    items = news_fetcher._parse_llm_json(sample, "600519", 5)
    assert len(items) == 1
    assert items[0].title == "Test News"
    assert items[0].content == "Good growth"


def test_parse_llm_json_skips_invalid():
    sample = '[{"date": "invalid", "title": "", "source": "X"}, {"date": "2026-05-25", "title": "Valid", "source": "CNBC"}]'
    items = news_fetcher._parse_llm_json(sample, "600519", 5)
    assert len(items) == 1
    assert items[0].title == "Valid"


def test_parse_llm_json_dedupes():
    sample = '''[
        {"date": "2026-05-27", "title": "Dup", "source": "A"},
        {"date": "2026-05-27", "title": "Dup", "source": "B"}
    ]'''
    items = news_fetcher._parse_llm_json(sample, "600519", 5)
    assert len(items) == 1


def test_insert_news_upsert():
    tmpdir = tempfile.mkdtemp()
    Database._instance = None
    db = Database.init(os.path.join(tmpdir, "test.db"))

    item = NewsItem(
        code="600519", date="2026-05-27", title="Dup Test",
        source="Reuters", sentiment="positive", confidence=0.9,
    )
    db.insert_news([item])
    item.sentiment = "negative"
    item.confidence = 0.7
    db.insert_news([item])

    rows = db.execute(
        "SELECT COUNT(*), sentiment FROM news_sentiment WHERE code=? AND title=?",
        ("600519", "Dup Test"),
    ).fetchone()
    assert rows[0] == 1, f"期望 1 条记录，实际 {rows[0]}"
    assert rows[1] == "negative", "upsert 应更新 sentiment"


def test_cache_24h_with_sentiment():
    tmpdir = tempfile.mkdtemp()
    Database._instance = None
    db = Database.init(os.path.join(tmpdir, "test2.db"))
    today = date.today().isoformat()

    for i in range(5):
        db.insert_news([NewsItem(
            code="AAPL", date=today, title=f"News {i}",
            source="CNBC", sentiment="positive", confidence=0.8,
        )])

    recent = db.get_recent_news_with_sentiment("AAPL", hours=24, limit=5)
    assert len(recent) == 5


def test_fallback_cache():
    tmpdir = tempfile.mkdtemp()
    Database._instance = None
    db = Database.init(os.path.join(tmpdir, "test3.db"))
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    db.insert_news([NewsItem(
        code="TSLA", date=yesterday, title="Old News",
        source="Reuters", sentiment="neutral", confidence=0.5,
    )])

    result = news_fetcher._fallback_cache(db, "TSLA", 5, [])
    assert len(result) == 1
    assert result[0].title == "Old News"


def test_merge_news_dedupes():
    real = [NewsItem(code="AAPL", date="2026-05-27", title="Real", source="Reuters")]
    llm = [
        NewsItem(code="AAPL", date="2026-05-27", title="Real", source="LLM"),
        NewsItem(code="AAPL", date="2026-05-26", title="Extra", source="LLM"),
    ]
    merged = news_fetcher._merge_news(real, llm, 5)
    assert len(merged) == 2
    assert merged[0].title == "Real"
    assert merged[0].source == "Reuters"


def test_parse_yfinance_entry():
    from data.news_providers import _parse_yfinance_entry
    entry = {
        "title": "Apple beats earnings",
        "providerPublishTime": 1748367890,
        "publisher": "Reuters",
        "summary": "Strong iPhone sales",
    }
    item = _parse_yfinance_entry(entry, "AAPL")
    assert item is not None
    assert item.title == "Apple beats earnings"
    assert item.source == "Reuters"
    assert item.content == "Strong iPhone sales"


def test_parse_finnhub_entry():
    from data.news_providers import _parse_finnhub_entry
    entry = {
        "headline": "MSFT cloud growth",
        "datetime": 1748367890,
        "source": "Bloomberg",
        "summary": "Azure revenue up",
    }
    item = _parse_finnhub_entry(entry, "MSFT")
    assert item is not None
    assert item.title == "MSFT cloud growth"
    assert item.source == "Bloomberg"


if __name__ == "__main__":
    tests = [
        test_parse_llm_json_markdown,
        test_parse_llm_json_skips_invalid,
        test_parse_llm_json_dedupes,
        test_insert_news_upsert,
        test_cache_24h_with_sentiment,
        test_fallback_cache,
        test_merge_news_dedupes,
        test_parse_yfinance_entry,
        test_parse_finnhub_entry,
    ]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {e}")
    print(f"\n{passed}/{len(tests)} passed")
    sys.exit(0 if passed == len(tests) else 1)
