# agent services

Each built-in agent now has its own deployable package directory under `backend/services/agents`.

## Canonical packages

- `account-balance-agent` -> `account_balance_agent`
- `transfer-money-agent` -> `transfer_money_agent`
- `credit-card-repayment-agent` -> `credit_card_repayment_agent`
- `gas-bill-agent` -> `gas_bill_agent`
- `forex-agent` -> `forex_agent`
- `fallback-agent` -> `fallback_agent`

## Local install examples

```bash
python -m pip install backend/services/agents/account-balance-agent
python -m pip install backend/services/agents/transfer-money-agent
```

## Local run examples

```bash
python -m uvicorn account_balance_agent.app:app --reload --port 8101
python -m uvicorn transfer_money_agent.app:app --reload --port 8102
```

Each agent package is deployed and versioned independently; there is no aggregate agent shim layer.
