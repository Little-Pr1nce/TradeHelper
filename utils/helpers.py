"""
工具函数模块

提供股票代码校验、市场识别、日期格式化、中文字体查找、
回测日期计算和日志配置等通用工具函数。
"""

import re
import logging
import os
import sys
from datetime import datetime, date, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


def is_valid_stock_code(code: str) -> tuple[bool, str]:
    """
    校验股票代码格式是否合法，并自动识别所属市场。

    规则：
      - A 股：6 位纯数字，如 "600519"（贵州茅台）、"000001"（平安银行）
      - 美股：1-6 位大写字母，如 "AAPL"、"TSLA"、"GOOGL"

    Args:
        code: 用户输入的股票代码字符串（支持大小写、前后空格）

    Returns:
        (是否合法, 市场标识) — 市场标识为 "A"、"US" 或空字符串 ""
    """
    if not code or not isinstance(code, str):
        return False, ""
    code = code.strip().upper()
    # A 股：6 位纯数字
    if re.match(r"^\d{6}$", code):
        return True, "A"
    # 美股：1-6 位纯字母
    if re.match(r"^[A-Z]{1,6}$", code):
        return True, "US"
    return False, ""


def detect_market(code: str) -> str:
    """
    仅识别股票所属市场，不做合法性校验。

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


def format_date(d: date | datetime | str) -> str:
    """
    将各种日期类型统一格式化为 "YYYY-MM-DD" 字符串。

    支持输入类型：
      - str: 直接截取前 10 位
      - datetime: 调用 strftime
      - date: 调用 isoformat

    Args:
        d: 待格式化的日期对象

    Returns:
        "YYYY-MM-DD" 格式的日期字符串
    """
    if isinstance(d, str):
        return d[:10]
    if isinstance(d, datetime):
        return d.strftime("%Y-%m-%d")
    if isinstance(d, date):
        return d.isoformat()
    return str(d)


def get_chinese_font_path() -> str | None:
    """
    自动查找操作系统中可用的中文字体路径。

    用于 mplfinance K 线图标题和 reportlab PDF 的中文渲染。
    按平台分别搜索常见中文字体：
      - macOS: 苹方、黑体、Hiragino Sans GB
      - Windows: 微软雅黑、黑体、宋体
      - Linux: 文泉驿、Droid Sans、Noto Sans CJK

    【扩展点】如需支持更多字体，在对应平台的 font_paths 列表中添加路径即可。

    Returns:
        找到的第一个可用字体文件的绝对路径，未找到则返回 None
    """
    font_paths = []

    if sys.platform == "darwin":
        # macOS 常见中文字体
        font_paths = [
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/STHeiti Light.ttc",
            "/System/Library/Fonts/Hiragino Sans GB.ttc",
            "/Library/Fonts/Arial Unicode.ttf",
        ]
    elif sys.platform == "win32":
        # Windows 常见中文字体
        font_paths = [
            os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", f)
            for f in ["msyh.ttc", "msyhbd.ttc", "simhei.ttf", "simsun.ttc"]
        ]
    else:
        # Linux 常见中文字体
        font_paths = [
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        ]

    for p in font_paths:
        if os.path.exists(p):
            return p
    return None


def get_backtest_dates(period: str) -> tuple[str, str]:
    """
    根据回测周期计算起始日期和结束日期。

    支持的周期：
      - "3m": 3 个月（约 90 天）
      - "6m": 6 个月（约 180 天）
      - "1y": 1 年（约 365 天）
      - "3y": 3 年（约 1095 天）

    【扩展点】如需支持更多周期（如 5y、10y），在 periods 字典中添加键值对即可。

    Args:
        period: 回测周期标识 ("3m" / "6m" / "1y" / "3y")

    Returns:
        (开始日期, 结束日期) 的日期字符串元组
    """
    today = date.today()
    periods = {
        "3m": 90,
        "6m": 180,
        "1y": 365,
        "3y": 1095,
    }
    days = periods.get(period, 90)
    # 对于 >=1 年的周期使用年份减法（更自然），短周期用 timedelta
    start = today.replace(year=today.year - max(1, days // 365))
    if days < 365:
        start = today - timedelta(days=days)
    return format_date(start), format_date(today)


def setup_logging(work_dir: str):
    """
    配置应用全局日志系统。

    日志同时输出到：
      - 文件：{work_dir}/logs/tradehelper.log（UTF-8 编码，追加模式）
      - 控制台：标准输出

    Args:
        work_dir: 用户配置的工作目录路径
    """
    log_dir = Path(work_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "tradehelper.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


def _search_a_stock(keyword: str) -> list[dict]:
    """通过 akshare 在线搜索 A 股。"""
    try:
        import akshare as ak
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


def _search_a_stock_fallback(keyword: str) -> list[dict]:
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


def _search_us_stock_online(keyword: str) -> list[dict]:
    """通过 Yahoo Finance 在线搜索美股。"""
    results = []
    try:
        import yfinance as yf
        search = yf.Search(keyword)
        for quote in search.quotes[:10]:
            symbol = quote.get("symbol", "")
            if not symbol or "." in symbol:
                continue
            name = quote.get("shortname") or quote.get("longname") or symbol
            results.append({"code": symbol, "name": name, "market": "US"})
    except Exception:
        try:
            import requests
            resp = requests.get(
                "https://query1.finance.yahoo.com/v1/finance/search",
                params={"q": keyword, "lang": "en-US", "quotesCount": 10},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
            )
            for quote in resp.json().get("quotes", []):
                symbol = quote.get("symbol", "")
                if not symbol or "." in symbol:
                    continue
                name = quote.get("shortname") or quote.get("longname") or symbol
                results.append({"code": symbol, "name": name, "market": "US"})
        except Exception:
            pass
    return results


def _search_us_stock_fallback(keyword: str) -> list[dict]:
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
