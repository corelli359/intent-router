import logging


logging.getLogger("router_service").setLevel(logging.INFO)
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

app = None


def create_router_app():
    """Import the FastAPI app lazily so utility scripts can import router modules without web deps."""
    from router_service.api.app import create_router_app as _create_router_app

    return _create_router_app()


try:
    from router_service.api.app import app as _app
except ModuleNotFoundError as exc:
    if exc.name not in {"fastapi", "starlette"}:
        raise
else:
    app = _app

__all__ = ["app", "create_router_app"]
