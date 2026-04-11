"""Backward-compatible shim for account_balance_agent.app legacy alias."""

from account_balance_agent.app import app, create_app
from account_balance_agent.app import get_account_balance_service as get_order_status_service
from account_balance_agent.app import get_account_balance_settings as get_order_status_settings

__all__ = ["app", "create_app", "get_order_status_service", "get_order_status_settings"]
