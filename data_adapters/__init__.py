"""
数据适配器模块（架构预留 — 当前不实现）。

本模块为未来的实时数据接入和增量更新预留接口设计，
当前仅定义抽象基类，**严禁在本模块中实现任何实际逻辑**。

未来扩展方向：
  1. DataAdapter 统一接口：支持盘前集合竞价数据、盘中实时 Bar（历史+增量拼接）、盘后完整数据注入
  2. SignalStabilizer 盘中信号防抖器：变动幅度容忍度过滤 + Bar 走完确认模式
  3. SentimentProvider 情绪因子热插拔：可切换为其他 NLP API 或量价情绪代理因子
  4. GlobalRiskManager 组合级全局风控钩子：总净值回撤熔断、行业暴露度限制
  5. StrategyAutoRegistry 策略自动注册装饰器

设计原则：
  - 所有接口遵循「依赖倒置」，Alpha 打分和回测引擎依赖抽象接口而非具体实现
  - 新数据源只需实现 DataAdapter 接口，无需修改核心逻辑
"""

from abc import ABC, abstractmethod
from typing import Protocol

import pandas as pd


class DataAdapter(ABC):
    """
    数据适配器抽象基类（未来扩展 — 当前不实现）。

    统一实时/历史/盘中数据的注入接口，隔离数据源差异。
    """

    @abstractmethod
    def get_daily_data(self, code: str, date: str) -> pd.DataFrame:
        """获取单日完整 Bar 数据。"""
        ...

    @abstractmethod
    def get_intraday_data(self, code: str, date: str) -> pd.DataFrame:
        """获取盘中实时未完成 Bar 数据（历史缓存 + 增量拼接）。"""
        ...

    @abstractmethod
    def get_auction_data(self, code: str, date: str) -> dict:
        """获取盘前集合竞价数据。"""
        ...


class SignalStabilizer(ABC):
    """
    盘中信号防抖器（未来扩展 — 当前不实现）。

    避免盘中 Final_Score 剧烈波动导致频繁交易。
    支持两种确认模式：
      - tolerance: 变动幅度容忍度过滤（如 Final_Score 变动 < 0.05 忽略）
      - bar_complete: Bar 走完确认模式（等当前 Bar 彻底收线后再计算信号）
    """

    @abstractmethod
    def should_emit(self, current_score: float, previous_score: float) -> bool:
        """判断当前信号是否应该发出。"""
        ...


class SentimentProvider(Protocol):
    """
    情绪因子提供者协议（未来扩展 — 当前不实现）。

    支持在不修改执行策略的前提下无缝切换情绪数据源。
    当前 FinBERT 固定读取 → 未来可切换为：
      - 其他 NLP 模型 API（GPT、BloombergGPT 等）
      - 量价代理因子（如恐慌指数 VIX 映射）
      - 社交媒体情绪聚合
    """

    def get_sentiment(self, code: str, date: str) -> float:
        """返回 [-1, +1] 范围的情感得分。"""
        ...


class GlobalRiskManager(ABC):
    """
    组合级全局风控钩子（未来扩展 — 当前不实现）。

    当前仅实现个股级止损，本接口预留组合级约束：
      - 账户总净值回撤熔断（如当日回撤 > 5% 暂停交易）
      - 行业暴露度限制（单一行业持仓不超过 30%）
      - 净敞口限制（多空组合的 Beta 中性约束）
    """

    @abstractmethod
    def check_portfolio_limits(self, positions: list, account: dict) -> list[str]:
        """检查组合级风控限制，返回触发的限制列表。"""
        ...


__all__ = [
    "DataAdapter",
    "SignalStabilizer",
    "SentimentProvider",
    "GlobalRiskManager",
]
