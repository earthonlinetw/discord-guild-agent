"""結構化日誌設定。

使用 structlog 提供結構化日誌輸出，支援 console（開發）與 json（生產）格式。
整合標準 logging 模組，統一所有日誌輸出。
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog


def setup_logging(level: str = "INFO", log_format: str = "console") -> None:
    """初始化結構化日誌系統。

    Args:
        level: 日誌等級（DEBUG / INFO / WARNING / ERROR）。
        log_format: 輸出格式，console 為開發者友善格式，json 為生產格式。
    """
    # 設定標準 logging 基礎
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )

    # 抑制第三方套件的冗餘日誌
    for noisy in ("discord", "httpx", "openai", "aiosqlite"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # 共用處理器
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        structlog.processors.format_exc_info,
    ]

    if log_format == "json":
        # JSON 生產格式
        renderer = structlog.processors.JSONRenderer()
    else:
        # Console 開發格式（彩色）
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # 設定 Formatter 給標準 logging
    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """取得結構化 Logger 實例。

    Args:
        name: Logger 名稱，通常為模組名。

    Returns:
        結構化 Logger。
    """
    return structlog.get_logger(name)
