"""V2-12 只读 V1 读取、计划和事务执行。"""
from .legacy_reader import LegacySource, LegacyReader
from .planner import MigrationPlanner
from .executor import MigrationExecutor, MigrationExecutionError
from .config import merge_empty_settings

__all__ = ["LegacySource", "LegacyReader", "MigrationPlanner", "MigrationExecutor", "MigrationExecutionError", "merge_empty_settings"]
