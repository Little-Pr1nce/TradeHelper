"""
基本面与估值因子 — akshare 真实数据优先，LLM 兜底。

股票数据源：
  - A 股：akshare stock_value_em（PE/PB 3年历史）+ stock_financial_analysis_indicator（财务）
  - 美股：akshare stock_financial_us_analysis_indicator_em（财务）
          PE/PB 历史通过 LLM 补充（akshare stock_us_valuation_baidu 不稳定）
"""

import logging
from datetime import date, timedelta

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def fetch_fundamental_factors(
    name: str, code: str, market: str,
    model: str = "", base_url: str = "", api_key: str = "",
) -> dict:
    """
    获取估值和基本面因子。akshare 优先，LLM 兜底。

    Returns:
        {style_factors, fundamental_factors, source}
    """
    # 先尝试 akshare
    financials = _fetch_financials_akshare(code, market)
    pe_pb = _fetch_pe_pb_akshare(code, market)

    if financials and pe_pb:
        style = _calc_style_factors(pe_pb)
        logger.info(
            f"基本面(akshare): PE分位={style['pe_percentile']:.1%}, "
            f"PB分位={style['pb_percentile']:.1%}, ROE={financials['roe']:.1%}"
        )
        return {"style_factors": style, "fundamental_factors": financials, "source": "akshare"}

    # 部分缺失 → LLM 兜底
    if api_key:
        logger.info(f"akshare 数据不全(fin={bool(financials)}, pe_pb={bool(pe_pb)})，LLM 兜底")
        from alpha.fundamental_llm import fetch_fundamental_factors_llm
        return fetch_fundamental_factors_llm(name, code, market, model, base_url, api_key)

    # 用已有数据 + 默认值
    style = _calc_style_factors(pe_pb) if pe_pb else {"pe_percentile": 0.5, "pb_percentile": 0.5}
    fin = financials or {"roe": 0, "gross_margin": 0, "debt_ratio": 0,
                         "net_profit_yoy": 0, "revenue_yoy": 0}
    logger.warning("基本面数据部分缺失，使用默认值")
    return {"style_factors": style, "fundamental_factors": fin, "source": "partial"}


# ── akshare 数据获取 ──


def _fetch_financials_akshare(code: str, market: str) -> dict | None:
    """通过 akshare 获取财务指标。"""
    try:
        import akshare as ak

        if market == "A":
            df = ak.stock_financial_analysis_indicator(symbol=code)
            if df is None or df.empty:
                return None
            last = df.iloc[-1]
            return {
                "roe": _pct(last.get("净资产收益率")),
                "gross_margin": _pct(last.get("销售毛利率")),
                "debt_ratio": _pct(last.get("资产负债率")),
                "net_profit_yoy": _pct(last.get("净利润同比增长率")),
                "revenue_yoy": _pct(last.get("营业收入同比增长率")),
            }
        else:
            df = ak.stock_financial_us_analysis_indicator_em(symbol=code, indicator="年报")
            if df is None or df.empty:
                return None
            last = df.iloc[-1]
            return {
                "roe": _pct(last.get("ROE_AVG")) or _pct(last.get("ROE_AVG", 0)),
                "gross_margin": _pct(last.get("GROSS_PROFIT_RATIO", 0)),
                "debt_ratio": _pct(last.get("DEBT_ASSET_RATIO", 0)),
                "net_profit_yoy": _pct(last.get("PARENT_HOLDER_NETPROFIT_YOY", 0)),
                "revenue_yoy": _pct(last.get("OPERATE_INCOME_YOY", 0)),
            }
    except Exception as e:
        logger.warning(f"akshare 财务获取失败 ({code}): {e}")
        return None


def _fetch_pe_pb_akshare(code: str, market: str) -> list[dict] | None:
    """通过 akshare 获取 PE/PB 3 年历史。"""
    try:
        import akshare as ak

        if market == "A":
            df = ak.stock_value_em(symbol=code)
            if df is None or df.empty:
                return None
            col_map = {"日期": "date", "PE(TTM)": "pe", "市净率": "pb"}
        else:
            # 美股 PE/PB 历史：stock_us_valuation_baidu 不稳定，跳过
            return None

        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
        if "date" not in df.columns:
            return None

        df["date"] = pd.to_datetime(df["date"])
        cutoff = date.today() - timedelta(days=1095)
        df = df[df["date"] >= pd.Timestamp(cutoff)]
        if df.empty:
            return None

        return [{
            "date": str(r["date"])[:10],
            "pe": float(r.get("pe", np.nan) or np.nan),
            "pb": float(r.get("pb", np.nan) or np.nan),
        } for _, r in df.iterrows()]

    except Exception as e:
        logger.warning(f"akshare PE/PB 获取失败 ({code}): {e}")
        return None


# ── 得分计算 ──


def _calc_style_factors(pe_pb_history: list[dict]) -> dict:
    if not pe_pb_history or len(pe_pb_history) < 12:
        return {"pe_percentile": 0.5, "pb_percentile": 0.5}
    pes = [d["pe"] for d in pe_pb_history if d.get("pe") and not np.isnan(d["pe"])]
    pbs = [d["pb"] for d in pe_pb_history if d.get("pb") and not np.isnan(d["pb"])]
    if not pes or not pbs:
        return {"pe_percentile": 0.5, "pb_percentile": 0.5}
    return {
        "pe_percentile": round(sum(1 for p in pes if p < pes[-1]) / len(pes), 4),
        "pb_percentile": round(sum(1 for p in pbs if p < pbs[-1]) / len(pbs), 4),
    }


def score_style_factor(pe_pct: float, pb_pct: float) -> float:
    return round(np.tanh((0.5 - (pe_pct + pb_pct) / 2) * 4), 4)


def score_fundamental_factor(
    roe: float, gross_margin: float, debt_ratio: float,
    net_profit_yoy: float, revenue_yoy: float,
) -> float:
    scores = [
        np.tanh((roe - 0.10) * 8),
        np.tanh((gross_margin - 0.30) * 5),
        np.tanh((0.40 - debt_ratio) * 5),
        np.tanh(net_profit_yoy * 4),
        np.tanh(revenue_yoy * 4),
    ]
    return round(float(np.mean(scores)), 4)


def _pct(val) -> float:
    """安全转换百分比值：50.5 → 0.505，字符串也处理。"""
    if val is None:
        return 0.0
    try:
        v = float(val)
        return v / 100 if abs(v) > 1 else v
    except (ValueError, TypeError):
        return 0.0
