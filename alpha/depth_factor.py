"""
实时盘口因子 — 通过 itick /stock/depth 获取买卖力量对比。

计算买盘/卖盘不平衡比例，映射为 [-1, +1] 的短期方向信号。
买压 > 卖压 → 偏多，卖压 > 买压 → 偏空。

仅 itick 数据源支持（免费数据源无此接口）。
"""

import logging

import numpy as np

logger = logging.getLogger(__name__)


def fetch_depth_factor(
    code: str, market: str, token: str,
) -> dict:
    """
    从 itick 获取实时盘口数据，计算买卖力量因子。

    Args:
        code: 股票代码
        market: A/US
        token: itick API token

    Returns:
        {
            "depth_score": float,        # 盘口因子得分 [-1, +1]
            "bid_volume": float,         # 买盘总量
            "ask_volume": float,         # 卖盘总量
            "imbalance": float,          # 买/卖比例
            "available": bool,           # 数据是否可用
        }
    """
    from utils.market import detect_market
    market_type = detect_market(code) or market
    region = _a_stock_region(code) if market_type == "A" else "US"

    try:
        import requests
        resp = requests.get(
            "https://api0.itick.org/stock/depth",
            params={"region": region, "code": code},
            headers={"accept": "application/json", "token": token},
            timeout=15,
        )
        data = resp.json()
        if data.get("code") != 0:
            logger.warning(f"itick depth API 错误: {data.get('msg')}")
            return _empty_result()

        depth = data.get("data", {})
        bids = depth.get("b", [])
        asks = depth.get("a", [])

        if not bids or not asks:
            logger.info(f"盘口数据为空 ({code})")
            return _empty_result()

        # 计算买卖总量
        bid_vol = sum(b.get("v", 0) for b in bids)
        ask_vol = sum(a.get("v", 0) for a in asks)

        if ask_vol <= 0:
            return _empty_result()

        # 不平衡比例
        imbalance = bid_vol / ask_vol

        # 映射到 [-1, +1]：imbalance=1.0(平衡)→0, >2.0→+1, <0.5→-1
        depth_score = round(np.tanh((imbalance - 1.0) * 2), 4)

        logger.info(
            f"盘口因子 ({code}): bid={bid_vol:.0f} ask={ask_vol:.0f} "
            f"imbalance={imbalance:.2f} score={depth_score:+.3f}"
        )

        return {
            "depth_score": depth_score,
            "bid_volume": round(bid_vol, 0),
            "ask_volume": round(ask_vol, 0),
            "imbalance": round(imbalance, 4),
            "available": True,
        }

    except Exception as e:
        logger.warning(f"盘口数据获取失败 ({code}): {e}")
        return _empty_result()


def _a_stock_region(code: str) -> str:
    """A 股代码 → itick region。"""
    if code.isdigit() and len(code) == 6:
        return "SH" if code[0] == "6" else "SZ"
    return "SH"


def _empty_result() -> dict:
    return {
        "depth_score": 0.0,
        "bid_volume": 0,
        "ask_volume": 0,
        "imbalance": 1.0,
        "available": False,
    }
