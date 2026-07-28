"""Process-wide, rotating and redacted runtime logging for the desktop app."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import sys


LOG_FILE_NAME = "tradehelper_v2.log"
MAX_LOG_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 4
_HANDLER_MARKER = "_tradehelper_handler"

_SECRET_PATTERNS = (
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+"), "Bearer <redacted>"),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|access[_-]?token|auth(?:orization)?|bearer|"
            r"password|secret|stock_token_(?:us|a)|news_token_(?:us|a)|llm_api_key)"
            r"\b\s*[:=]\s*([^\s,;]+)"
        ),
        r"\1=<redacted>",
    ),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"), "<redacted-api-key>"),
)


def redact_log_text(value: object) -> str:
    """Remove common credential forms from a fully rendered log message."""

    text = str(value)
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFormatter(logging.Formatter):
    """Redact the complete formatted record, including exception text."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_log_text(super().format(record))


class PrivateRotatingFileHandler(RotatingFileHandler):
    """Keep the active log private after initial creation and every rollover."""

    def _open(self):
        stream = super()._open()
        try:
            os.chmod(self.baseFilename, 0o600)
        except OSError:
            pass
        return stream


def _runtime_handlers(root: logging.Logger):
    return tuple(
        handler for handler in root.handlers
        if getattr(handler, _HANDLER_MARKER, False)
    )


def shutdown_runtime_logging() -> None:
    """Flush and detach only handlers installed by this module."""

    root = logging.getLogger()
    for handler in _runtime_handlers(root):
        root.removeHandler(handler)
        handler.flush()
        handler.close()


def configure_runtime_logging(
    work_dir: Path | str,
    *,
    level: int | str | None = None,
    max_bytes: int = MAX_LOG_BYTES,
    backup_count: int = LOG_BACKUP_COUNT,
    console: bool = True,
) -> Path:
    """Install idempotent console and rotating-file handlers.

    The function intentionally keeps third-party/test handlers intact and only
    replaces handlers previously installed by TradeHelper.
    """

    configured_level = level if level is not None else os.environ.get("TRADEHELPER_LOG_LEVEL", "INFO")
    if isinstance(configured_level, str):
        configured_level = getattr(logging, configured_level.strip().upper(), logging.INFO)
    log_dir = Path(work_dir).expanduser() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(log_dir, 0o700)
    except OSError:
        pass
    log_path = log_dir / LOG_FILE_NAME
    shutdown_runtime_logging()

    formatter = RedactingFormatter(
        "%(asctime)s [%(levelname)s] [%(threadName)s] "
        "%(name)s [%(filename)s:%(lineno)d]: %(message)s"
    )
    root = logging.getLogger()
    root.setLevel(configured_level)

    file_handler = PrivateRotatingFileHandler(
        log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(configured_level)
    file_handler.setFormatter(formatter)
    setattr(file_handler, _HANDLER_MARKER, True)
    root.addHandler(file_handler)

    if console:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(configured_level)
        console_handler.setFormatter(formatter)
        setattr(console_handler, _HANDLER_MARKER, True)
        root.addHandler(console_handler)

    logging.captureWarnings(True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # yfinance emits alarming "possibly delisted" ERROR records for an empty
    # fallback window even when the authoritative provider or repository has
    # already supplied valid bars. TradeHelper records the complete provider
    # attempt chain itself, so suppress this misleading third-party wording.
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    return log_path
