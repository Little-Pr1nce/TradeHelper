"""ReportDocument 的确定性 Markdown 投影。"""
from __future__ import annotations
from contracts import ReportBlockKind
from presentation.formatting import format_datetime

def _cell(value):
    text=str(value).replace("|","\\|").replace("\n","<br>")
    return text.replace("；","<br>• ") if len(text)>=48 else text

def render_markdown(document):
    lines=[f"# {document.title}","",document.summary,"",f"数据时点：{format_datetime(document.as_of, document.market, seconds=True)}"]
    for section in document.sections:
        lines.extend(("",f"## {section.title}",section.purpose))
        for block in section.blocks:
            if block.kind in {ReportBlockKind.TEXT,ReportBlockKind.CALLOUT}: lines.extend(("",str(block.payload)))
            elif block.kind is ReportBlockKind.TABLE:
                table=block.payload; lines.extend(("",f"### {table.title}","",f"样本/行数：{len(table.rows)}","","| "+" | ".join(_cell(item) for item in table.columns)+" |","| "+" | ".join("---" for _ in table.columns)+" |"))
                lines.extend("| "+" | ".join(_cell(cell) for cell in row.cells)+" |" for row in table.rows)
                if not table.rows and table.empty_state: lines.append(table.empty_state)
                if table.interpretation: lines.extend(("",table.interpretation))
            elif block.kind is ReportBlockKind.CHART:
                chart=block.payload; lines.extend(("",f"### 图表：{chart.title}",f"横轴：{chart.x_axis}；纵轴：{chart.y_axis}；样本数：{chart.sample_count}",chart.interpretation))
                for name,points in chart.series:
                    lines.extend(("",f"- {name}: "+"；".join(f"{x}={y:.6g}" for x,y in points)))
                if not chart.series and chart.empty_state: lines.append(chart.empty_state)
    return "\n".join(lines)+"\n"
