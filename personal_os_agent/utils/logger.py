"""
Structured logging setup using structlog.
- JSON renderer → logs/agent-runtime.log (machine-readable)
- ConsoleRenderer (colorized) → stdout (human-readable during demo)
colorama.init() is required for ANSI colors on Windows.
"""
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import colorama
import structlog

_PROVIDER_LOGGERS = frozenset({
    "mcp", "sse_starlette", "openai", "httpcore", "httpx", "urllib3", "google",
    "googleapiclient", "chromadb", "sentence_transformers", "twilio",
})


class _ApplicationLogs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.name.split(".", 1)[0] not in _PROVIDER_LOGGERS


def setup_logging(log_file: str = "logs/agent-runtime.log") -> None:
    """
    Configure structlog with dual output:
      1. Colorized ConsoleRenderer to stdout
      2. JSON renderer to log_file
    Call this once at startup before any agents are created.
    """
    colorama.init(autoreset=True)  # Windows ANSI color support

    # Ensure log directory exists
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    # Shared processors for both renderers
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    # ── File handler (JSON) ───────────────────────────────────────────────────
    file_handler = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(message)s"))
    file_handler.setLevel(logging.INFO)
    file_handler.addFilter(_ApplicationLogs())

    # ── Stdout handler (colorized) ────────────────────────────────────────────
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.addFilter(_ApplicationLogs())

    # Standard library root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    structlog.configure(
        processors=shared_processors
        + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Apply formatter for the file sink (JSON)
    file_formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=shared_processors,
    )
    file_handler.setFormatter(file_formatter)

    # Apply formatter for the console sink (colorized key=value)
    console_formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.dev.ConsoleRenderer(colors=True),
        foreign_pre_chain=shared_processors,
    )
    console_handler.setFormatter(console_formatter)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger bound with the component name."""
    return structlog.get_logger(name)
