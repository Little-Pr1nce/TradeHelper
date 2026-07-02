"""HTML report readability helpers shared by Tab1 and Tab3 exports."""

import re


_TABLE_RE = re.compile(r"<table(?:\s[^>]*)?>.*?</table>", re.IGNORECASE | re.DOTALL)
_ROW_RE = re.compile(r"<tr(?:\s[^>]*)?>", re.IGNORECASE)


def fold_long_html_tables(body_html: str, min_data_rows: int = 8) -> str:
    """Wrap long rendered Markdown tables in native collapsible details blocks."""
    if not body_html:
        return body_html

    def replace(match: re.Match) -> str:
        table = match.group(0)
        row_count = max(len(_ROW_RE.findall(table)) - 1, 0)
        if row_count <= min_data_rows:
            return table
        return (
            '<details class="report-table-fold">'
            f'<summary>展开完整表格（{row_count} 行）</summary>'
            f'{table}</details>'
        )

    return _TABLE_RE.sub(replace, body_html)


REPORT_TABLE_FOLD_CSS = """
  details.report-table-fold { margin: 14px 0 20px; border: 1px solid #d7dde5; background: #fff; }
  details.report-table-fold > summary { cursor: pointer; padding: 10px 12px; color: #1f5f8b; font-weight: 600; }
  details.report-table-fold[open] > summary { border-bottom: 1px solid #d7dde5; }
  details.report-table-fold table { margin: 0; }
"""
