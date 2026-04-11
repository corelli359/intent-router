"""Backward-compatible shim for transfer_money_agent.app legacy alias."""

from transfer_money_agent.app import app, create_app
from transfer_money_agent.app import get_transfer_money_service as get_cancel_appointment_service
from transfer_money_agent.app import get_transfer_money_settings as get_cancel_appointment_settings

__all__ = ["app", "create_app", "get_cancel_appointment_service", "get_cancel_appointment_settings"]
