"""启动/关闭顺序：设置 -> 工作目录 -> schema17 -> 迁移检查 -> composition root。"""
from __future__ import annotations
from pathlib import Path
from dataclasses import dataclass
from tradehelper_v2.config.settings import V2Settings
from tradehelper_v2.data.repository import SQLiteRepository
from dataclasses import fields
from tradehelper_v2.runtime.paths import ensure_work_dir, default_source_path, default_legacy_config_path
from tradehelper_v2.migration.config import merge_empty_settings
from tradehelper_v2.migration.legacy_reader import LegacyReader
from tradehelper_v2.migration.planner import MigrationPlanner
from .container import RuntimeContainer, build_runtime_container
from .version import APP_VERSION

@dataclass(slots=True)
class RuntimeLifecycle:
    container: RuntimeContainer
    migration_source: Path | None = None
    def __enter__(self): return self.container
    def __exit__(self, exc_type, exc, tb): self.close()
    def close(self): self.container.close()

def start_runtime(settings: V2Settings | None = None, *, settings_path: Path | None = None, migration_source: Path | None = None) -> RuntimeLifecycle:
    value=settings or V2Settings.load(settings_path)
    source=migration_source or default_source_path(value.work_dir)
    if source.exists():
        legacy=LegacyReader(source).read_config(default_legacy_config_path())
        current={item.name:getattr(value,item.name) for item in fields(value)}
        value=V2Settings.from_mapping(merge_empty_settings(current,legacy))
    ensure_work_dir(value.work_dir)
    # 启动即固化一份用户专属配置；save 使用临时文件 + replace + 0600。
    if settings is None or settings_path is not None:
        value.save(settings_path)
    container=build_runtime_container(value)
    completed=None
    if source.exists():
        reader=LegacyReader(source)
        completed=container.repository.find_completed_migration(reader.source.fingerprint(),MigrationPlanner.VERSION)
    container.migration_status = "completed" if completed else "pending" if source.exists() else "not_required"
    return RuntimeLifecycle(container, source if source.exists() and completed is None else None)
