"""
组合管理与分析服务。
"""

import json
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from core.pipeline import run_pipeline
from data.database import Database
from data.models import PortfolioAnalysis, PortfolioHolding
from data.stock_fetcher import get_stock_fetcher
from utils.dates import get_backtest_dates
from utils.market import detect_market


@dataclass
class PortfolioCandidate:
    code: str
    name: str
    market: str
    industry: str
    weight: float
    final_score: float
    max_drawdown: float
    total_return: float
    rank: int = 0

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class PortfolioAnalysisResult:
    summary: str
    industry_exposure: dict[str, float]
    max_drawdown: float
    risk_triggered: bool
    candidates: list[PortfolioCandidate] = field(default_factory=list)
    analysis_id: int | None = None


class PortfolioService:
    """组合 CRUD 辅助和批量分析。"""

    def __init__(self):
        self.db = Database()

    def create_or_update_portfolio(self, name: str, description: str = "", risk_stop_pct: float = 0.08) -> int:
        return self.db.create_portfolio(name.strip(), description.strip(), risk_stop_pct)

    def add_or_update_holding(
        self,
        portfolio_id: int,
        code: str,
        name: str = "",
        market: str = "US",
        industry: str = "",
        weight: float = 0.0,
        note: str = "",
    ):
        self.db.upsert_portfolio_holding(PortfolioHolding(
            portfolio_id=portfolio_id,
            code=code.strip().upper(),
            name=name.strip() or code.strip().upper(),
            market=market,
            industry=industry.strip(),
            weight=weight,
            note=note.strip(),
        ))

    def analyze_portfolio(self, portfolio_id: int, period: str = "1y") -> PortfolioAnalysisResult:
        portfolio = self.db.get_portfolio(portfolio_id)
        if not portfolio:
            raise ValueError("组合不存在")

        holdings = self.db.list_portfolio_holdings(portfolio_id)
        if not holdings:
            raise ValueError("组合中还没有标的")

        candidates: list[PortfolioCandidate] = []
        industry_exposure: dict[str, float] = {}
        portfolio_drawdown = 0.0

        start, end = get_backtest_dates(period)
        for holding in holdings:
            market = holding.market or detect_market(holding.code) or "US"
            prices = self.db.get_prices(holding.code, start, end)
            if not prices:
                fetcher = get_stock_fetcher(market)
                prices = fetcher.fetch_price_history(holding.code, start, end)
                if prices:
                    self.db.insert_prices(prices)
                    prices = self.db.get_prices(holding.code, start, end)
            if not prices:
                continue

            df = pd.DataFrame([p.to_dict() for p in prices])
            df["date"] = pd.to_datetime(df["date"])
            result = run_pipeline(df, news_df=None, market=market, w_tech=1.0, w_news=0.0)
            latest_score = 0.0
            if "Final_Score" in result.df.columns and not result.df["Final_Score"].dropna().empty:
                latest_score = float(result.df["Final_Score"].dropna().iloc[-1])

            best_return = 0.0
            best_drawdown = 0.0
            for bt in result.backtest.values():
                if bt.total_return >= best_return:
                    best_return = bt.total_return
                    best_drawdown = bt.max_drawdown
            portfolio_drawdown = min(portfolio_drawdown, best_drawdown)

            industry = holding.industry or "未分类"
            industry_exposure[industry] = industry_exposure.get(industry, 0.0) + holding.weight
            candidates.append(PortfolioCandidate(
                code=holding.code,
                name=holding.name or holding.code,
                market=market,
                industry=industry,
                weight=holding.weight,
                final_score=latest_score,
                max_drawdown=best_drawdown,
                total_return=best_return,
            ))

        candidates.sort(key=lambda c: (c.final_score, c.total_return), reverse=True)
        for idx, candidate in enumerate(candidates, start=1):
            candidate.rank = idx

        risk_triggered = abs(portfolio_drawdown) >= portfolio.risk_stop_pct
        summary = self._build_summary(candidates, industry_exposure, portfolio_drawdown, risk_triggered)
        analysis = PortfolioAnalysis(
            portfolio_id=portfolio_id,
            create_time=datetime.now().isoformat(),
            summary=summary,
            industry_exposure=json.dumps(industry_exposure, ensure_ascii=False),
            max_drawdown=portfolio_drawdown,
            risk_triggered=risk_triggered,
            candidates_json=json.dumps([c.to_dict() for c in candidates], ensure_ascii=False),
        )
        analysis_id = self.db.insert_portfolio_analysis(analysis)
        return PortfolioAnalysisResult(
            summary=summary,
            industry_exposure=industry_exposure,
            max_drawdown=portfolio_drawdown,
            risk_triggered=risk_triggered,
            candidates=candidates,
            analysis_id=analysis_id,
        )

    @staticmethod
    def _build_summary(
        candidates: list[PortfolioCandidate],
        industry_exposure: dict[str, float],
        max_drawdown: float,
        risk_triggered: bool,
    ) -> str:
        lines = ["## 组合分析结果", ""]
        lines.append(f"- 候选标的数：{len(candidates)}")
        lines.append(f"- 组合最大回撤参考：{max_drawdown:.2%}")
        lines.append(f"- 回撤熔断：{'触发' if risk_triggered else '未触发'}")
        if industry_exposure:
            lines.append("- 行业暴露：" + "、".join(
                f"{k} {v:.1%}" for k, v in sorted(industry_exposure.items(), key=lambda item: item[1], reverse=True)
            ))
        lines.extend(["", "| 排名 | 代码 | 名称 | Final Score | 策略收益 | 最大回撤 |", "|------|------|------|-------------|----------|----------|"])
        for candidate in candidates:
            lines.append(
                f"| {candidate.rank} | {candidate.code} | {candidate.name} | "
                f"{candidate.final_score:+.3f} | {candidate.total_return:+.2%} | {candidate.max_drawdown:.2%} |"
            )
        return "\n".join(lines)
