"""
基本面与估值因子。

美股数据源：Finnhub /stock/metric（优先）→ yfinance（降级）→ LLM 兜底
A 股数据源：baostock（优先）→ akshare（降级）→ LLM 兜底
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
    获取估值和基本面因子。美股优先 Finnhub，A 股优先 baostock，LLM 兜底。

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
                    # 负债比率：Finnhub 不返回 totalDebt/totalEquity 时用 yfinance 补齐
                    if fin.get("debt_ratio", 0) == 0:
                        try:
                            fin_yf, _ = _fetch_fundamentals_yfinance(code)
                            if fin_yf and fin_yf.get("debt_ratio", 0) > 0:
                                fin["debt_ratio"] = fin_yf["debt_ratio"]
                                logger.info(f"负债比率: yfinance 补齐 → {fin['debt_ratio']:.1%}")
                        except Exception:
                            pass

                    # PE/PB 分位需要至少 3 个历史点，Finnhub 只返回当前值 →
                    # 用百度股市通 3 年历史补齐分位计算
                    if len(pe_pb) < 3:
                        pe_pb_history = _fetch_pe_pb_us_baidu(code)
                        if pe_pb_history and len(pe_pb_history) >= 3:
                            # 百度提供 3 年历史，但它的当前 PE 是 TTM；
                            # 用 Finnhub 的当前值（含 forward PE）替换最后一条，
                            # 确保 PE 混合计算能拿到 forwardPE。
                            finnhub_current = pe_pb[-1]
                            pe_pb = pe_pb_history
                            pe_pb[-1] = finnhub_current
                            logger.info(
                                f"PE/PB 分位: Finnhub 当前值(PE={finnhub_current['pe']:.1f}, "
                                f"PB={finnhub_current['pb']:.1f}) "
                                f"+ 百度 {len(pe_pb_history)} 条历史"
                            )
                        else:
                            logger.info("PE/PB 分位数据不足（<3条），使用默认 50%")
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
        financials_source = ""
        pe_pb_source = ""

        financials = _fetch_financials_baostock(code)
        if financials is not None:
            financials_source = "baostock"
        if financials is None:
            logger.info(f"baostock 财务不可用，降级到 akshare ({code})")
            financials = _fetch_financials_akshare(code, market)
            if financials is not None:
                financials_source = "akshare"

        pe_pb = _fetch_pe_pb_baostock(code)
        if pe_pb is not None:
            pe_pb_source = "baostock"
        if pe_pb is None:
            logger.info(f"baostock PE/PB 不可用，降级到 akshare ({code})")
            pe_pb = _fetch_pe_pb_akshare(code, market)
            if pe_pb is not None:
                pe_pb_source = "akshare"

        style = _calc_style_factors(pe_pb) if pe_pb else {"pe_percentile": 0.5, "pb_percentile": 0.5}
        fin = financials or {"roe": 0, "gross_margin": 0, "debt_ratio": 0,
                             "net_profit_yoy": 0, "revenue_yoy": 0, "ev_ebitda": 0,
                             "gross_margin_5y": 0, "net_profit_yoy_5y": 0, "revenue_yoy_5y": 0}

        if financials and pe_pb:
            source = financials_source if financials_source == pe_pb_source else f"{pe_pb_source}(估值)+{financials_source}(财务)"
        elif pe_pb:
            source = f"{pe_pb_source}(估值)"
        elif financials:
            source = f"{financials_source}(财务)"
        else:
            if api_key:
                logger.info("基本面全部缺失，LLM 兜底")
                try:
                    from alpha.fundamental_llm import fetch_fundamental_factors_llm
                    _baostock_logout()
                    return fetch_fundamental_factors_llm(name, code, market, model, base_url, api_key)
                except Exception as e:
                    logger.warning(f"LLM 基本面兜底失败: {e}")
            source = "default"

        logger.info(
            f"基本面({source}): PE分位={style['pe_percentile']:.1%}, "
            f"PB分位={style['pb_percentile']:.1%}, ROE={fin['roe']:.1%}"
        )
        _baostock_logout()
        return {"style_factors": style, "fundamental_factors": fin, "source": source}

    # ── 美股：Finnhub → yfinance → akshare → LLM ──
    fin_from_yf = False
    pe_from_yf = False
    if market == "US":
        logger.info(f"Finnhub 不可用，降级到 yfinance ({code})")
        financials, pe_pb = _fetch_fundamentals_yfinance(code)
        fin_from_yf = financials is not None
        pe_from_yf = pe_pb is not None

    # yfinance 拿不到 → akshare 兜底
    if not financials:
        financials = _fetch_financials_akshare(code, market)
    if not pe_pb:
        pe_pb = _fetch_pe_pb_akshare(code, market)

    # PE/PB 估值分位 + 财务指标各自独立取，各自缺失互不影响
    style = _calc_style_factors(pe_pb) if pe_pb else {"pe_percentile": 0.5, "pb_percentile": 0.5}
    fin = financials or {"roe": 0, "gross_margin": 0, "debt_ratio": 0,
                         "net_profit_yoy": 0, "revenue_yoy": 0, "ev_ebitda": 0,
                         "gross_margin_5y": 0, "net_profit_yoy_5y": 0, "revenue_yoy_5y": 0}

    # 确定来源标签（美股 yfinance + akshare 各自可能提供不同部分）
    fin_label = "yfinance" if fin_from_yf else "akshare"
    pe_label = "yfinance" if pe_from_yf else "akshare"
    if financials and pe_pb:
        source = fin_label if fin_label == pe_label else f"{pe_label}(估值)+{fin_label}(财务)"
    elif pe_pb:
        source = f"{pe_label}(估值)"
    elif financials:
        source = f"{fin_label}(财务)"
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
      - grossMarginAnnual   → 毛利率（年报值，比 TTM 更稳定，与券商口径一致）
      - totalDebt/totalEquity → 负债比率（需计算）
      - roaRfy              → ROA（备用）
      - revenueGrowthTTMYoy → 营收增速
      - epsGrowthTTMYoy     → 利润增速（替代 net_profit_yoy）

    返回 None 表示关键字段缺失过多。
    """
    m = metrics.get("metric", {})
    if not m:
        return None

    roe = _safe_float(m.get("roeRfy"))
    gross_margin = _safe_float(m.get("grossMarginAnnual")) or _safe_float(m.get("grossMarginTTM"))

    # Finnhub 返回的 ROE/grossMargin 等百分比字段是"数值百分数"（如 76.33 = 76.33%），
    # 需要除以 100 转为小数（0.7633），以便跟 akshare 的数据格式保持一致。
    if roe > 1:
        roe = roe / 100
    if gross_margin > 1:
        gross_margin = gross_margin / 100

    # Finnhub 的 metric 中 debt/equity 已经是归一化比率。直接使用该字段，
    # 避免把不存在或单位不一致的 totalDebt/totalEquity 误算成极端值。
    debt_ratio = (
        _safe_float(m.get("totalDebt/totalEquityAnnual"))
        or _safe_float(m.get("totalDebt/totalEquityQuarterly"))
        or _safe_float(m.get("longTermDebt/equityAnnual"))
        or _safe_float(m.get("longTermDebt/equityQuarterly"))
    )

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

    # 5 年均值作为历史基准参考（不参与评分，仅提供给 LLM 判断周期位置）
    gross_margin_5y = _safe_float(m.get("grossMargin5Y")) / 100 if _safe_float(m.get("grossMargin5Y")) > 1 else _safe_float(m.get("grossMargin5Y"))
    np_yoy_5y = _safe_float(m.get("epsGrowth5Y")) / 100 if _safe_float(m.get("epsGrowth5Y")) > 1 else _safe_float(m.get("epsGrowth5Y"))
    rev_yoy_5y = _safe_float(m.get("revenueGrowth5Y")) / 100 if _safe_float(m.get("revenueGrowth5Y")) > 1 else _safe_float(m.get("revenueGrowth5Y"))

    logger.info(
        f"Finnhub 财务: ROE={roe:.1%}, margin={gross_margin:.1%}(5Y均={gross_margin_5y:.1%}), "
        f"debt={debt_ratio:.1%}, np_yoy={net_profit_yoy:.1%}(5Y均={np_yoy_5y:.1%}), rev_yoy={revenue_yoy:.1%}(5Y均={rev_yoy_5y:.1%})"
    )
    # EV/EBITDA（企业价值/息税折旧摊销前利润）— 资本结构中性估值指标
    ev_ebitda = _safe_float(m.get("evToEbitdaTTM")) or _safe_float(m.get("evEbitdaAnnual"))

    return {
        "roe": roe,
        "gross_margin": gross_margin,
        "debt_ratio": debt_ratio,
        "net_profit_yoy": net_profit_yoy,
        "revenue_yoy": revenue_yoy,
        "ev_ebitda": ev_ebitda,
        # 5 年均值 — LLM 自主判断是否处于周期高位
        "gross_margin_5y": gross_margin_5y,
        "net_profit_yoy_5y": np_yoy_5y,
        "revenue_yoy_5y": rev_yoy_5y,
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
        pb_current = _safe_float(m.get("pb"))  # pb=当前市净率，pbAnnual 是年报值（偏低）
        pe_forward = _safe_float(m.get("forwardPE"))  # 远期 PE，用于混合 PE 计算
        if pe_current > 0 or pb_current > 0:
            item = {"date": date.today().isoformat(), "pe": pe_current, "pb": pb_current}
            if pe_forward > 0:
                item["pe_forward"] = pe_forward
            return [item]
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


def _empty_result(source: str = "default") -> dict:
    """统一的基本面缺省返回，供真实源和 LLM 兜底共用。"""
    return {
        "style_factors": {"pe_percentile": 0.5, "pb_percentile": 0.5},
        "fundamental_factors": {
            "roe": 0,
            "gross_margin": 0,
            "debt_ratio": 0,
            "net_profit_yoy": 0,
            "revenue_yoy": 0,
            "ev_ebitda": 0,
            "gross_margin_5y": 0,
            "net_profit_yoy_5y": 0,
            "revenue_yoy_5y": 0,
        },
        "source": source,
    }


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
        latest_year = _latest_baostock_report_year(bs, symbol, "profit")
        if latest_year is None:
            return None
        prev_year = _latest_baostock_report_year(bs, symbol, "profit", before_year=latest_year)

        # 盈利能力
        profit = bs.query_profit_data(code=symbol, year=latest_year, quarter=4)
        roe = gross_margin = 0.0
        revenue_latest = revenue_prev = 0.0
        if profit.error_code == '0':
            while profit.next():
                row = profit.get_row_data()
                d = dict(zip(profit.fields, row))
                roe = _safe_float(d.get("roeAvg"))
                gross_margin = _safe_float(d.get("gpMargin"))
                revenue_latest = _safe_float(d.get("MBRevenue"))

        # 去年营收（算同比）
        profit_prev = bs.query_profit_data(code=symbol, year=prev_year, quarter=4) if prev_year else None
        if profit_prev and profit_prev.error_code == '0':
            while profit_prev.next():
                row = profit_prev.get_row_data()
                d = dict(zip(profit_prev.fields, row))
                revenue_prev = _safe_float(d.get("MBRevenue"))

        # 偿债能力
        balance = bs.query_balance_data(code=symbol, year=latest_year, quarter=4)
        debt_ratio = 0.0
        if balance.error_code == '0':
            while balance.next():
                row = balance.get_row_data()
                d = dict(zip(balance.fields, row))
                debt_ratio = _safe_float(d.get("liabilityToAsset"))

        # 成长能力
        growth = bs.query_growth_data(code=symbol, year=latest_year, quarter=4)
        net_profit_yoy = 0.0
        if growth.error_code == '0':
            while growth.next():
                row = growth.get_row_data()
                d = dict(zip(growth.fields, row))
                net_profit_yoy = _safe_float(d.get("YOYNI"))

        revenue_yoy = ((revenue_latest - revenue_prev) / revenue_prev
                       if revenue_prev > 0 else 0.0)

        if roe == 0 and gross_margin == 0 and debt_ratio == 0:
            return None

        logger.info(
            f"baostock 财务({latest_year}Q4): ROE={roe:.1%}, margin={gross_margin:.1%}, "
            f"debt={debt_ratio:.1%}, np_yoy={net_profit_yoy:.1%}, rev_yoy={revenue_yoy:.1%}"
        )
        return {
            "roe": roe, "gross_margin": gross_margin, "debt_ratio": debt_ratio,
            "net_profit_yoy": net_profit_yoy, "revenue_yoy": revenue_yoy,
        }

    except Exception as e:
        logger.warning(f"baostock 财务获取失败 ({code}): {e}")
        return None


def _latest_baostock_report_year(bs, symbol: str, kind: str, before_year: int | None = None) -> int | None:
    """Find the latest year with a non-empty baostock annual report."""
    current_year = date.today().year
    start_year = min(before_year - 1, current_year) if before_year else current_year
    query_map = {
        "profit": bs.query_profit_data,
        "balance": bs.query_balance_data,
        "growth": bs.query_growth_data,
    }
    query = query_map[kind]
    for year in range(start_year, current_year - 8, -1):
        rs = query(code=symbol, year=year, quarter=4)
        if rs.error_code != '0':
            continue
        if rs.next():
            return year
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


def _fetch_stock_listing_date_baostock(code: str) -> str:
    """通过 baostock 股票基本资料获取 A 股上市日期。"""
    import baostock as bs
    try:
        if not _baostock_login():
            return ""
        symbol = f"sh.{code}" if code.startswith(("6", "5", "9")) else f"sz.{code}"
        rs = bs.query_stock_basic(code=symbol)
        if rs.error_code == "0" and rs.next():
            row = dict(zip(rs.fields, rs.get_row_data()))
            return str(row.get("ipoDate") or "")[:10]
    except Exception as exc:
        logger.warning(f"baostock 上市日期获取失败 ({code}): {exc}")
    return ""


# ── yfinance 数据获取（美股基本面降级） ──


def _fetch_fundamentals_yfinance(code: str) -> tuple[dict | None, list[dict] | None]:
    """从 yfinance Ticker.info 提取美股财务指标和 PE/PB 历史。

    Returns:
        (financials_dict | None, pe_pb_history | None)
        financials_dict: {roe, gross_margin, debt_ratio, net_profit_yoy, revenue_yoy}
        pe_pb_history: [{date, pe, pb}, ...]（单条，当前值）
    """
    try:
        import yfinance as yf

        ticker = yf.Ticker(code)
        info = ticker.info or {}

        if not info:
            logger.debug(f"yfinance info 为空 ({code})")
            return None, None

        # ── 财务指标 ──
        roe = _safe_float(info.get("returnOnEquity", 0))
        gross_margin = _safe_float(info.get("grossMargins", 0))
        debt_to_equity = _safe_float(info.get("debtToEquity", 0))
        revenue_growth = _safe_float(info.get("revenueGrowth", 0))
        profit_growth = _safe_float(info.get("earningsGrowth", 0))

        has_any = any(v > 0 for v in [roe, gross_margin, debt_to_equity])
        financials = None
        if has_any:
            financials = {
                "roe": roe,
                "gross_margin": gross_margin,
                "debt_ratio": debt_to_equity,
                "net_profit_yoy": profit_growth,
                "revenue_yoy": revenue_growth,
            }

        # ── PE/PB ──
        pe = _safe_float(info.get("trailingPE", 0)) or _safe_float(info.get("forwardPE", 0))
        pb = _safe_float(info.get("priceToBook", 0))

        pe_pb = None
        if pe > 0 or pb > 0:
            pe_pb = [{"date": date.today().isoformat(), "pe": pe, "pb": pb}]

        logger.info(
            f"yfinance 基本面 ({code}): PE={pe:.1f}, PB={pb:.1f}, "
            f"ROE={roe:.1%}, 毛利率={gross_margin:.1%}"
        )
        return financials, pe_pb
    except Exception as e:
        logger.warning(f"yfinance 基本面获取失败 ({code}): {e}")
        return None, None


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
                result[d]["pe" if indicator == "市盈率(TTM)" else "pb"] = float(row[1])
        except Exception as e:
            logger.warning(f"百度 {indicator} 获取失败 ({code}): {e}")

    if not result:
        return None
    items = [v for v in result.values() if v.get("pe") and v.get("pb")]
    items.sort(key=lambda x: str(x["date"]))
    return items


# ── 得分计算 ──


def _calc_style_factors(pe_pb_history: list[dict]) -> dict:
    """从 PE/PB 历史序列计算当前值所处的历史分位。

    当 forward PE 可用时，当前 PE 使用 (trailing + forward) / 2 混合值，
    避免周期股因盈利波动导致的 trailing PE 极端值误判。

    最低要求 3 条数据。PE/PB 值可以是 float 或字符串（兼容百度 API 返回值）。
    """
    min_entries = 3
    if not pe_pb_history or len(pe_pb_history) < min_entries:
        return {"pe_percentile": 0.5, "pb_percentile": 0.5}

    def _safe_pe_pb(val) -> float | None:
        """转 float，排除 nan/inf/非数字字符串。"""
        try:
            v = float(val)
        except (ValueError, TypeError):
            return None
        if np.isnan(v) or np.isinf(v):
            return None
        return v

    pes = [_safe_pe_pb(d.get("pe")) for d in pe_pb_history]
    pes = [p for p in pes if p is not None]
    pbs = [_safe_pe_pb(d.get("pb")) for d in pe_pb_history]
    pbs = [p for p in pbs if p is not None]

    if len(pes) < min_entries or len(pbs) < min_entries:
        return {"pe_percentile": 0.5, "pb_percentile": 0.5}

    # 当前 PE 混合：如果有远期 PE，用 (trailing + forward) / 2 替代纯 trailing
    pe_current = pes[-1]
    last_item = pe_pb_history[-1]
    pe_forward = _safe_pe_pb(last_item.get("pe_forward"))
    if pe_forward and pe_forward > 0:
        pe_blended = (pe_current + pe_forward) / 2
        pes[-1] = pe_blended
        logger.info(
            f"PE 混合: trailing={pe_current:.1f}, forward={pe_forward:.1f} → blended={pe_blended:.1f}"
        )

    return {
        "pe_percentile": round(sum(1 for p in pes if p < pes[-1]) / len(pes), 4),
        "pb_percentile": round(sum(1 for p in pbs if p < pbs[-1]) / len(pbs), 4),
    }


def score_style_factor(pe_pct: float, pb_pct: float, ev_ebitda: float = 0.0) -> float:
    """多指标估值风格得分：PE 分位 + PB 分位 + EV/EBITDA。

    - PE/PB 分位 ∈ [0,1]：高分位 = 偏贵 → 负得分
    - EV/EBITDA：<10 偏便宜，>20 偏贵，映射到 [-1,+1]
    - 三项等权平均

    如果 ev_ebitda=0（数据缺失），仅用 PE+PB 两项。
    """
    pe_pb_score = np.tanh((0.5 - (pe_pct + pb_pct) / 2) * 4)

    if ev_ebitda > 0:
        # EV/EBITDA 映射：15 为中性（tanh(0)=0），<5 便宜（+1），>25 贵（-1）
        ev_score = np.tanh((0.5 - ev_ebitda / 30) * 4)
        return round(float(np.mean([pe_pb_score, ev_score])), 4)

    return round(float(pe_pb_score), 4)


def score_fundamental_factor(
    roe: float, gross_margin: float, debt_ratio: float,
    net_profit_yoy: float, revenue_yoy: float,
) -> float:
    """基本面因子得分，缺失数据（0 或 NaN）自动排除不参与平均。"""
    raw = [roe, gross_margin, debt_ratio, net_profit_yoy, revenue_yoy]
    funcs = [
        lambda v: np.tanh((v - 0.10) * 8),       # ROE: 中性线 10%
        lambda v: np.tanh((v - 0.30) * 5),       # 毛利率: 中性线 30%
        lambda v: np.tanh((0.40 - v) * 5),       # 负债率: 中性线 40%
        lambda v: np.tanh(v * 4),                 # 净利润增速
        lambda v: np.tanh(v * 4),                 # 营收增速
    ]
    scores = []
    for val, fn in zip(raw, funcs):
        if val is None or (isinstance(val, float) and (np.isnan(val) or val == 0)):
            continue  # 缺失数据 → 跳过
        scores.append(fn(val))
    if not scores:
        return 0.0
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


# ── 基本面数据 24h 缓存 + 便捷包装 ──

_fundamental_cache: dict[str, tuple[float, dict]] = {}


def get_fundamental_data(name: str, code: str, market: str) -> dict | None:
    """获取基本面数据的便捷包装，带 24h 内存缓存。

    自动读取 Settings 中的 LLM 和 Finnhub 配置，消除 3 处重复的参数组装代码。
    同一 (code, market) 在 24h 内直接返回缓存，避免重复 API 调用。
    """
    cache_key = f"{code}:{market}"
    now_ts = __import__("time").time()
    if cache_key in _fundamental_cache:
        cached_ts, cached_data = _fundamental_cache[cache_key]
        ttl = 86400 if cached_data else 300
        if now_ts - cached_ts < ttl:
            logger.debug(f"基本面缓存命中 ({cache_key})")
            return cached_data

    from config.settings import Settings
    settings = Settings()
    result = fetch_fundamental_factors(
        name=name, code=code, market=market,
        model=settings.get("llm_model", ""),
        base_url=settings.get("llm_base_url", ""),
        api_key=settings.get("llm_api_key", ""),
        finnhub_token=settings.get("news_token_us", ""),
    )
    # Successful fundamentals are stable for a day. A transient provider or
    # DNS failure is cached for only five minutes so it cannot poison the
    # whole desktop session.
    _fundamental_cache[cache_key] = (now_ts, result)
    return result
