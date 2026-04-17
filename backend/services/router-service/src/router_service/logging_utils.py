from __future__ import annotations

import logging
import os
from logging.handlers import QueueHandler, QueueListener
from queue import SimpleQueue


ROUTER_ASYNC_LOGGING_ENABLED_ENV = "ROUTER_ASYNC_LOGGING_ENABLED"
_ROUTER_QUEUE_LISTENER_ATTR = "_router_queue_listener"
_ROUTER_QUEUE_HANDLER_ATTR = "_router_queue_handler"


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


def async_router_logging_enabled(default: bool = False) -> bool:
    """Return whether router logs should be decoupled through a queue listener."""
    raw_value = os.getenv(ROUTER_ASYNC_LOGGING_ENABLED_ENV)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


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


def stop_router_logging_listener(logger: logging.Logger | None = None) -> None:
    """Stop any queue listener previously attached to the router package logger."""
    package_logger = logger or logging.getLogger("router_service")
    listener = getattr(package_logger, _ROUTER_QUEUE_LISTENER_ATTR, None)
    if listener is not None:
        listener.stop()
        delattr(package_logger, _ROUTER_QUEUE_LISTENER_ATTR)
    if hasattr(package_logger, _ROUTER_QUEUE_HANDLER_ATTR):
        delattr(package_logger, _ROUTER_QUEUE_HANDLER_ATTR)


def bind_router_logger_to_runtime_handlers(
    level_name: str | None = None,
    *,
    async_enabled: bool | None = None,
) -> logging.Logger:
    """Bind router logs to uvicorn/root handlers once the ASGI runtime is ready."""
    level = resolve_log_level(level_name) if level_name is not None else configured_router_log_level()
    package_logger = logging.getLogger("router_service")
    runtime_handlers = list(logging.getLogger("uvicorn.error").handlers) or list(logging.getLogger().handlers)
    resolved_runtime_handlers = runtime_handlers or [_stream_handler(level)]
    stop_router_logging_listener(package_logger)
    if async_enabled is None:
        async_enabled = async_router_logging_enabled()
    if async_enabled:
        log_queue: SimpleQueue[logging.LogRecord] = SimpleQueue()
        queue_handler = QueueHandler(log_queue)
        queue_handler.setLevel(level)
        listener = QueueListener(
            log_queue,
            *resolved_runtime_handlers,
            respect_handler_level=True,
        )
        listener.start()
        package_logger.handlers = [queue_handler]
        setattr(package_logger, _ROUTER_QUEUE_LISTENER_ATTR, listener)
        setattr(package_logger, _ROUTER_QUEUE_HANDLER_ATTR, queue_handler)
    else:
        package_logger.handlers = resolved_runtime_handlers
    package_logger.setLevel(level)
    package_logger.propagate = False
    for handler in package_logger.handlers:
        if handler.level not in {logging.NOTSET, level}:
            handler.setLevel(level)
    return package_logger
