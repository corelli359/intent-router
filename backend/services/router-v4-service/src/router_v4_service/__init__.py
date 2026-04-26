from __future__ import annotations

__all__ = ["create_app"]


def create_app():
    from router_v4_service.api.app import create_app as _create_app

    return _create_app()
