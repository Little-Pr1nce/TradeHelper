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
