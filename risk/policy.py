"""冻结的 V2-6 风控政策；硬约束由 RiskPolicy 合同复核，不能被关闭。"""
from contracts.risk import RiskPolicy

DEFAULT_RISK_POLICY = RiskPolicy()
