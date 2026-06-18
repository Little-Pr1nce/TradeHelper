"""
配置管理模块

使用单例模式 + JSON 文件持久化，管理应用的全局配置：
  - 工作目录路径
  - 大模型 API（OpenAI 兼容格式）
  - 数据源选择（免费/自定义）
  - 自定义 API 端点

配置文件存储在系统标准应用配置目录。
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
    "work_dir": str(Path.home() / "TradeHelper"),
    "llm_base_url": "",
    "llm_api_key": "",
    "llm_model": "",
    "stock_token_us": "",         # 美股数据源 Token（TickFlow API Key，免费注册即可获取实时行情）
    "stock_token_a": "",          # A 股数据源 Token（同上）
    "news_token_us": "",         # 美股新闻 Token（如 Finnhub）
    "news_token_a": "",          # A 股新闻 Token（如 Tushare）
    "finbert_model_path": "",    # 程序自动设置，用户无需关心
    "llm_enable_thinking": False,  # 启用模型思考/推理模式（DeepSeek V3/V3.1 等支持 extended thinking 的模型）
}

# 必填配置项（未填时禁止使用分析功能）
REQUIRED_FIELDS = ["work_dir", "llm_api_key", "llm_base_url", "llm_model"]

# 必填字段的中文标签（供提示使用）
FIELD_LABELS = {
    "work_dir": "工作目录",
    "llm_api_key": "LLM API Key",
    "llm_base_url": "LLM Base URL",
    "llm_model": "模型名称",
}


class Settings:
    """
    全局配置单例类。

    使用方式：
        settings = Settings.init(Settings.default_config_path())  # 首次初始化
        settings = Settings()  # 后续任意位置获取实例
        api_key = settings.get("llm_api_key")

    【扩展点】如需新增配置项：
      1. 在 DEFAULT_CONFIG 字典中添加默认值
      2. 在 UI 设置页面 (settings_ui.py) 添加对应的输入控件
      3. 在 _save_settings 方法中添加保存逻辑
    """

    # 必填配置项
    REQUIRED_FIELDS: list[str] = REQUIRED_FIELDS
    FIELD_LABELS: dict[str, str] = FIELD_LABELS

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
        初始化配置系统——从固定路径加载配置。

        Args:
            config_path: 配置文件固定路径
        """
        cls._config_path = Path(config_path)
        instance = cls()
        instance._data = dict(DEFAULT_CONFIG)

        if cls._config_path.exists():
            try:
                with open(cls._config_path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                if isinstance(saved, dict):
                    instance._data.update(saved)
                else:
                    raise ValueError("config root is not a JSON object")
            except (json.JSONDecodeError, IOError, ValueError) as e:
                backup = cls._config_path.with_name(
                    f"{cls._config_path.name}.broken-{datetime.now().strftime('%Y%m%d_%H%M%S')}")
                try:
                    shutil.copy2(cls._config_path, backup)
                    logger.warning(f"Config corrupted ({e}); backed up to {backup}")
                except Exception:
                    pass
                instance._data = dict(DEFAULT_CONFIG)
                try:
                    instance.save()
                except Exception:
                    pass
        else:
            instance.save()  # 首次运行创建默认配置
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

    @staticmethod
    def default_config_path() -> Path:
        """配置文件固定路径（跨平台）。"""
        import sys
        if sys.platform == "win32":
            base = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
        elif sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support"
        else:
            base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        return base / "TradeHelper" / "config.json"

    def set(self, key: str, value: Any):
        """
        设置配置项的值（仅在内存中修改，需调用 save() 持久化）。
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

    def is_fully_configured(self) -> bool:
        """
        检查是否所有必填项都已配置。

        Returns:
            True 表示所有 REQUIRED_FIELDS 都有值
        """
        return all(bool(self.get(f)) for f in REQUIRED_FIELDS)

    def missing_fields(self) -> list[str]:
        """
        返回未填写的必填配置项列表。

        Returns:
            缺失字段的 key 列表
        """
        return [f for f in REQUIRED_FIELDS if not bool(self.get(f, ""))]

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
