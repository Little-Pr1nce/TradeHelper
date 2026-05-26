"""
PDF 报告导出模块

使用 reportlab 库将 Markdown 格式的分析报告转换为 PDF 文件。

核心功能：
  1. 中文字体支持（自动查找系统字体，找不到则回退到 Helvetica）
  2. Markdown 段落解析（标题、列表、引用、粗体、代码块）
  3. K 线图嵌入（可选，图表嵌入在封面之后）
  4. 标准 A4 页面排版，专业配色

PDF 输出路径：{work_dir}/reports/report_{code}_{timestamp}.pdf

【扩展点】自定义 PDF 样式：
  1. 修改 _get_styles() 中的 ParagraphStyle 参数调整字体、间距、颜色
  2. 修改页面尺寸（A4 → Letter / A3）
  3. 添加页眉/页脚（在 SimpleDocTemplate 的 onPage 回调中实现）
  4. 添加水印或公司 Logo
  5. 支持多语言报告模板（英文排版）
"""

import logging
import os
import re
from datetime import datetime

# ---- reportlab 导入 ----
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm, cm
from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Image,
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from config.settings import Settings

logger = logging.getLogger(__name__)

# 全局标志：中文字体是否已注册（避免重复注册）
_chinese_font_registered = False


def _register_chinese_font() -> str:
    """
    注册中文字体到 reportlab，返回字体名称。

    策略：
      1. 自动查找系统中的中文字体文件
      2. 如果找到 → 注册为 "ChineseFont" 并返回
      3. 如果未找到 → 回退到 "Helvetica"（英文无问题，中文可能显示为方块）

    字体注册只需一次（全局标志 _chinese_font_registered 控制）。

    Returns:
        reportlab 中的字体名称字符串
    """
    global _chinese_font_registered
    if _chinese_font_registered:
        return "ChineseFont"

    from utils.helpers import get_chinese_font_path
    font_path = get_chinese_font_path()
    if font_path:
        try:
            pdfmetrics.registerFont(TTFont("ChineseFont", font_path))
            _chinese_font_registered = True
            return "ChineseFont"
        except Exception:
            pass

    _chinese_font_registered = True
    return "Helvetica"  # 回退字体


def _get_styles() -> dict:
    """
    构建 reportlab 段落样式表。

    样式层级：
      - ChineseTitle: 报告大标题（居中，22pt）
      - ChineseH1:    一级标题（18pt）
      - ChineseH2:    二级标题（14pt）
      - ChineseBody:  正文段落（11pt，两端对齐）
      - ChineseSmall: 小字/注释（9pt，灰色，居中）
      - ChineseCode:  代码块（9pt，灰底）

    Returns:
        reportlab 样式字典

    【扩展点】调整此方法中的参数可统一修改所有 PDF 的视觉风格。
    """
    font_name = _register_chinese_font()

    styles = {
        "ChineseTitle": ParagraphStyle(
            name="ChineseTitle",
            fontName=font_name,
            fontSize=22,
            leading=30,          # 行距
            alignment=TA_CENTER, # 居中
            spaceAfter=12,
            textColor=HexColor("#1a1a1a"),
        ),
        "ChineseH1": ParagraphStyle(
            name="ChineseH1",
            fontName=font_name,
            fontSize=18,
            leading=24,
            spaceBefore=20,
            spaceAfter=10,
            textColor=HexColor("#2c3e50"),
        ),
        "ChineseH2": ParagraphStyle(
            name="ChineseH2",
            fontName=font_name,
            fontSize=14,
            leading=20,
            spaceBefore=14,
            spaceAfter=6,
            textColor=HexColor("#34495e"),
        ),
        "ChineseH3": ParagraphStyle(
            name="ChineseH3",
            fontName=font_name,
            fontSize=12,
            leading=18,
            spaceBefore=10,
            spaceAfter=4,
            textColor=HexColor("#41607a"),
        ),
        "ChineseBody": ParagraphStyle(
            name="ChineseBody",
            fontName=font_name,
            fontSize=11,
            leading=18,
            spaceBefore=4,
            spaceAfter=4,
            alignment=TA_JUSTIFY,  # 两端对齐（更美观）
            textColor=HexColor("#333333"),
        ),
        "ChineseSmall": ParagraphStyle(
            name="ChineseSmall",
            fontName=font_name,
            fontSize=9,
            leading=14,
            textColor=HexColor("#999999"),
            alignment=TA_CENTER,
        ),
        "ChineseCode": ParagraphStyle(
            name="ChineseCode",
            fontName=font_name,
            fontSize=9,
            leading=14,
            textColor=HexColor("#555555"),
            backColor=HexColor("#f5f5f5"),  # 浅灰背景
            borderPadding=6,
        ),
    }
    return styles


def export_report_to_pdf(
    report_content: str,
    chart_path: str = "",
    stock_name: str = "",
    stock_code: str = "",
    backtest_period: str = "",
) -> str | None:
    """
    将分析报告导出为 PDF 文件。

    文档结构：
      1. 封面（标题 + 元信息）
      2. K 线图（如有）
      3. 报告正文（Markdown 解析后排版）
      4. 免责声明

    Args:
        report_content:  Markdown 格式的完整报告
        chart_path:      K 线图 PNG 路径（可选）
        stock_name:      股票名称（用于封面）
        stock_code:      股票代码（用于文件命名）
        backtest_period: 回测周期（用于封面）

    Returns:
        PDF 文件的绝对路径，失败返回 None
    """
    try:
        # ---- 确定输出路径 ----
        pdf_dir = Settings().pdf_dir
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"report_{stock_code}_{timestamp}.pdf"
        filepath = os.path.join(pdf_dir, filename)

        # ---- 初始化样式和字体 ----
        styles = _get_styles()

        # ---- 创建文档 ----
        doc = SimpleDocTemplate(
            filepath,
            pagesize=A4,
            topMargin=2*cm,
            bottomMargin=2*cm,
            leftMargin=2*cm,
            rightMargin=2*cm,
        )

        elements = []  # 文档元素列表

        # ---- 封面 ----
        elements.append(Paragraph(
            f"{stock_name} 分析报告", styles["ChineseTitle"]
        ))
        elements.append(Spacer(1, 4*mm))

        # 元信息行
        elements.append(Paragraph(
            f"股票代码：{stock_code} &nbsp;&nbsp;|&nbsp;&nbsp; "
            f"回测周期：{backtest_period} &nbsp;&nbsp;|&nbsp;&nbsp; "
            f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
            styles["ChineseSmall"],
        ))
        elements.append(Spacer(1, 8*mm))

        # ---- K 线图（可选嵌入） ----
        if chart_path and os.path.exists(chart_path):
            try:
                # 缩放图片以适应 A4 宽度
                img = Image(chart_path, width=16*cm, height=8*cm)
                elements.append(img)
                elements.append(Spacer(1, 6*mm))
            except Exception as e:
                logger.warning(f"Failed to embed chart: {e}")

        # ---- 解析 Markdown 段落并排版 ----
        sections = _parse_markdown_sections(report_content)
        for title, content in sections:
            # 标题
            if title:
                elements.append(Paragraph(_inline_md_to_html(title), styles["ChineseH2"]))

            # 正文逐行处理
            lines = content.strip().split("\n")
            in_code_block = False
            for line in lines:
                stripped = line.strip()
                if not stripped:
                    elements.append(Spacer(1, 2*mm))  # 空行
                    continue

                # 代码块开关（``` 成对出现）
                if stripped.startswith("```"):
                    in_code_block = not in_code_block
                    continue
                if in_code_block:
                    elements.append(Paragraph(_escape_html(stripped), styles["ChineseCode"]))
                    continue

                if stripped.startswith("### "):
                    elements.append(Paragraph(_inline_md_to_html(stripped[4:]), styles["ChineseH3"]))
                elif stripped.startswith("## "):
                    elements.append(Paragraph(_inline_md_to_html(stripped[3:]), styles["ChineseH2"]))
                elif stripped.startswith("# "):
                    elements.append(Paragraph(_inline_md_to_html(stripped[2:]), styles["ChineseH1"]))
                elif stripped.startswith("- "):
                    elements.append(Paragraph(
                        f"&bull; {_inline_md_to_html(stripped[2:])}", styles["ChineseBody"]
                    ))
                elif stripped.startswith("> "):
                    elements.append(Paragraph(_inline_md_to_html(stripped[2:]), styles["ChineseSmall"]))
                else:
                    elements.append(Paragraph(_inline_md_to_html(stripped), styles["ChineseBody"]))

        # ---- 免责声明 ----
        elements.append(Spacer(1, 1*cm))
        elements.append(Paragraph(
            "免责声明：本报告仅供学习参考，不构成任何投资建议。股市有风险，投资需谨慎。",
            styles["ChineseSmall"],
        ))

        # ---- 生成 PDF ----
        doc.build(elements)
        logger.info(f"PDF exported: {filepath}")
        return filepath

    except Exception as e:
        logger.error(f"PDF export failed: {e}")
        return None


def _escape_html(text: str) -> str:
    """转义 reportlab Paragraph 会解释的 HTML 字符。"""
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))


def _inline_md_to_html(text: str) -> str:
    """
    把内联 Markdown 转成 reportlab Paragraph 支持的 HTML 子集。

    支持：
      - 行内粗体  **xxx** → <b>xxx</b>
      - 行内斜体  *xxx*   → <i>xxx</i>（避免吞掉 **）
      - 行内代码  `xxx`   → <font face="Courier">xxx</font>
    其他字符做 HTML 转义，避免 reportlab 解析错误。
    """
    escaped = _escape_html(text)
    # 粗体：**xx** （非贪婪），先于斜体
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
    # 斜体：*xx*
    escaped = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", escaped)
    # 行内代码：`xx`
    escaped = re.sub(r"`([^`]+)`", r'<font face="Courier">\1</font>', escaped)
    return escaped


def _parse_markdown_sections(content: str) -> list[tuple[str, str]]:
    """
    将 Markdown 文本按 ## 标题拆分为段落。

    解析规则：
      - 以 "## " 开头的行视为二级标题
      - 以 "# " 开头的行视为一级标题
      - 标题后的内容归入该节，直到遇到下一个标题

    示例：
      输入: "## 标题A\n内容1\n## 标题B\n内容2"
      输出: [("标题A", "内容1\n"), ("标题B", "内容2\n")]

    Args:
        content: Markdown 文本

    Returns:
        [(标题, 内容)] 列表
    """
    sections = []
    current_title = ""
    current_content = ""

    for line in content.split("\n"):
        if line.startswith("## "):
            # 遇到新二级标题 → 保存上一节
            if current_title or current_content.strip():
                sections.append((current_title, current_content))
            current_title = line[3:].strip()
            current_content = ""
        elif line.startswith("# "):
            # 遇到新一级标题
            if current_title or current_content.strip():
                sections.append((current_title, current_content))
            current_title = line[2:].strip()
            current_content = ""
        else:
            current_content += line + "\n"

    # 保存最后一节
    if current_title or current_content.strip():
        sections.append((current_title, current_content))

    return sections
