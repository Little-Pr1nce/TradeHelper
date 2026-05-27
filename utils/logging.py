"""
日志配置工具 — 统一的应用日志初始化。
"""

import logging
from pathlib import Path


def setup_logging(work_dir: str):
    """
    配置应用全局日志系统。

    日志同时输出到：
      - 文件：{work_dir}/logs/tradehelper.log（UTF-8 编码，追加模式）
      - 控制台：标准输出
    """
    log_dir = Path(work_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "tradehelper.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
