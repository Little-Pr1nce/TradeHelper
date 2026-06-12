"""
盘中信号防抖器。

用于避免实时价格小幅波动时重复生成盘中报告。
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from data.database import Database
from data.models import AnalysisReport


@dataclass
class StabilizerDecision:
    """防抖判断结果。"""
    should_emit: bool
    reason: str = ""
    previous_report: AnalysisReport | None = None


class SignalStabilizer:
    """基于价格变动幅度和最短间隔的盘中信号防抖器。"""

    def __init__(self, tolerance_pct: float = 0.003, min_interval_minutes: int = 10):
        self.tolerance_pct = tolerance_pct
        self.min_interval_minutes = min_interval_minutes

    def should_emit(self, code: str, current_price: float) -> StabilizerDecision:
        if current_price <= 0:
            return StabilizerDecision(True, "invalid current price")

        reports = Database().get_reports_by_code(code, mode="intraday", since_hours=24)
        if not reports:
            return StabilizerDecision(True, "no recent intraday report")

        latest = reports[0]
        previous_price = self._extract_latest_price(latest.content)
        if previous_price <= 0:
            return StabilizerDecision(True, "previous price unavailable", latest)

        try:
            created_at = datetime.fromisoformat(latest.create_time)
        except Exception:
            created_at = datetime.min
        within_interval = datetime.now() - created_at < timedelta(minutes=self.min_interval_minutes)
        move_pct = abs(current_price - previous_price) / previous_price

        if within_interval and move_pct < self.tolerance_pct:
            return StabilizerDecision(
                False,
                f"价格变动 {move_pct:.2%} 小于防抖阈值 {self.tolerance_pct:.2%}",
                latest,
            )
        return StabilizerDecision(True, f"价格变动 {move_pct:.2%}", latest)

    @staticmethod
    def _extract_latest_price(content: str) -> float:
        import re

        patterns = [
            r"最新价\s*\|\s*\*\*([0-9]+(?:\.[0-9]+)?)\*\*",
            r"最新价[:：]\s*([0-9]+(?:\.[0-9]+)?)",
        ]
        for pattern in patterns:
            match = re.search(pattern, content or "")
            if match:
                try:
                    return float(match.group(1))
                except ValueError:
                    return 0.0
        return 0.0
