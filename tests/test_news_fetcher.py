"""
新闻获取模块单元测试。
"""

import sys
import tempfile
import os
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.models import NewsItem
from data.database import Database
from data import news_fetcher



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
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for i in range(5):
        db.insert_news([NewsItem(
            code="AAPL", date=today, title=f"News {i}",
            source="CNBC", sentiment="positive", confidence=0.8,
            fetched_at=fetched_at,
        )])

    recent = db.get_recent_news_with_sentiment("AAPL", hours=24, limit=5)
    assert len(recent) == 5


def test_refresh_state_reuses_partial_cache_without_refetching():
    tmpdir = tempfile.mkdtemp()
    Database._instance = None
    db = Database.init(os.path.join(tmpdir, "partial.db"))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.insert_news([
        NewsItem(
            code="AAPL", date=date.today().isoformat(), title=f"Partial {i}",
            sentiment="positive", confidence=0.8, fetched_at=now,
        )
        for i in range(2)
    ])
    db.save_news_refresh_state(
        "AAPL", "US", attempted_at=now, status="success", item_count=2
    )

    with patch("data.news_fetcher.fetch_from_providers") as provider_fetch:
        result = news_fetcher.fetch_news(
            "Apple", "AAPL", "US", news_token_us="token",
            limit=5, cache_hours=1,
        )

    assert len(result) == 2
    provider_fetch.assert_not_called()


def test_expired_refresh_state_fetches_and_stamps_latest_news():
    tmpdir = tempfile.mkdtemp()
    Database._instance = None
    db = Database.init(os.path.join(tmpdir, "expired.db"))
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    db.save_news_refresh_state(
        "AAPL", "US", attempted_at=old, status="success", item_count=1
    )
    latest = NewsItem(
        code="AAPL", date=date.today().isoformat(), title="Latest independent news"
    )

    with (
        patch("data.news_fetcher.get_news_providers", return_value=[object()]),
        patch("data.news_fetcher.fetch_from_providers", return_value=[latest]) as provider_fetch,
    ):
        result = news_fetcher.fetch_news(
            "Apple", "AAPL", "US", news_token_us="token",
            limit=5, cache_hours=0.5,
        )

    assert provider_fetch.call_count == 1
    assert result[0].fetched_at
    state = db.get_news_refresh_state("AAPL")
    assert state["status"] == "success"
    assert state["item_count"] == 1


def test_shared_news_service_refreshes_without_tab1_and_persists_sentiment():
    from services.news_service import analyze_and_store_news, fetch_stock_news_items

    tmpdir = tempfile.mkdtemp()
    Database._instance = None
    db = Database.init(os.path.join(tmpdir, "shared.db"))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.save_news_refresh_state(
        "MSFT", "US", attempted_at=now, status="success", item_count=1
    )
    latest = NewsItem(
        code="MSFT", date=date.today().isoformat(), title="MSFT latest",
        fetched_at=now,
    )

    def fake_analyze(items):
        for item in items:
            item.sentiment = "positive"
            item.confidence = 0.91
        return items

    with (
        patch("services.news_service.fetch_news", return_value=[latest]) as fetch_mock,
        patch("services.news_service.analyze", side_effect=fake_analyze) as analyze_mock,
    ):
        items = fetch_stock_news_items(
            code="MSFT", name="Microsoft", market="US", mode="intraday",
            db=db,
        )
        analyze_and_store_news({"MSFT": items}, db=db)

    fetch_mock.assert_called_once()
    analyze_mock.assert_called_once()
    stored = db.get_news("MSFT", limit=5)
    assert stored[0].title == "MSFT latest"
    assert stored[0].sentiment == "positive"


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


def test_dedupe_before_unique_index():
    """模拟旧库含重复新闻，初始化时不应抛 IntegrityError。"""
    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "dup.db")
    Database._instance = None

    conn = __import__("sqlite3").connect(db_path)
    conn.executescript("""
        CREATE TABLE news_sentiment (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT, date TEXT, title TEXT,
            source TEXT, sentiment TEXT, confidence REAL
        );
    """)
    conn.executemany(
        "INSERT INTO news_sentiment (code, date, title, source, sentiment, confidence) VALUES (?,?,?,?,?,?)",
        [
            ("600519", "2026-05-27", "Dup", "A", "", 0.0),
            ("600519", "2026-05-27", "Dup", "B", "positive", 0.9),
        ],
    )
    conn.commit()
    conn.close()

    db = Database.init(db_path)
    row = db.execute(
        "SELECT COUNT(*), sentiment FROM news_sentiment WHERE code=? AND title=?",
        ("600519", "Dup"),
    ).fetchone()
    assert row[0] == 1, f"去重后应剩 1 条，实际 {row[0]}"
    assert row[1] == "positive", "应保留带 sentiment 的记录"

    indexes = db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_news_unique'"
    ).fetchall()
    assert len(indexes) == 1


def test_sentiment_aggregate_confidence_and_macro_weighting():
    from indicators.sentiment import aggregate

    result = aggregate([
        NewsItem(
            code="AAPL", date="2026-06-24", title="Company beat",
            sentiment="positive", confidence=0.9,
        ),
        NewsItem(
            code="AAPL", date="2026-06-24", title="Macro risk",
            sentiment="negative", confidence=0.9, is_macro=True,
        ),
    ])

    # 个股正面 0.9 权重，宏观负面 0.9*0.5 权重 → (0.9-0.45)/(1.35)=0.3333
    assert abs(result["sentiment_score"] - 0.3333) < 0.0001


def test_news_df_uses_confidence_weighted_daily_score():
    from services.analysis_service import AnalysisService

    tmpdir = tempfile.mkdtemp()
    Database._instance = None
    db = Database.init(os.path.join(tmpdir, "weighted.db"))
    db.insert_news([
        NewsItem(
            code="AAPL", date="2026-06-24", title="Company beat",
            sentiment="positive", confidence=0.9,
        ),
        NewsItem(
            code="AAPL", date="2026-06-24", title="Company miss",
            sentiment="negative", confidence=0.3,
        ),
    ])

    df = AnalysisService()._build_news_df("AAPL")

    assert df is not None
    score = float(df.loc[df["date"] == "2026-06-24", "finbert_score"].iloc[0])
    assert abs(score - 0.5) < 0.0001


if __name__ == "__main__":
    tests = [
        test_insert_news_upsert,
        test_cache_24h_with_sentiment,
        test_refresh_state_reuses_partial_cache_without_refetching,
        test_expired_refresh_state_fetches_and_stamps_latest_news,
        test_shared_news_service_refreshes_without_tab1_and_persists_sentiment,
        test_fallback_cache,
        test_parse_finnhub_entry,
        test_dedupe_before_unique_index,
        test_sentiment_aggregate_confidence_and_macro_weighting,
        test_news_df_uses_confidence_weighted_daily_score,
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
