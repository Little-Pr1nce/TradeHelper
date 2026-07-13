"""Raw-provider adapters and fixture parsers for the V2 data boundary."""

from .base import ProviderClient, RetryingClient, unavailable_result
from .adapters import AkshareAdapter, BaostockAdapter, FinnhubAdapter, FundamentalAdapter, NasdaqAdapter, TickFlowAdapter, YFinanceAdapter
from .parsers import (
    parse_baostock_listing_date,
    parse_finnhub_fundamentals,
    parse_finnhub_metadata,
    parse_finnhub_news,
    parse_nasdaq_bars,
    parse_nasdaq_quote,
    parse_tickflow_bars,
    parse_tickflow_metadata,
    parse_tickflow_quote,
    parse_yfinance_bars,
    parse_yfinance_quote,
)

__all__ = [
    "ProviderClient",
    "RetryingClient",
    "AkshareAdapter",
    "BaostockAdapter",
    "FinnhubAdapter",
    "FundamentalAdapter",
    "NasdaqAdapter",
    "TickFlowAdapter",
    "YFinanceAdapter",
    "parse_baostock_listing_date",
    "parse_finnhub_fundamentals",
    "parse_finnhub_metadata",
    "parse_finnhub_news",
    "parse_nasdaq_bars",
    "parse_nasdaq_quote",
    "parse_tickflow_bars",
    "parse_tickflow_metadata",
    "parse_tickflow_quote",
    "parse_yfinance_bars",
    "parse_yfinance_quote",
    "unavailable_result",
]
