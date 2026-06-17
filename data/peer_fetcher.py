"""
同类股票/同板块获取模块。

美股优先使用 Finnhub /stock/peers，A 股优先使用 baostock 行业分类 +
akshare 板块成分股。遵循数据源优先级：美股 Finnhub+ TickFlow → yfinance
（兜底），A 股 baostock+ TickFlow → akshare（兜底）。

同级函数 fetch_peers() 按市场自动分发。
"""

import logging
from typing import Optional

from config.settings import Settings

logger = logging.getLogger(__name__)


def fetch_peers(
    code: str,
    market: str,
    industry: str = "",
    limit: int = 10,
) -> list[dict]:
    """
    获取同行业/同类股票列表。

    Args:
        code:     股票代码
        market:   "A" / "US"
        industry: 行业名称（A 股必需，由 baostock 提前获取）
        limit:    最多返回数量（含自身过滤）

    Returns:
        [{"code": str, "name": str, "market": str}, ...]
    """
    if market == "US":
        return _fetch_us_peers(code, limit)
    elif market == "A":
        return _fetch_a_peers(code, industry, limit)
    else:
        logger.warning(f"不支持的市场: {market}")
        return []


# ═══════════════════════════════════════════════════════════════
# 美股 — Finnhub /stock/peers（优先）
# ═══════════════════════════════════════════════════════════════


def _fetch_us_peers(code: str, limit: int = 10) -> list[dict]:
    """美股同类股：Finnhub /stock/peers → 代码列表。"""
    settings = Settings()
    token = (settings.get("news_token_us", "") or "").strip()
    if not token:
        logger.warning("未配置 news_token_us，无法获取美股同类股")
        return []

    try:
        from data.finnhub_client import fetch_peers as finnhub_peers
        tickers = finnhub_peers(token, code)
    except Exception as e:
        logger.warning(f"Finnhub peers 获取失败 ({code}): {e}")
        return []

    if not tickers:
        return []

    # 过滤掉自身
    peers = [t for t in tickers if t.upper() != code.upper()]
    logger.info(f"美股同类股 ({code}): {len(peers)} 只（已去自身）")

    # 构建结果（名称先留空，后续由 peer 分析时补齐）
    results: list[dict] = []
    for ticker in peers[:limit]:
        results.append({"code": ticker.upper(), "name": "", "market": "US"})
    return results


# ═══════════════════════════════════════════════════════════════
# A 股 — baostock 行业分类 + akshare 板块成分股（优先 baostock）
# ═══════════════════════════════════════════════════════════════


def _fetch_a_peers(code: str, industry: str = "", limit: int = 10) -> list[dict]:
    """
    A 股同板块标的。

    优先路径：baostock 获取行业名 → akshare 拉板块成分股。
    baostock 仅能查询单只股票的行业分类，无法直接列出同行业所有股票，
    因此第二步仍需 akshare 补齐（唯一可行路径）。

    Args:
        code:     股票代码
        industry: 行业名称（如"半导体"）— 由调用方通过 baostock 提前获取
    """
    if not industry:
        logger.warning(f"A 股 {code} 未提供行业名称，无法获取同板块")
        return []

    try:
        import akshare as ak
        df = ak.stock_board_industry_cons_em(symbol=industry)
        if df is None or df.empty:
            logger.warning(f"akshare 板块成分股为空 (行业={industry})")
            return []
    except Exception as e:
        logger.warning(f"akshare 板块成分股获取失败 (行业={industry}): {e}")
        return []

    # akshare 返回列：代码、名称、最新价、涨跌幅 等
    code_col = _find_column(df, ["代码", "code", "symbol"])
    name_col = _find_column(df, ["名称", "name", "股票名称"])

    if not code_col:
        logger.warning(f"akshare 板块成分股缺少代码列，可用列: {list(df.columns)}")
        return []

    results: list[dict] = []
    for _, row in df.iterrows():
        peer_code = str(row[code_col]).strip()
        # 过滤自身 + 仅保留 6 位数字代码
        if peer_code == code or not (peer_code.isdigit() and len(peer_code) == 6):
            continue
        peer_name = str(row[name_col]).strip() if name_col else peer_code
        results.append({"code": peer_code, "name": peer_name, "market": "A"})
        if len(results) >= limit:
            break

    logger.info(f"A 股同板块 ({code}, 行业={industry}): {len(results)} 只")
    return results


def _find_column(df, candidates: list[str]) -> Optional[str]:
    """在 DataFrame 列中查找第一个匹配的列名。"""
    for col in candidates:
        if col in df.columns:
            return col
    return None
