"""
市场识别与股票搜索工具。

提供 A 股/美股的市场判别、在线搜索和离线兜底匹配。
"""

import re
import logging

logger = logging.getLogger(__name__)


def detect_market(code: str) -> str:
    """
    识别股票所属市场。

    Args:
        code: 股票代码字符串

    Returns:
        市场标识 "A" / "US" / ""
    """
    code = code.strip().upper()
    if re.match(r"^\d{6}$", code):
        return "A"
    if re.match(r"^[A-Z]{1,6}$", code):
        return "US"
    return ""


def search_a_stock(keyword: str) -> list[dict]:
    """通过 akshare 在线搜索 A 股。"""
    try:
        from data.stock_fetcher import _without_system_proxy
        import akshare as ak
        with _without_system_proxy():
            df = ak.stock_info_a_code_name()
        if df is not None and not df.empty:
            mask = df["name"].str.contains(keyword, na=False)
            return [
                {"code": str(row["code"]), "name": str(row["name"]), "market": "A"}
                for _, row in df[mask].head(10).iterrows()
            ]
    except Exception:
        pass
    return []


def search_a_stock_fallback(keyword: str) -> list[dict]:
    """热门 A 股中英文对照表（在线搜索前的快速匹配）。"""
    FALLBACK = {
        "600519": ["贵州茅台", "茅台"],
        "000858": ["五粮液"],
        "000568": ["泸州老窖"],
        "000001": ["平安银行"],
        "600036": ["招商银行"],
        "601398": ["工商银行"],
        "601939": ["建设银行"],
        "002415": ["海康威视", "海康"],
        "300750": ["宁德时代", "宁德"],
        "002594": ["比亚迪"],
        "601012": ["隆基绿能", "隆基"],
        "600900": ["长江电力"],
        "601857": ["中国石油", "中石油"],
        "600028": ["中国石化", "中石化"],
        "601088": ["中国神华", "神华"],
        "601318": ["中国平安", "平安"],
        "000333": ["美的集团", "美的"],
        "000651": ["格力电器", "格力"],
        "600887": ["伊利股份", "伊利"],
        "002714": ["牧原股份", "牧原"],
        "603259": ["药明康德", "药明"],
        "600276": ["恒瑞医药", "恒瑞"],
        "300059": ["东方财富"],
        "600030": ["中信证券"],
        "601688": ["华泰证券"],
        "688981": ["中芯国际", "中芯"],
        "601728": ["中国电信"],
        "600050": ["中国联通"],
        "601166": ["兴业银行"],
        "600809": ["山西汾酒", "汾酒"],
        "000725": ["京东方", "京东方A"],
        "002475": ["立讯精密", "立讯"],
        "300124": ["汇川技术", "汇川"],
        "688111": ["金山办公", "金山"],
        "601899": ["紫金矿业", "紫金"],
        "600585": ["海螺水泥", "海螺"],
        "000002": ["万科", "万科A"],
        "001979": ["招商蛇口"],
    }
    results = []
    for code, names in FALLBACK.items():
        for name in names:
            if keyword in name or name in keyword:
                results.append({"code": code, "name": names[0], "market": "A"})
                break
    return results


def search_us_stock_online(keyword: str) -> list[dict]:
    """通过 Finnhub /search 在线搜索美股（需 news_token_us）。"""
    from config.settings import Settings
    token = (Settings().get("news_token_us", "") or "").strip()
    if not token:
        logger.warning("未配置 news_token_us，跳过 Finnhub 在线搜索")
        return []
    from data.finnhub_client import search_stock
    return search_stock(token, keyword)


def search_us_stock_fallback(keyword: str) -> list[dict]:
    """
    内置热门美股中英文对照表（仅在线搜索失败时兜底）。

    【扩展点】在此字典中添加更多股票的中文名映射。
    """
    FALLBACK = {
        "NVDA":  ["英伟达", "NVIDIA"],
        "AAPL":  ["苹果", "Apple"],
        "MSFT":  ["微软", "Microsoft"],
        "GOOGL": ["谷歌", "Google"],
        "AMZN":  ["亚马逊", "Amazon"],
        "META":  ["Meta", "Facebook", "脸书"],
        "TSLA":  ["特斯拉", "Tesla"],
        "TSM":   ["台积电", "TSMC"],
        "AMD":   ["AMD", "超微"],
        "INTC":  ["英特尔", "Intel"],
        "BABA":  ["阿里巴巴", "Alibaba"],
        "JD":    ["京东", "JD.com"],
        "PDD":   ["拼多多", "Pinduoduo"],
        "NIO":   ["蔚来", "NIO"],
        "BIDU":  ["百度", "Baidu"],
        "NFLX":  ["奈飞", "Netflix"],
        "DIS":   ["迪士尼", "Disney"],
        "JPM":   ["摩根大通"],
        "BAC":   ["美国银行"],
        "BRK.B": ["伯克希尔", "巴菲特"],
        "V":     ["Visa"],
        "WMT":   ["沃尔玛", "Walmart"],
        "KO":    ["可口可乐", "Coca-Cola"],
        "PEP":   ["百事", "Pepsi"],
        "COST":  ["好市多", "Costco"],
        "ADBE":  ["Adobe"],
        "ORCL":  ["甲骨文", "Oracle"],
        "CSCO":  ["思科", "Cisco"],
        "QCOM":  ["高通", "Qualcomm"],
        "UBER":  ["优步", "Uber"],
        "UBI":   ["育碧", "Ubisoft"],
        "SNAP":  ["Snapchat"],
        "SPY":   ["标普500", "SPY"],
        "QQQ":   ["纳斯达克ETF", "QQQ"],
    }
    results = []
    kw_lower = keyword.lower()
    for code, names in FALLBACK.items():
        for name in names:
            if kw_lower in name.lower() or name.lower() in kw_lower:
                results.append({"code": code, "name": names[0], "market": "US"})
                break
    return results
