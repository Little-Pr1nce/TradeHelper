"""
实时盘口因子。

当前行情源统一为 TickFlow；TickFlow 目前未暴露独立盘口深度接口，
因此盘口因子默认不可用，并在 Alpha 权重中自动回退。
"""

import logging

logger = logging.getLogger(__name__)


def fetch_depth_factor(code: str, market: str, token: str) -> dict:
    """返回盘口因子默认回退结果。"""
    logger.info(f"盘口深度暂不可用 ({market}:{code})，使用中性盘口因子")
    return _empty_result()


def _empty_result() -> dict:
    return {
        "depth_score": 0.0,
        "bid_volume": 0,
        "ask_volume": 0,
        "imbalance": 1.0,
        "available": False,
    }
