"""
金融新闻情感分析模块

使用 FinBERT 深度学习模型进行金融新闻情感分类：
  - 模型: mrm8488/distilroberta-finetuned-financial-news-sentiment-analysis
  - 输出: positive / negative / neutral 三分类
  - 首次需下载约 300MB，后续缓存秒级加载

降级方案：模型加载失败时自动切关键词匹配。
"""

import logging
import os
from typing import Optional

from data.models import NewsItem

logger = logging.getLogger(__name__)

_sentiment_pipeline: Optional[object] = None


def _get_pipeline():
    """加载 FinBERT 模型，失败则降级关键词方案。"""
    global _sentiment_pipeline
    if _sentiment_pipeline is None:
        try:
            from transformers import pipeline
            model_name = "mrm8488/distilroberta-finetuned-financial-news-sentiment-analysis"
            logger.info(f"Loading FinBERT: {model_name}")
            _sentiment_pipeline = pipeline(
                "sentiment-analysis", model=model_name,
                tokenizer=model_name, max_length=512, truncation=True,
                local_files_only=True,
            )
            logger.info("FinBERT loaded successfully")
        except Exception as e:
            logger.warning(f"FinBERT failed ({e}), using keyword fallback")
            _sentiment_pipeline = _SimpleFallbackAnalyzer()
    return _sentiment_pipeline


class _SimpleFallbackAnalyzer:
    POSITIVE_WORDS = {"涨", "增长", "利好", "突破", "盈利", "升", "牛",
                       "up", "gain", "profit", "growth", "bull", "breakthrough"}
    NEGATIVE_WORDS = {"跌", "下降", "利空", "亏损", "风险", "降", "熊", "暴跌",
                       "down", "loss", "risk", "bear", "decline", "crash"}

    def __call__(self, texts, **_kwargs):
        results = []
        for text in texts:
            pos_count = sum(1 for w in self.POSITIVE_WORDS if w in text)
            neg_count = sum(1 for w in self.NEGATIVE_WORDS if w in text)
            if pos_count > neg_count:
                results.append({"label": "positive", "score": 0.6})
            elif neg_count > pos_count:
                results.append({"label": "negative", "score": 0.6})
            else:
                results.append({"label": "neutral", "score": 0.7})
        return results


def analyze(news_list: list[NewsItem]) -> list[NewsItem]:
    if not news_list:
        return []

    pipeline = _get_pipeline()
    texts = [n.title for n in news_list]

    try:
        raw_results = pipeline(texts)
    except Exception as e:
        logger.error(f"Sentiment analysis failed: {e}")
        return news_list

    for item, result in zip(news_list, raw_results):
        label = result.get("label", "neutral")
        score = result.get("score", 0.0)
        if label in ("LABEL_0", "NEGATIVE", "negative"):
            item.sentiment = "negative"
        elif label in ("LABEL_1", "NEUTRAL", "neutral"):
            item.sentiment = "neutral"
        elif label in ("LABEL_2", "POSITIVE", "positive"):
            item.sentiment = "positive"
        else:
            item.sentiment = label.lower()
        item.confidence = round(score, 4)

    return news_list


def aggregate(news_list: list[NewsItem]) -> dict:
    if not news_list:
        return {"total": 0, "positive": 0, "negative": 0, "neutral": 0,
                "sentiment_score": 0.0, "summary": "暂无相关新闻数据。", "top_news": ""}

    pos = sum(1 for n in news_list if n.sentiment == "positive")
    neg = sum(1 for n in news_list if n.sentiment == "negative")
    neu = sum(1 for n in news_list if n.sentiment == "neutral")
    total = len(news_list)
    score = (pos - neg) / total if total > 0 else 0.0

    if score > 0.3:
        summary = f"近期新闻整体偏正面，积极新闻占比 {pos/total*100:.1f}%。"
    elif score < -0.3:
        summary = f"近期新闻整体偏负面，消极新闻占比 {neg/total*100:.1f}%。"
    else:
        summary = f"近期新闻整体中性，正面 {pos/total*100:.1f}%，负面 {neg/total*100:.1f}%。"

    top_news = []
    for n in news_list[:5]:
        emoji = {"positive": "🟢", "negative": "🔴", "neutral": "⚪"}.get(n.sentiment, "⚪")
        top_news.append(f"- {emoji} [{n.date}] {n.title}")

    return {"total": total, "positive": pos, "negative": neg, "neutral": neu,
            "sentiment_score": round(score, 4), "summary": summary, "top_news": "\n".join(top_news)}
