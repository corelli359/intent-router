import logging


logging.getLogger("router_service").setLevel(logging.INFO)
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

from router_service.api.app import app, create_router_app

__all__ = ["app", "create_router_app"]
