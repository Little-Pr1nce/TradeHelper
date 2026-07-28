"""V2-12 生产组合根和生命周期。"""
from .version import APP_VERSION
from .container import RuntimeContainer, build_runtime_container
from .lifecycle import RuntimeLifecycle, start_runtime

__all__ = ["APP_VERSION", "RuntimeContainer", "build_runtime_container", "RuntimeLifecycle", "start_runtime"]
