"""
配置管理模块

使用单例模式 + JSON 文件持久化，管理应用的全局配置：
  - 工作目录路径
  - 大模型 API（OpenAI 兼容格式）
  - 数据源选择（免费/自定义）
  - 自定义 API 端点

配置文件默认存储在 ~/.tradehelper/config.json。
"""

import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# 默认配置模板，首次运行时使用
DEFAULT_CONFIG = {
    "work_dir": str(Path.home() / "TradeHelperData"),
    "llm_base_url": "https://api.openai.com/v1",
    "llm_api_key": "",
    "llm_model": "gpt-4o",
    "data_source": "free",
    "custom_api_endpoint": "",
    "custom_api_key": "",
    "proxy": "",
}


class Settings:
    """
    全局配置单例类。

    使用方式：
        settings = Settings.init("~/.tradehelper/config.json")  # 首次初始化
        settings = Settings()  # 后续任意位置获取实例
        api_key = settings.get("llm_api_key")

    【扩展点】如需新增配置项：
      1. 在 DEFAULT_CONFIG 字典中添加默认值
      2. 在 UI 设置页面 (settings_ui.py) 添加对应的输入控件
      3. 在 _save_settings 方法中添加保存逻辑
    """

    _instance = None          # 单例实例
    _config_path: Path | None = None  # 配置文件路径
    _data: dict[str, Any] = {}        # 内存中的配置数据

    def __new__(cls):
        """确保全局只有一个 Settings 实例（线程安全的简化实现）。"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._data = dict(DEFAULT_CONFIG)
        return cls._instance

    @classmethod
    def init(cls, config_path: str | Path) -> "Settings":
        """
        初始化配置系统——加载已有配置，或创建默认配置。

        应在应用启动时调用一次。
        如果配置文件已存在，合并到内存（已有键优先，新增默认键自动补全）。

        Args:
            config_path: JSON 配置文件的完整路径

        Returns:
            Settings 单例实例
        """
        cls._config_path = Path(config_path)
        instance = cls()
        # 重置为默认值，防止单例被复用时残留旧配置
        instance._data = dict(DEFAULT_CONFIG)

        if cls._config_path.exists():
            try:
                with open(cls._config_path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                if not isinstance(saved, dict):
                    raise ValueError("config root is not a JSON object")
                # 合并已保存配置（已保存的值覆盖默认值）
                instance._data.update(saved)
            except (json.JSONDecodeError, IOError, ValueError) as e:
                # 配置文件损坏：备份原文件 → 回退默认配置 → 重新落盘
                backup = cls._config_path.with_name(
                    f"{cls._config_path.name}.broken-{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                )
                try:
                    shutil.copy2(cls._config_path, backup)
                    logger.warning(
                        f"Config file is corrupted ({e}); backed up to {backup} "
                        f"and restored defaults"
                    )
                except Exception as copy_err:
                    logger.warning(
                        f"Config file is corrupted ({e}); failed to back up: {copy_err}"
                    )
                instance._data = dict(DEFAULT_CONFIG)
                try:
                    instance.save()
                except Exception:
                    pass
        else:
            instance.save()  # 首次运行时创建默认配置文件
        return instance

    def get(self, key: str, default: Any = None) -> Any:
        """
        获取配置项的值。

        Args:
            key: 配置键名
            default: 键不存在时的默认值

        Returns:
            配置值
        """
        return self._data.get(key, DEFAULT_CONFIG.get(key, default))

    def set(self, key: str, value: Any):
        """
        设置配置项的值（仅在内存中修改，需调用 save() 持久化）。

        Args:
            key: 配置键名
            value: 新值
        """
        self._data[key] = value

    def save(self):
        """将当前内存中的配置持久化到 JSON 文件。"""
        if self._config_path:
            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._config_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)

    def is_configured(self) -> bool:
        """
        检查是否已完成必要配置（至少配置了 LLM API Key）。

        Returns:
            True 表示 LLM API 已就绪
        """
        return bool(self.get("llm_api_key"))

    # ---- 便捷属性：自动拼装子目录路径 ----

    @property
    def work_dir(self) -> str:
        """用户配置的工作根目录。"""
        return self.get("work_dir")

    @property
    def db_path(self) -> str:
        """SQLite 数据库文件的完整路径 ({work_dir}/tradehelper.db)。"""
        work_dir = self.work_dir
        os.makedirs(work_dir, exist_ok=True)
        return os.path.join(work_dir, "tradehelper.db")

    @property
    def chart_dir(self) -> str:
        """K 线图输出目录 ({work_dir}/charts)。"""
        work_dir = self.work_dir
        chart_dir = os.path.join(work_dir, "charts")
        os.makedirs(chart_dir, exist_ok=True)
        return chart_dir

    @property
    def pdf_dir(self) -> str:
        """PDF 报告输出目录 ({work_dir}/reports)。"""
        work_dir = self.work_dir
        pdf_dir = os.path.join(work_dir, "reports")
        os.makedirs(pdf_dir, exist_ok=True)
        return pdf_dir
