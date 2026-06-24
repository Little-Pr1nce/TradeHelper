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
