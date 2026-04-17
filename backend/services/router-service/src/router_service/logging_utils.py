from __future__ import annotations

import logging
import os


ROUTER_LOG_LEVEL_ENV = "ROUTER_LOG_LEVEL"
DEFAULT_ROUTER_LOG_LEVEL = "INFO"
DEFAULT_ROUTER_LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"


def resolve_log_level(level_name: str | None) -> int:
    """Resolve a textual or numeric log level into a logging module constant."""
    if level_name is None:
        return logging.INFO
    normalized = str(level_name).strip()
    if not normalized:
        return logging.INFO
    if normalized.isdigit():
        return int(normalized)
    upper = normalized.upper()
    mapping = logging.getLevelNamesMapping()
    if upper == "WARN":
        upper = "WARNING"
    return mapping.get(upper, logging.INFO)


def configured_router_log_level(default: str = DEFAULT_ROUTER_LOG_LEVEL) -> int:
    """Read the router log level from the environment."""
    return resolve_log_level(os.getenv(ROUTER_LOG_LEVEL_ENV, default))


def _stream_handler(level: int) -> logging.Handler:
    """Create a plain stdout/stderr stream handler for router logs."""
    handler = logging.StreamHandler()
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(DEFAULT_ROUTER_LOG_FORMAT))
    return handler


def bootstrap_router_logging(level_name: str | None = None) -> logging.Logger:
    """Ensure router package logs are visible even before FastAPI/uvicorn fully boots."""
    level = resolve_log_level(level_name) if level_name is not None else configured_router_log_level()
    package_logger = logging.getLogger("router_service")
    package_logger.setLevel(level)
    package_logger.propagate = False
    if not package_logger.handlers:
        package_logger.addHandler(_stream_handler(level))
    else:
        for handler in package_logger.handlers:
            handler.setLevel(level)
    return package_logger


def bind_router_logger_to_runtime_handlers(level_name: str | None = None) -> logging.Logger:
    """Bind router logs to uvicorn/root handlers once the ASGI runtime is ready."""
    level = resolve_log_level(level_name) if level_name is not None else configured_router_log_level()
    package_logger = logging.getLogger("router_service")
    runtime_handlers = list(logging.getLogger("uvicorn.error").handlers) or list(logging.getLogger().handlers)
    package_logger.handlers = runtime_handlers or [_stream_handler(level)]
    package_logger.setLevel(level)
    package_logger.propagate = False
    for handler in package_logger.handlers:
        if handler.level not in {logging.NOTSET, level}:
            handler.setLevel(level)
    return package_logger
