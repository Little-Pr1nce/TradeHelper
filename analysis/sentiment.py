"""
金融新闻情感分析模块

使用 HuggingFace FinBERT 模型对新闻标题进行情感分类：
  - 模型: mrm8488/distilroberta-finetuned-financial-news-sentiment-analysis
  - 该模型基于 DistilRoBERTa，专门针对金融新闻领域微调
  - 输出三分类: positive（积极）/ negative（消极）/ neutral（中性）

架构特点：
  - 延迟加载：模型在首次调用 analyze() 时才加载，避免启动时耗时
  - 内存缓存：模型加载后缓存在模块全局变量中，后续调用直接复用
  - 降级方案：如果模型加载失败（无网络/显存不足等），
    自动降级为关键词匹配分析器，保证功能可用

【扩展点】切换/替换情感分析模型：
  1. 修改 _get_pipeline() 中的 model_name 为新的 HuggingFace 模型 ID
  2. 如果新模型输出标签格式不同，在 analyze() 中调整标签映射逻辑
  3. 可选：支持同时运行多个模型进行集成推理
"""

import logging
from typing import Optional

from data.models import NewsItem

logger = logging.getLogger(__name__)

# 模块级缓存：情感分析 pipeline 实例（延迟初始化）
_sentiment_pipeline: Optional[object] = None


def _get_pipeline():
    """
    获取情感分析 pipeline 实例（懒加载 + 缓存）。

    加载策略：
      1. 首次调用时加载 FinBERT 模型（约 300MB，需联网下载）
      2. 加载后缓存在 _sentiment_pipeline 全局变量中
      3. 加载失败则降级为 _SimpleFallbackAnalyzer 关键词方案

    模型下载位置：
      ~/.cache/huggingface/hub/

    可通过环境变量自定义：
      export HF_HOME=/your/custom/path
    """
    global _sentiment_pipeline
    if _sentiment_pipeline is None:
        from transformers import pipeline
        # HuggingFace 上的金融新闻情感分析模型 ID
        model_name = "mrm8488/distilroberta-finetuned-financial-news-sentiment-analysis"
        logger.info(f"Loading sentiment model: {model_name}")
        try:
            _sentiment_pipeline = pipeline(
                "sentiment-analysis",
                model=model_name,
                tokenizer=model_name,
                max_length=512,   # 金融新闻标题通常较短，512 足够
                truncation=True,  # 超长文本自动截断
            )
        except Exception as e:
            logger.warning(f"Failed to load sentiment model, using fallback: {e}")
            _sentiment_pipeline = _SimpleFallbackAnalyzer()
    return _sentiment_pipeline


class _SimpleFallbackAnalyzer:
    """
    关键词匹配情感分析器（模型不可用时的降级方案）。

    通过预设的正负面关键词词典进行简单计数评分。
    这不是精确的情感分析，但保证了在没有模型时功能的连续性。

    【扩展点】可扩充 POSITIVE_WORDS 和 NEGATIVE_WORDS 词典，
    以覆盖更多金融领域常见表达。
    """

    POSITIVE_WORDS = {
        "涨", "增长", "利好", "突破", "盈利", "增长", "升", "牛",
        "up", "gain", "profit", "growth", "bull", "breakthrough"
    }
    NEGATIVE_WORDS = {
        "跌", "下降", "利空", "亏损", "风险", "降", "熊", "暴跌",
        "down", "loss", "risk", "bear", "decline", "crash"
    }

    def __call__(self, texts, **_kwargs):
        """
        对文本列表进行关键词情感评分。

        评分逻辑：
          - 正关键词数 > 负关键词数 → positive
          - 负关键词数 > 正关键词数 → negative
          - 相等 → neutral

        Args:
            texts: 待分析文本列表

        Returns:
            [{"label": ..., "score": ...}, ...] 格式的结果列表
        """
        results = []
        for text in texts:
            pos_count = sum(1 for w in self.POSITIVE_WORDS if w in text)
            neg_count = sum(1 for w in self.NEGATIVE_WORDS if w in text)
            if pos_count > neg_count:
                results.append({"label": "positive", "score": 0.6 + 0.1 * min(pos_count, 4)})
            elif neg_count > pos_count:
                results.append({"label": "negative", "score": 0.6 + 0.1 * min(neg_count, 4)})
            else:
                results.append({"label": "neutral", "score": 0.7})
        return results


def analyze(news_list: list[NewsItem]) -> list[NewsItem]:
    """
    对新闻列表进行情感分析，填充 sentiment 和 confidence 字段。

    处理流程：
      1. 提取所有新闻标题
      2. 批量送入 FinBERT pipeline 推理
      3. 将模型输出的标签映射到统一格式 (positive/negative/neutral)
      4. 填充回 NewsItem 的 sentiment 和 confidence 字段

    标签映射规则（兼容不同模型的输出格式）：
      - LABEL_0 / NEGATIVE / negative → "negative"
      - LABEL_1 / NEUTRAL / neutral  → "neutral"
      - LABEL_2 / POSITIVE / positive → "positive"

    Args:
        news_list: 待分析的新闻列表（可空）

    Returns:
        已填充 sentiment 和 confidence 的 NewsItem 列表
    """
    if not news_list:
        return []

    pipeline = _get_pipeline()
    texts = [n.title for n in news_list]

    try:
        # 批量推理：一次传入所有标题，利用 GPU 并行加速
        raw_results = pipeline(texts)
    except Exception as e:
        logger.error(f"Sentiment analysis failed: {e}")
        return news_list

    # 将每个结果映射回对应的 NewsItem
    for item, result in zip(news_list, raw_results):
        label = result.get("label", "neutral")
        score = result.get("score", 0.0)
        # 兼容不同模型的标签格式
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
    """
    汇总情感分析结果，生成统计摘要。

    计算指标：
      - total:          新闻总数
      - positive:       积极新闻数
      - negative:       消极新闻数
      - neutral:        中性新闻数
      - sentiment_score: 综合情感得分（范围 -1.0 ~ 1.0）
      - summary:        自然语言摘要
      - top_news:       前 5 条新闻标题（带情感标识）

    综合情感得分计算：
      sentiment_score = (positive - negative) / total
      > 0.3   → 偏正面
      < -0.3  → 偏负面
      之间    → 中性偏均衡

    Args:
        news_list: 已完成情感分析的新闻列表

    Returns:
        包含统计信息的字典，直接传入报告生成模块
    """
    if not news_list:
        return {
            "total": 0,
            "positive": 0,
            "negative": 0,
            "neutral": 0,
            "sentiment_score": 0.0,
            "summary": "暂无相关新闻数据。",
        }

    pos = sum(1 for n in news_list if n.sentiment == "positive")
    neg = sum(1 for n in news_list if n.sentiment == "negative")
    neu = sum(1 for n in news_list if n.sentiment == "neutral")
    total = len(news_list)

    # 综合情感得分: [-1, 1]，正值偏积极，负值偏消极
    if total > 0:
        score = (pos - neg) / total
    else:
        score = 0.0

    # 根据得分生成中文摘要
    if score > 0.3:
        summary = f"近期新闻整体偏正面，积极新闻占比 {pos/total*100:.1f}%。市场情绪较为乐观。"
    elif score < -0.3:
        summary = f"近期新闻整体偏负面，消极新闻占比 {neg/total*100:.1f}%。市场情绪偏谨慎。"
    else:
        summary = f"近期新闻整体中性偏均衡，正面 {pos/total*100:.1f}%，负面 {neg/total*100:.1f}%。"

    # 提取前 5 条新闻标题，带情感标识
    top_news = []
    for n in news_list[:5]:
        emoji = {"positive": "🟢", "negative": "🔴", "neutral": "⚪"}.get(n.sentiment, "⚪")
        top_news.append(f"- {emoji} [{n.date}] {n.title}")

    return {
        "total": total,
        "positive": pos,
        "negative": neg,
        "neutral": neu,
        "sentiment_score": round(score, 4),
        "summary": summary,
        "top_news": "\n".join(top_news),
    }
