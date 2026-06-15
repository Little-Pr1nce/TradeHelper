#!/usr/bin/env python3
"""
TradeHelper 量化回测 CLI Demo。

CLI 直接获取指定日期范围的原始数据 → 调用 run_pipeline() 执行分析 → 打印结果。
与 UI 共用 core.pipeline 计算逻辑，但对于精确日期范围直接绕过 Service 层的缓存策略。

用法:
    python run_backtest.py --code 600519 --start 2024-01-01 --end 2024-12-31
    python run_backtest.py --code AAPL --start 2024-01-01 --end 2024-12-31 --strategy A
    python run_backtest.py --code 600519 --start 2024-01-01 --end 2024-12-31 --capital 500000
"""

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.pipeline import run_pipeline
from backtest.analytics import compare_strategies, plot_comparison
from data.stock_fetcher import get_stock_fetcher
from utils.market import detect_market

logger = logging.getLogger("run_backtest")


def print_header(title: str):
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")


def print_metrics_table(summary: list[dict]):
    if not summary:
        return
    keys = list(summary[0].keys())
    col_widths = {k: max(len(k), max(len(str(row.get(k, ""))) for row in summary)) + 2
                  for k in keys}
    header = "".join(f"{k:<{col_widths[k]}}" for k in keys)
    print(header)
    print("-" * len(header))
    for row in summary:
        print("".join(f"{str(row.get(k, '')):<{col_widths[k]}}" for k in keys))


def main():
    parser = argparse.ArgumentParser(description="TradeHelper 量化回测 CLI Demo")
    parser.add_argument("--code", required=True, help="股票代码（A 股 6 位 / 美股字母）")
    parser.add_argument("--start", required=True, help="起始日期 YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="结束日期 YYYY-MM-DD")
    parser.add_argument("--strategy", default="all", help="策略选择: A/B/C/all（逗号分隔）")
    parser.add_argument("--capital", type=float, default=100000.0, help="初始资金（默认 100000）")
    parser.add_argument("--w-tech", type=float, default=0.6, help="技术面权重（默认 0.6）")
    parser.add_argument("--w-news", type=float, default=0.4, help="新闻面权重（默认 0.4）")
    args = parser.parse_args()

    if abs(args.w_tech + args.w_news - 1.0) > 1e-10:
        print(f"错误: w_tech({args.w_tech}) + w_news({args.w_news}) ≠ 1.0")
        sys.exit(1)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    code = args.code.strip().upper()
    market = detect_market(code) or "A"

    # 初始化 Settings（get_stock_fetcher 需要读取配置）
    from config.settings import Settings
    Settings.init(Settings.default_config_path())

    print_header(f"回测 {code} | {args.start} → {args.end}")

    # ── 1. 拉取股价数据（跳过缓存，直接获取指定日期范围） ──
    print("  正在获取股价数据...")
    from config.settings import Settings
    fetcher = get_stock_fetcher(market)
    prices = fetcher.fetch_price_history(code, args.start, args.end)
    if not prices:
        if market == "US":
            print(f"  错误: 美股需要配置「美股数据源 Token」。请在设置中填写。")
        else:
            print(f"  错误: 无法获取 {code} 的股价数据，请检查代码或网络")
        sys.exit(1)

    df = pd.DataFrame([p.to_dict() for p in prices])
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    print(f"  获取 {len(df)} 条 K 线 ({df['date'].iloc[0].strftime('%Y-%m-%d')} ~ "
          f"{df['date'].iloc[-1].strftime('%Y-%m-%d')})")

    # ── 2. 加载新闻数据（从本地数据库） ──
    news_df = None
    try:
        from data.database import Database
        news_list = Database().get_news(code, limit=500)
        if news_list:
            daily_scores: dict[str, list[float]] = {}
            for n in news_list:
                if not n.sentiment:
                    continue
                d = str(n.date)[:10]
                score_map = {"positive": 1.0, "negative": -1.0, "neutral": 0.0}
                daily_scores.setdefault(d, []).append(score_map.get(n.sentiment, 0.0))
            rows = [{"date": k, "finbert_score": sum(v) / len(v)}
                    for k, v in daily_scores.items()]
            if rows:
                news_df = pd.DataFrame(rows)
                print(f"  加载 {len(news_df)} 天的新闻情感数据")
    except Exception as e:
        logger.warning(f"新闻数据加载失败: {e}")

    # ── 3. 执行分析管道 ──
    print(f"  执行分析管道 (w_tech={args.w_tech}, w_news={args.w_news})...")
    if args.strategy.lower() == "all":
        strategy_names = ["A", "B", "C"]
    else:
        strategy_names = [s.strip() for s in args.strategy.split(",")]

    result = run_pipeline(
        df, news_df,
        initial_capital=args.capital,
        market=market,
        strategy_names=strategy_names,
        w_tech=args.w_tech,
        w_news=args.w_news,
    )

    # ── 4. 展示回测结果 ──
    print_header("多策略绩效对比")
    for name, r in result.backtest.items():
        print(f"\n  [{name}]")
        print(f"    总收益: {r.total_return*100:+.2f}%  年化: {r.annual_return*100:+.2f}%")
        print(f"    最大回撤: {r.max_drawdown*100:.2f}%  夏普: {r.sharpe_ratio:.2f}  "
              f"Calmar: {r.calmar_ratio:.2f}")
        print(f"    胜率: {r.win_rate*100:.1f}%  交易: {r.total_trades}次  "
              f"持仓: {r.avg_holding_days:.0f}天")

    comparison = compare_strategies(result.backtest)
    if comparison["summary_table"]:
        print(f"\n  {'指标对比表':-^60}")
        print_metrics_table(comparison["summary_table"])

    # ── 5. 交易明细 ──
    print_header("交易明细（最近 10 笔）")
    for name, r in result.backtest.items():
        trades = r.trades[-10:]
        if trades:
            print(f"\n  [{name}]")
            for t in trades:
                entry = t.get("entry_date", "?")
                exit_ = t.get("exit_date", "持仓中")
                pnl = t.get("pnl", 0)
                ret = t.get("return_pct", 0)
                reason = t.get("exit_reason", t.get("reason", ""))
                print(f"    {entry} → {exit_}: PnL={pnl:+.2f} ({ret:+.1f}%)  {reason}")
        else:
            print(f"\n  [{name}] 无交易")

    print_header("回测完成")


if __name__ == "__main__":
    main()
