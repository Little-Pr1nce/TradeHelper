"""
金融新闻情感分析模块

使用 FinBERT 深度学习模型进行金融新闻情感分类：
  - 模型: mrm8488/distilroberta-finetuned-financial-news-sentiment-analysis
  - 输出: positive / negative / neutral 三分类
  - 首次需下载约 300MB，后续缓存秒级加载

降级方案：模型加载失败时自动切关键词匹配。
"""

import logging
from typing import Optional

from data.models import NewsItem

logger = logging.getLogger(__name__)

_sentiment_pipeline: Optional[object] = None
_FINBERT_MODEL = "mrm8488/distilroberta-finetuned-financial-news-sentiment-analysis"


def _is_model_cached(model_name: str) -> bool:
    """检查 HuggingFace 缓存目录中是否已存在该模型文件。"""
    try:
        from huggingface_hub import try_to_load_from_cache
        for fname in ("config.json", "pytorch_model.bin", "model.safetensors"):
            path = try_to_load_from_cache(repo_id=model_name, filename=fname)
            if path is not None:
                return True
        return False
    except Exception:
        return False


def _get_pipeline():
    """
    加载 FinBERT 模型，采用两阶段策略：

      阶段 1：尝试本地缓存加载（local_files_only=True）
        → 成功：秒级加载，无网络请求
        → 失败：模型未下载或缓存损坏 → 进入阶段 2

      阶段 2：尝试联网下载（local_files_only=False）
        → 成功：从 HuggingFace 下载约 300MB，下次走阶段 1
        → 失败：网络不可用 → 进入降级方案

      降级方案：_SimpleFallbackAnalyzer（关键词匹配）
        → 无需模型文件，不依赖网络，始终可用
    """
    global _sentiment_pipeline
    if _sentiment_pipeline is not None:
        return _sentiment_pipeline

    try:
        from transformers import pipeline

        # ── 阶段 1：优先使用本地缓存（离线，最快） ──
        if _is_model_cached(_FINBERT_MODEL):
            logger.info(f"FinBERT 本地缓存命中，离线加载: {_FINBERT_MODEL}")
            try:
                _sentiment_pipeline = pipeline(
                    "sentiment-analysis",
                    model=_FINBERT_MODEL,
                    tokenizer=_FINBERT_MODEL,
                    max_length=512,
                    truncation=True,
                    local_files_only=True,
                )
                logger.info("FinBERT 加载成功（本地缓存）")
                return _sentiment_pipeline
            except Exception as e:
                logger.warning(f"FinBERT 本地加载失败 ({e})，尝试联网下载...")
        else:
            logger.info(f"FinBERT 未缓存，尝试从 HuggingFace 下载: {_FINBERT_MODEL}")

        # ── 阶段 2：联网下载模型（需要网络） ──
        _sentiment_pipeline = pipeline(
            "sentiment-analysis",
            model=_FINBERT_MODEL,
            tokenizer=_FINBERT_MODEL,
            max_length=512,
            truncation=True,
            local_files_only=False,
        )
        logger.info("FinBERT 加载成功（联网下载）")

    except Exception as e:
        # ── 降级方案：关键词匹配（始终可用） ──
        logger.warning(f"FinBERT 不可用 ({e})，降级为关键词匹配")
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

    from indicators.constants import SENTIMENT_BATCH_SIZE

    pipeline = _get_pipeline()
    texts = [n.title for n in news_list]

    # 拆批推理，避免一次性传入过多文本卡死 UI 线程
    raw_results: list[dict] = []
    try:
        for i in range(0, len(texts), SENTIMENT_BATCH_SIZE):
            batch = texts[i : i + SENTIMENT_BATCH_SIZE]
            raw_results.extend(pipeline(batch))
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
