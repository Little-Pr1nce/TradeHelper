"""回测模块。"""

from backtest.broker import Broker, BrokerConfig, Account
from backtest.engine import BacktestEngine, BacktestConfig, BacktestResult
from backtest.analytics import compute_metrics, compute_rank_ic, compare_strategies, plot_comparison
