from __future__ import annotations

import logging

from router_service.core.support.trace_logging import current_trace_id, router_stage, router_trace


def _snapshot_logger_state(logger: logging.Logger) -> dict[str, object]:
    """Capture enough logger state to restore it after a test mutates logging."""
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


def test_router_trace_and_stage_emit_correlated_logs(caplog) -> None:
    logger = logging.getLogger("router_service.tests.trace_logging")
    snapshot = _snapshot_logger_state(logger)
    try:
        logger.handlers = [caplog.handler]
        logger.setLevel(logging.INFO)
        logger.propagate = False

        with router_trace(
            logger,
            entrypoint="test_entrypoint",
            session_id="session_demo",
            cust_id="cust_demo",
            content="帮我给小红转200",
            details={"router_only": True},
        ):
            assert current_trace_id() is not None
            with router_stage(logger, "demo.stage", node_id="node_demo"):
                pass

        assert current_trace_id() is None
    finally:
        _restore_logger_state(logger, snapshot)

    assert "Router trace started" in caplog.text
    assert "Router trace completed" in caplog.text
    assert "Router stage started" in caplog.text
    assert "Router stage completed" in caplog.text
    assert "entrypoint=test_entrypoint" in caplog.text
    assert "stage=demo.stage" in caplog.text
