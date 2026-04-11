from __future__ import annotations

import asyncio

import httpx

from credit_card_repayment_agent.service import (  # noqa: E402
    CreditCardRepaymentAgentRequest,
    CreditCardRepaymentAgentService,
)
from credit_card_repayment_agent.app import create_app as create_credit_card_repayment_app  # noqa: E402
from forex_agent.app import create_app as create_forex_exchange_app  # noqa: E402
from forex_agent.service import ForexExchangeAgentRequest, ForexExchangeAgentService  # noqa: E402
from gas_bill_agent.app import create_app as create_gas_bill_payment_app  # noqa: E402
from gas_bill_agent.service import (  # noqa: E402
    GasBillPaymentAgentRequest,
    GasBillPaymentAgentService,
)


def test_credit_card_repayment_service_completes_with_required_slots() -> None:
    async def run() -> None:
        service = CreditCardRepaymentAgentService()
        response = await service.handle(
            CreditCardRepaymentAgentRequest(
                sessionId="session_cc_001",
                taskId="task_cc_001",
                input="信用卡卡号 6222021234567890，尾号 1234",
                conversation={"recentMessages": ["user: 查信用卡还款信息"], "longTermMemory": []},
            )
        )

        assert response.status == "completed"
        assert response.payload["due_amount"] == 3200
        assert response.slot_memory["card_number"] == "6222021234567890"

    asyncio.run(run())


def test_gas_bill_payment_service_waits_for_missing_amount() -> None:
    async def run() -> None:
        service = GasBillPaymentAgentService()
        response = await service.handle(
            GasBillPaymentAgentRequest(
                sessionId="session_gas_001",
                taskId="task_gas_001",
                input="帮我交天然气费，户号 88001234",
                gas={"accountNumber": "88001234"},
                conversation={"recentMessages": ["user: 帮我交天然气费"], "longTermMemory": []},
            )
        )

        assert response.status == "waiting_user_input"
        assert response.payload["missing_fields"] == ["amount"]

    asyncio.run(run())


def test_forex_exchange_service_completes_with_currency_pair() -> None:
    async def run() -> None:
        service = ForexExchangeAgentService()
        response = await service.handle(
            ForexExchangeAgentRequest(
                sessionId="session_fx_001",
                taskId="task_fx_001",
                input="卡号 6222021234567890，尾号 1234，把1000人民币换成美元",
                account={"cardNumber": "6222021234567890", "phoneLast4": "1234"},
                exchange={"sourceCurrency": "CNY", "targetCurrency": "USD", "amount": "1000"},
                conversation={"recentMessages": ["user: 把1000人民币换成美元"], "longTermMemory": []},
            )
        )

        assert response.status == "completed"
        assert response.payload["source_currency"] == "CNY"
        assert response.payload["target_currency"] == "USD"
        assert response.payload["exchanged_amount"] == "140.00"

    asyncio.run(run())


def test_credit_card_repayment_app_accepts_credit_card_request() -> None:
    async def run() -> None:
        app = create_credit_card_repayment_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/agent/run",
                json={
                    "sessionId": "session_order_001",
                    "taskId": "task_order_001",
                    "input": "信用卡卡号 6222021234567890，尾号 1234",
                    "creditCard": {"cardNumber": "6222021234567890", "phoneLast4": "1234"},
                    "conversation": {"recentMessages": [], "longTermMemory": []},
                },
            )

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "completed"
        assert payload["payload"]["agent"] == "query_credit_card_repayment"

    asyncio.run(run())


def test_gas_bill_payment_app_accepts_gas_bill_request() -> None:
    async def run() -> None:
        app = create_gas_bill_payment_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/agent/run",
                json={
                    "sessionId": "session_payment_001",
                    "taskId": "task_payment_001",
                    "input": "燃气户号 88001234，缴费 88 元",
                    "gas": {"accountNumber": "88001234"},
                    "payment": {"amount": "88"},
                    "conversation": {"recentMessages": [], "longTermMemory": []},
                },
            )

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "completed"
        assert payload["payload"]["agent"] == "pay_gas_bill"

    asyncio.run(run())


def test_forex_exchange_app_accepts_forex_request() -> None:
    async def run() -> None:
        app = create_forex_exchange_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/agent/run",
                json={
                    "sessionId": "session_forex_001",
                    "taskId": "task_forex_001",
                    "input": "卡号 6222021234567890，尾号 1234，把1000人民币换成美元",
                    "account": {"cardNumber": "6222021234567890", "phoneLast4": "1234"},
                    "exchange": {"sourceCurrency": "CNY", "targetCurrency": "USD", "amount": "1000"},
                    "conversation": {"recentMessages": [], "longTermMemory": []},
                },
            )

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "completed"
        assert payload["payload"]["agent"] == "exchange_forex"

    asyncio.run(run())


def test_credit_card_repayment_service_executes_directly_with_prefilled_slots_and_empty_input() -> None:
    async def run() -> None:
        service = CreditCardRepaymentAgentService()
        response = await service.handle(
            CreditCardRepaymentAgentRequest(
                sessionId="session_cc_direct_001",
                taskId="task_cc_direct_001",
                input="",
                creditCard={"cardNumber": "6222021234567890", "phoneLast4": "1234"},
                conversation={"recentMessages": [], "longTermMemory": []},
            )
        )

        assert response.status == "completed"
        assert response.payload["agent"] == "query_credit_card_repayment"

    asyncio.run(run())


def test_gas_bill_payment_service_executes_directly_with_prefilled_slots_and_empty_input() -> None:
    async def run() -> None:
        service = GasBillPaymentAgentService()
        response = await service.handle(
            GasBillPaymentAgentRequest(
                sessionId="session_gas_direct_001",
                taskId="task_gas_direct_001",
                input="",
                gas={"accountNumber": "88001234"},
                payment={"amount": "88"},
                conversation={"recentMessages": [], "longTermMemory": []},
            )
        )

        assert response.status == "completed"
        assert response.payload["agent"] == "pay_gas_bill"

    asyncio.run(run())


def test_forex_exchange_service_executes_directly_with_prefilled_slots_and_empty_input() -> None:
    async def run() -> None:
        service = ForexExchangeAgentService()
        response = await service.handle(
            ForexExchangeAgentRequest(
                sessionId="session_fx_direct_001",
                taskId="task_fx_direct_001",
                input="",
                account={"cardNumber": "6222021234567890", "phoneLast4": "1234"},
                exchange={"sourceCurrency": "CNY", "targetCurrency": "USD", "amount": "1000"},
                conversation={"recentMessages": [], "longTermMemory": []},
            )
        )

        assert response.status == "completed"
        assert response.payload["agent"] == "exchange_forex"

    asyncio.run(run())
