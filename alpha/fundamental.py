"""
基本面与估值因子 — akshare 真实数据优先，LLM 兜底。

股票数据源：
  - A 股：akshare stock_value_em（PE/PB 3年历史）+ stock_financial_analysis_indicator（财务）
  - 美股：Finnhub /stock/metric（优先）→ akshare（兜底）→ LLM 估算
"""

import logging
from datetime import date, timedelta

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def fetch_fundamental_factors(
    name: str, code: str, market: str,
    model: str = "", base_url: str = "", api_key: str = "",
    finnhub_token: str = "",
) -> dict:
    """
    获取估值和基本面因子。美股优先 Finnhub，A 股 akshare，LLM 兜底。

    Returns:
        {style_factors, fundamental_factors, source}
    """
    # ── 美股：优先 Finnhub /stock/metric ──
    if market == "US" and finnhub_token:
        try:
            from data.finnhub_client import fetch_basic_metrics
            metrics = fetch_basic_metrics(finnhub_token, code)
            if metrics:
                fin = _extract_financials_from_finnhub(metrics)
                pe_pb = _extract_pe_pb_from_finnhub(metrics)
                if fin and pe_pb:
                    style = _calc_style_factors(pe_pb)
                    logger.info(
                        f"基本面(Finnhub): PE分位={style['pe_percentile']:.1%}, "
                        f"PB分位={style['pb_percentile']:.1%}, ROE={fin['roe']:.1%}"
                    )
                    return {"style_factors": style, "fundamental_factors": fin, "source": "finnhub"}
        except Exception as e:
            logger.warning(f"Finnhub 基本面获取失败 ({code}): {e}")

    # ── A 股：baostock 优先，akshare 兜底 ──
    if market == "A":
        financials = _fetch_financials_baostock(code)
        if financials is None:
            financials = _fetch_financials_akshare(code, market)

        pe_pb = _fetch_pe_pb_baostock(code)
        if pe_pb is None:
            pe_pb = _fetch_pe_pb_akshare(code, market)

        style = _calc_style_factors(pe_pb) if pe_pb else {"pe_percentile": 0.5, "pb_percentile": 0.5}
        fin = financials or {"roe": 0, "gross_margin": 0, "debt_ratio": 0,
                             "net_profit_yoy": 0, "revenue_yoy": 0}

        if financials and pe_pb:
            source = "baostock"
        elif pe_pb:
            source = "baostock(估值)" if _fetch_pe_pb_baostock.__name__ else "akshare(估值)"
        elif financials:
            source = "baostock(财务)" if _fetch_financials_baostock.__name__ else "akshare(财务)"
        else:
            if api_key:
                logger.info("基本面全部缺失，LLM 兜底")
                try:
                    from alpha.fundamental_llm import fetch_fundamental_factors_llm
                    return fetch_fundamental_factors_llm(name, code, market, model, base_url, api_key)
                except Exception as e:
                    logger.warning(f"LLM 基本面兜底失败: {e}")
            source = "default"

        logger.info(
            f"基本面({source}): PE分位={style['pe_percentile']:.1%}, "
            f"PB分位={style['pb_percentile']:.1%}, ROE={fin['roe']:.1%}"
        )
        return {"style_factors": style, "fundamental_factors": fin, "source": source}

    # ── 美股：Finnhub → akshare ──
    financials = _fetch_financials_akshare(code, market)
    pe_pb = _fetch_pe_pb_akshare(code, market)

    # PE/PB 估值分位 + 财务指标各自独立取，各自缺失互不影响
    style = _calc_style_factors(pe_pb) if pe_pb else {"pe_percentile": 0.5, "pb_percentile": 0.5}
    fin = financials or {"roe": 0, "gross_margin": 0, "debt_ratio": 0,
                         "net_profit_yoy": 0, "revenue_yoy": 0}

    if financials and pe_pb:
        source = "akshare"
    elif pe_pb:
        source = "akshare(估值)"
    elif financials:
        source = "akshare(财务)"
    else:
        # 全部缺失 → LLM 兜底
        if api_key:
            logger.info("基本面全部缺失，LLM 兜底")
            try:
                from alpha.fundamental_llm import fetch_fundamental_factors_llm
                return fetch_fundamental_factors_llm(name, code, market, model, base_url, api_key)
            except Exception as e:
                logger.warning(f"LLM 基本面兜底失败: {e}")
        source = "default"

    _baostock_logout()
    logger.info(
        f"基本面({source}): PE分位={style['pe_percentile']:.1%}, "
        f"PB分位={style['pb_percentile']:.1%}, ROE={fin['roe']:.1%}"
    )
    return {"style_factors": style, "fundamental_factors": fin, "source": source}


# ── Finnhub /stock/metric 解析 ──


def _extract_financials_from_finnhub(metrics: dict) -> dict | None:
    """
    从 Finnhub /stock/metric?metric=all 提取财务指标。

    metric 字段常用 key（Finnhub 命名规范）：
      - roeRfy              → ROE
      - grossMarginTTM      → 毛利率
      - totalDebt/totalEquity → 负债比率（需计算）
      - roaRfy              → ROA（备用）
      - revenueGrowthTTMYoy → 营收增速（可能不存在，需从 series 算）
      - epsGrowthTTMYoy     → 利润增速（替代 net_profit_yoy）

    返回 None 表示关键字段缺失过多。
    """
    m = metrics.get("metric", {})
    if not m:
        return None

    roe = _safe_float(m.get("roeRfy"))
    gross_margin = _safe_float(m.get("grossMarginTTM"))

    # Finnhub 返回的 ROE/grossMargin 等百分比字段是"数值百分数"（如 76.33 = 76.33%），
    # 需要除以 100 转为小数（0.7633），以便跟 akshare 的数据格式保持一致。
    if roe > 1:
        roe = roe / 100
    if gross_margin > 1:
        gross_margin = gross_margin / 100

    # 资产负债比率：totalDebt / totalEquity
    debt = _safe_float(m.get("totalDebt"))
    equity = _safe_float(m.get("totalEquity"))
    debt_ratio = debt / equity if equity and equity > 0 else 0.0

    # 增速 — Finnhub 不直接提供 YoY，尝试从 series 推算或用 eps 增长
    net_profit_yoy = _safe_float(m.get("epsGrowthTTMYoy"))
    revenue_yoy = _safe_float(m.get("revenueGrowthTTMYoy"))

    # Finnhub 百分比字段归一化
    if net_profit_yoy > 1:
        net_profit_yoy = net_profit_yoy / 100
    if revenue_yoy > 1:
        revenue_yoy = revenue_yoy / 100

    if roe == 0 and gross_margin == 0:
        return None

    logger.info(
        f"Finnhub 财务: ROE={roe:.1%}, margin={gross_margin:.1%}, "
        f"debt={debt_ratio:.1%}, np_yoy={net_profit_yoy:.1%}, rev_yoy={revenue_yoy:.1%}"
    )
    return {
        "roe": roe,
        "gross_margin": gross_margin,
        "debt_ratio": debt_ratio,
        "net_profit_yoy": net_profit_yoy,
        "revenue_yoy": revenue_yoy,
    }


def _extract_pe_pb_from_finnhub(metrics: dict) -> list[dict] | None:
    """
    从 Finnhub /stock/metric?metric=all 提取 PE/PB 历史。

    series 字段结构：
      {
        "annual": {
          "peBasicExclExtraTTM": [
            {"period": "2024-12-31", "v": 32.5},
            {"period": "2023-12-31", "v": 28.1},
            ...
          ],
          "pbAnnual": [...]
        }
      }

    构造为 [{"date": "2024-12-31", "pe": 32.5, "pb": 3.2}, ...] 格式。
    """
    m = metrics.get("metric", {})
    series = metrics.get("series", {})

    pe_series = series.get("annual", {}).get("peBasicExclExtraTTM", [])
    pb_series = series.get("annual", {}).get("pbAnnual", [])

    if not pe_series and not pb_series:
        # 只有当前值，构造单条记录
        pe_current = _safe_float(m.get("peBasicExclExtraTTM"))
        pb_current = _safe_float(m.get("pbAnnual"))
        if pe_current > 0 or pb_current > 0:
            return [{"date": date.today().isoformat(), "pe": pe_current, "pb": pb_current}]
        return None

    # 对齐日期
    pe_map = {item["period"][:10]: item["v"] for item in pe_series if item.get("v")}
    pb_map = {item["period"][:10]: item["v"] for item in pb_series if item.get("v")}

    all_dates = sorted(set(list(pe_map.keys()) + list(pb_map.keys())))
    result = []
    for d in all_dates:
        pe = pe_map.get(d, np.nan)
        pb = pb_map.get(d, np.nan)
        if not (np.isnan(pe) and np.isnan(pb)):
            result.append({"date": d, "pe": pe, "pb": pb})

    return result if result else None


def _safe_float(val) -> float:
    """安全转换浮点数。"""
    if val is None:
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


# ── baostock 数据获取（A 股基本面，优先） ──


_baostock_logged_in = False


def _baostock_login():
    """baostock 全局登录（只登一次，后续调用跳过）。"""
    global _baostock_logged_in
    if _baostock_logged_in:
        return True
    import baostock as bs
    try:
        lg = bs.login()
        if lg.error_code == '0':
            _baostock_logged_in = True
            return True
    except Exception:
        pass
    return False


def _baostock_logout():
    """baostock 登出。"""
    global _baostock_logged_in
    try:
        import baostock as bs
        bs.logout()
    finally:
        _baostock_logged_in = False


def _fetch_financials_baostock(code: str) -> dict | None:
    """通过 baostock 获取 A 股财务指标（ROE、毛利率、资产负债率、增速）。"""
    import baostock as bs
    try:
        if not _baostock_login():
            return None

        symbol = f"sh.{code}" if code.startswith(("6", "5", "9")) else f"sz.{code}"

        # 盈利能力
        profit = bs.query_profit_data(code=symbol, year=2025, quarter=4)
        roe = gross_margin = 0.0
        revenue_2025 = revenue_2024 = 0.0
        if profit.error_code == '0':
            while profit.next():
                row = profit.get_row_data()
                d = dict(zip(profit.fields, row))
                roe = _safe_float(d.get("roeAvg"))
                gross_margin = _safe_float(d.get("gpMargin"))
                revenue_2025 = _safe_float(d.get("MBRevenue"))

        # 去年营收（算同比）
        profit_prev = bs.query_profit_data(code=symbol, year=2024, quarter=4)
        if profit_prev.error_code == '0':
            while profit_prev.next():
                row = profit_prev.get_row_data()
                d = dict(zip(profit_prev.fields, row))
                revenue_2024 = _safe_float(d.get("MBRevenue"))

        # 偿债能力
        balance = bs.query_balance_data(code=symbol, year=2025, quarter=4)
        debt_ratio = 0.0
        if balance.error_code == '0':
            while balance.next():
                row = balance.get_row_data()
                d = dict(zip(balance.fields, row))
                debt_ratio = _safe_float(d.get("liabilityToAsset"))

        # 成长能力
        growth = bs.query_growth_data(code=symbol, year=2025, quarter=4)
        net_profit_yoy = 0.0
        if growth.error_code == '0':
            while growth.next():
                row = growth.get_row_data()
                d = dict(zip(growth.fields, row))
                net_profit_yoy = _safe_float(d.get("YOYNI"))

        revenue_yoy = ((revenue_2025 - revenue_2024) / revenue_2024
                       if revenue_2024 > 0 else 0.0)

        if roe == 0 and gross_margin == 0 and debt_ratio == 0:
            return None

        logger.info(
            f"baostock 财务: ROE={roe:.1%}, margin={gross_margin:.1%}, "
            f"debt={debt_ratio:.1%}, np_yoy={net_profit_yoy:.1%}, rev_yoy={revenue_yoy:.1%}"
        )
        return {
            "roe": roe, "gross_margin": gross_margin, "debt_ratio": debt_ratio,
            "net_profit_yoy": net_profit_yoy, "revenue_yoy": revenue_yoy,
        }

    except Exception as e:
        logger.warning(f"baostock 财务获取失败 ({code}): {e}")
        return None


def _fetch_pe_pb_baostock(code: str) -> list[dict] | None:
    """通过 baostock 获取 A 股 PE/PB 3 年日线历史。"""
    import baostock as bs
    try:
        if not _baostock_login():
            return None

        symbol = f"sh.{code}" if code.startswith(("6", "5", "9")) else f"sz.{code}"
        from datetime import date, timedelta

        end_date = date.today().strftime("%Y-%m-%d")
        start_date = (date.today() - timedelta(days=1095)).strftime("%Y-%m-%d")

        rs = bs.query_history_k_data_plus(
            symbol, "date,peTTM,pbMRQ",
            start_date=start_date, end_date=end_date,
            frequency="d", adjustflag="2",
        )
        if rs.error_code != '0':
            logger.warning(f"baostock PE/PB 查询失败: {rs.error_msg}")
            return None

        result = []
        while rs.next():
            row = rs.get_row_data()
            d = dict(zip(rs.fields, row))
            pe = _safe_float(d.get("peTTM"))
            pb = _safe_float(d.get("pbMRQ"))
            if pe == 0 and pb == 0:
                continue
            result.append({"date": d["date"], "pe": pe, "pb": pb})

        if len(result) < 3:
            return None
        logger.info(f"baostock PE/PB: {len(result)} 条 ({result[0]['date']}~{result[-1]['date']})")
        return result

    except Exception as e:
        logger.warning(f"baostock PE/PB 获取失败 ({code}): {e}")
        return None


def _fetch_stock_industry_baostock(code: str) -> str:
    """通过 baostock 获取 A 股行业分类。"""
    import baostock as bs
    try:
        if not _baostock_login():
            return ""
        symbol = f"sh.{code}" if code.startswith(("6", "5", "9")) else f"sz.{code}"
        rs = bs.query_stock_industry(code=symbol)
        if rs.error_code == '0':
            while rs.next():
                row = rs.get_row_data()
                result = row[3] if len(row) > 3 else ""
                _baostock_logout()
                return result
    except Exception:
        pass
    _baostock_logout()
    return ""


# ── akshare 数据获取（兜底） ──


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
            # akshare 版本差异：旧版列名"日期"，新版"数据日期"
            col_map = {"日期": "date", "数据日期": "date", "PE(TTM)": "pe", "市净率": "pb"}
        else:
            return _fetch_pe_pb_us_baidu(code)

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


def _fetch_pe_pb_us_baidu(code: str) -> list[dict] | None:
    """美股 PE/PB：直接用 requests 调百度 API（绕过 akshare 不跟进 301 的 bug）。"""
    import requests, urllib.parse

    indicators = ["市盈率(TTM)", "市净率"]
    result = {}
    for indicator in indicators:
        try:
            params = {
                "openapi": "1", "dspName": "iphone", "tn": "tangram",
                "client": "app", "query": indicator, "code": code,
                "resource_id": "51171", "market": "us", "tag": indicator,
                "chart_select": "近三年", "skip_industry": "1", "finClientType": "pc",
            }
            url = f"https://gushitong.baidu.com/opendata?{urllib.parse.urlencode(params)}"
            resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            data = resp.json()
            body = data["Result"][0]["DisplayData"]["resultData"]["tplData"]["result"]["chartInfo"][0]["body"]
            for row in body:
                d = row[0]
                if d not in result:
                    result[d] = {}
                result[d]["date"] = d
                result[d]["pe" if indicator == "市盈率(TTM)" else "pb"] = row[1]
        except Exception as e:
            logger.warning(f"百度 {indicator} 获取失败 ({code}): {e}")

    if not result:
        return None
    items = [v for v in result.values() if v.get("pe") and v.get("pb")]
    items.sort(key=lambda x: str(x["date"]))
    return items


# ── 得分计算 ──


def _calc_style_factors(pe_pb_history: list[dict]) -> dict:
    # 最低 3 条（Finnhub 年数据）或 12 条（akshare 月数据）
    min_entries = 3
    if not pe_pb_history or len(pe_pb_history) < min_entries:
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
