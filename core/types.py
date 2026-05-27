"""
核心数据类型定义。

项目中跨模块传递的核心数据结构，使用 dataclass 保证类型安全。
"""

from dataclasses import dataclass, field


@dataclass
class AlphaStats:
    """Alpha 因子得分统计。"""
    mean: float = 0.0
    std: float = 0.0
    latest: float = 0.0


@dataclass
class NewsAggregation:
    """新闻情感汇总。"""
    total: int = 0
    positive: int = 0
    negative: int = 0
    neutral: int = 0
    sentiment_score: float = 0.0
    summary: str = ""
    top_news: str = ""
