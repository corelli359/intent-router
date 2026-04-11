"""Backward-compatible shim for transfer_money_agent.service legacy alias."""

from transfer_money_agent.service import TransferMoneyAgentRequest as CancelAppointmentAgentRequest
from transfer_money_agent.service import TransferMoneyAgentService as CancelAppointmentAgentService
from transfer_money_agent.service import TransferMoneyResolution as CancelAppointmentResolution

__all__ = ["CancelAppointmentAgentRequest", "CancelAppointmentAgentService", "CancelAppointmentResolution"]
