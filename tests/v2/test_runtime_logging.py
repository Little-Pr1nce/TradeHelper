from __future__ import annotations

import logging
import os

from runtime.logging_config import (
    LOG_FILE_NAME,
    configure_runtime_logging,
    shutdown_runtime_logging,
)


def test_runtime_log_is_written_and_credentials_are_redacted(tmp_path):
    root = logging.getLogger()
    previous_level = root.level
    try:
        path = configure_runtime_logging(tmp_path, console=False)
        logger = logging.getLogger("tradehelper.test.logging")
        logger.info(
            "provider failed api_key=visible-secret Authorization: Bearer bearer-secret sk-abcdefghijklmnop"
        )
        try:
            raise RuntimeError("stock_token_us=exception-secret")
        except RuntimeError:
            logger.exception("request failed")
        shutdown_runtime_logging()

        content = path.read_text(encoding="utf-8")
        assert path == tmp_path / "logs" / LOG_FILE_NAME
        assert "provider failed" in content and "request failed" in content
        assert "visible-secret" not in content
        assert "bearer-secret" not in content
        assert "abcdefghijklmnop" not in content
        assert "exception-secret" not in content
        assert "<redacted>" in content
        if os.name != "nt":
            assert path.stat().st_mode & 0o777 == 0o600
            assert path.parent.stat().st_mode & 0o777 == 0o700
    finally:
        shutdown_runtime_logging()
        root.setLevel(previous_level)


def test_runtime_logging_is_idempotent_and_rotates(tmp_path):
    root = logging.getLogger()
    previous_level = root.level
    try:
        configure_runtime_logging(tmp_path, max_bytes=350, backup_count=2, console=False)
        configure_runtime_logging(tmp_path, max_bytes=350, backup_count=2, console=False)
        logger = logging.getLogger("tradehelper.test.rotation")
        for index in range(40):
            logger.info("rotation-line-%02d %s", index, "x" * 60)
        logger.info("single-marker")
        shutdown_runtime_logging()

        files = tuple(sorted((tmp_path / "logs").glob(f"{LOG_FILE_NAME}*")))
        assert 2 <= len(files) <= 3
        combined = "".join(item.read_text(encoding="utf-8") for item in files)
        assert combined.count("single-marker") == 1
        assert any(item.name.endswith(".1") for item in files)
        if os.name != "nt":
            assert all(item.stat().st_mode & 0o777 == 0o600 for item in files)
    finally:
        shutdown_runtime_logging()
        root.setLevel(previous_level)


def test_runtime_logging_supports_diagnostic_level_and_suppresses_yfinance_noise(
    tmp_path, monkeypatch,
):
    root = logging.getLogger()
    previous_level = root.level
    previous_yfinance_level = logging.getLogger("yfinance").level
    monkeypatch.setenv("TRADEHELPER_LOG_LEVEL", "DEBUG")
    try:
        path = configure_runtime_logging(tmp_path, console=False)
        logging.getLogger("tradehelper.test.diagnostic").debug("diagnostic-detail")
        logging.getLogger("yfinance").error("possibly delisted fallback noise")
        shutdown_runtime_logging()

        content = path.read_text(encoding="utf-8")
        assert "diagnostic-detail" in content
        assert "possibly delisted fallback noise" not in content
        assert "test_runtime_logging.py:" in content
    finally:
        shutdown_runtime_logging()
        root.setLevel(previous_level)
        logging.getLogger("yfinance").setLevel(previous_yfinance_level)
