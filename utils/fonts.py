"""
中文字体查找 — 跨平台自动检测可用的中文字体路径。
"""

import os
import sys


def get_chinese_font_path() -> str | None:
    """
    自动查找操作系统中可用的中文字体路径。

    用于 mplfinance K 线图标题和 reportlab PDF 的中文渲染。
    按平台分别搜索常见中文字体：
      - macOS: 苹方、黑体、Hiragino Sans GB
      - Windows: 微软雅黑、黑体、宋体
      - Linux: 文泉驿、Droid Sans、Noto Sans CJK
    """
    font_paths = []

    if sys.platform == "darwin":
        font_paths = [
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/STHeiti Light.ttc",
            "/System/Library/Fonts/Hiragino Sans GB.ttc",
            "/Library/Fonts/Arial Unicode.ttf",
        ]
    elif sys.platform == "win32":
        font_paths = [
            os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", f)
            for f in ["msyh.ttc", "msyhbd.ttc", "simhei.ttf", "simsun.ttc"]
        ]
    else:
        font_paths = [
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        ]

    for p in font_paths:
        if os.path.exists(p):
            return p
    return None
