from __future__ import annotations

import logging
from logging.handlers import QueueHandler

from router_service.logging_utils import (
    ROUTER_ASYNC_LOGGING_ENABLED_ENV,
    bind_router_logger_to_runtime_handlers,
    bootstrap_router_logging,
    resolve_log_level,
    stop_router_logging_listener,
)


def _snapshot_logger_state(logger: logging.Logger) -> dict[str, object]:
    """Capture enough logger state to restore it after a test mutates global logging."""
    return {
        "handlers": list(logger.handlers),
        "level": logger.level,
        "propagate": logger.propagate,
    }


def _restore_logger_state(logger: logging.Logger, snapshot: dict[str, object]) -> None:
    """Restore the logger state captured by `_snapshot_logger_state`."""
    logger.handlers = list(snapshot["handlers"])
    logger.setLevel(int(snapshot["level"]))
    logger.propagate = bool(snapshot["propagate"])


def test_resolve_log_level_supports_text_and_numeric_values() -> None:
    assert resolve_log_level("info") == logging.INFO
    assert resolve_log_level("WARN") == logging.WARNING
    assert resolve_log_level("10") == logging.DEBUG
    assert resolve_log_level(None) == logging.INFO


def test_bootstrap_router_logging_installs_info_handler() -> None:
    package_logger = logging.getLogger("router_service")
    snapshot = _snapshot_logger_state(package_logger)
    try:
        package_logger.handlers = []
        package_logger.setLevel(logging.NOTSET)
        package_logger.propagate = True

        logger = bootstrap_router_logging("INFO")

        assert logger is package_logger
        assert logger.level == logging.INFO
        assert logger.propagate is False
        assert len(logger.handlers) == 1
        assert logger.handlers[0].level == logging.INFO
    finally:
        _restore_logger_state(package_logger, snapshot)


def test_bind_router_logger_to_runtime_handlers_reuses_uvicorn_handler() -> None:
    package_logger = logging.getLogger("router_service")
    uvicorn_error_logger = logging.getLogger("uvicorn.error")
    package_snapshot = _snapshot_logger_state(package_logger)
    uvicorn_snapshot = _snapshot_logger_state(uvicorn_error_logger)
    try:
        runtime_handler = logging.StreamHandler()
        uvicorn_error_logger.handlers = [runtime_handler]
        uvicorn_error_logger.setLevel(logging.INFO)

        logger = bind_router_logger_to_runtime_handlers("INFO", async_enabled=False)

        assert logger is package_logger
        assert logger.handlers == [runtime_handler]
        assert logger.level == logging.INFO
        assert logger.propagate is False
    finally:
        stop_router_logging_listener(package_logger)
        _restore_logger_state(package_logger, package_snapshot)
        _restore_logger_state(uvicorn_error_logger, uvicorn_snapshot)


def test_bind_router_logger_to_runtime_handlers_supports_async_queue(monkeypatch) -> None:
    package_logger = logging.getLogger("router_service")
    uvicorn_error_logger = logging.getLogger("uvicorn.error")
    package_snapshot = _snapshot_logger_state(package_logger)
    uvicorn_snapshot = _snapshot_logger_state(uvicorn_error_logger)
    try:
        runtime_handler = logging.StreamHandler()
        uvicorn_error_logger.handlers = [runtime_handler]
        uvicorn_error_logger.setLevel(logging.INFO)
        monkeypatch.setenv(ROUTER_ASYNC_LOGGING_ENABLED_ENV, "true")

        logger = bind_router_logger_to_runtime_handlers("INFO")

        assert logger is package_logger
        assert len(logger.handlers) == 1
        assert isinstance(logger.handlers[0], QueueHandler)
        assert logger.level == logging.INFO
        assert logger.propagate is False
    finally:
        stop_router_logging_listener(package_logger)
        _restore_logger_state(package_logger, package_snapshot)
        _restore_logger_state(uvicorn_error_logger, uvicorn_snapshot)
