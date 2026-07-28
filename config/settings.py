"""Small, explicit V2 settings contract."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping


_SETTING_KEYS = (
    "work_dir",
    "llm_base_url",
    "llm_api_key",
    "llm_model",
    "stock_token_us",
    "stock_token_a",
    "news_token_us",
    "news_token_a",
    "finbert_model_path",
    "llm_enable_thinking",
)


def _default_config_dir() -> Path:
    if sys.platform == "win32":
        return Path.home() / "AppData" / "Roaming" / "TradeHelper"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "TradeHelper"
    return Path.home() / ".config" / "TradeHelper"


@dataclass(frozen=True, slots=True)
class V2Settings:
    work_dir: Path
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    stock_token_us: str = ""
    stock_token_a: str = ""
    news_token_us: str = ""
    news_token_a: str = ""
    finbert_model_path: str = ""
    llm_enable_thinking: bool = False

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "V2Settings":
        work_dir = Path(str(values.get("work_dir") or Path.home() / "TradeHelperData"))
        return cls(
            work_dir=work_dir.expanduser(),
            llm_base_url=str(values.get("llm_base_url") or ""),
            llm_api_key=str(values.get("llm_api_key") or ""),
            llm_model=str(values.get("llm_model") or ""),
            stock_token_us=str(values.get("stock_token_us") or ""),
            stock_token_a=str(values.get("stock_token_a") or ""),
            news_token_us=str(values.get("news_token_us") or ""),
            news_token_a=str(values.get("news_token_a") or ""),
            finbert_model_path=str(values.get("finbert_model_path") or ""),
            llm_enable_thinking=bool(values.get("llm_enable_thinking", False)),
        )

    @classmethod
    def default_path(cls) -> Path:
        return _default_config_dir() / "config_v2.json"

    @classmethod
    def load(cls, path: Path | None = None) -> "V2Settings":
        config_path = path or cls.default_path()
        if not config_path.exists():
            return cls.from_mapping({})
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid V2 settings file: {config_path}") from exc
        if not isinstance(loaded, dict):
            raise ValueError("V2 settings root must be a JSON object")
        return cls.from_mapping(loaded)

    @property
    def database_path(self) -> Path:
        return self.work_dir / "tradehelper_v2.db"

    def to_public_mapping(self) -> dict[str, Any]:
        """Return a persisted mapping without exposing any secret values."""
        return {
            "work_dir": str(self.work_dir),
            "llm_base_url": self.llm_base_url,
            "llm_model": self.llm_model,
            "finbert_model_path": self.finbert_model_path,
            "llm_enable_thinking": self.llm_enable_thinking,
        }

    def save(self, path: Path | None = None) -> None:
        config_path = path or self.default_path()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            key: (str(self.work_dir) if key == "work_dir" else getattr(self, key))
            for key in _SETTING_KEYS
        }
        # 原子替换避免断电留下半个 JSON；密钥配置文件仅允许当前用户读取。
        temporary = config_path.with_suffix(config_path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(config_path)
        try:
            os.chmod(config_path, 0o600)
        except OSError:
            pass
