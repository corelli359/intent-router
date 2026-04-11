"""Backward-compatible shim for account_balance_agent.service legacy alias."""

from account_balance_agent.service import AccountBalanceAgentRequest as OrderStatusAgentRequest
from account_balance_agent.service import AccountBalanceAgentService as OrderStatusAgentService
from account_balance_agent.service import AccountBalanceResolution as OrderStatusResolution

__all__ = ["OrderStatusAgentRequest", "OrderStatusAgentService", "OrderStatusResolution"]
