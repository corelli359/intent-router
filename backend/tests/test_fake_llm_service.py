from __future__ import annotations

import httpx
import pytest

from fake_llm_service.api.app import app


@pytest.mark.asyncio
async def test_fake_llm_perf_recognizer_fast_path_returns_transfer_match() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "fake-router-llm",
                "stream": False,
                "messages": [
                    {"role": "system", "content": "你是一个多意图识别器。"},
                    {
                        "role": "user",
                        "content": (
                            "当前消息:\n给小明转500元\n\n"
                            "最近对话(JSON):\n[]\n\n"
                            "长期记忆(JSON):\n[]\n\n"
                            "已注册意图清单(JSON):\n[]"
                        ),
                    },
                ],
            },
        )

    assert response.status_code == 200
    payload = response.json()
    content = payload["choices"][0]["message"]["content"]
    assert '"intent_code":"AG_TRANS"' in content
    assert '"confidence":0.96' in content


@pytest.mark.asyncio
async def test_fake_llm_perf_slot_fast_path_returns_transfer_slots() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "fake-router-llm",
                "stream": False,
                "messages": [
                    {
                        "role": "system",
                        "content": "你是路由层的槽位抽取器，只为单个 leaf intent 抽取槽位。",
                    },
                    {
                        "role": "user",
                        "content": (
                            "当前消息:\n给小明转500元\n\n"
                            "当前节点原始片段:\n给小明转500元\n\n"
                            "意图定义(JSON):\n"
                            '{"intent_code":"AG_TRANS","slot_schema":[{"slot_key":"payee_name"},{"slot_key":"amount"}]}\n\n'
                            "已有槽位(JSON):\n{}\n\n"
                            "请输出 JSON:\n"
                        ),
                    },
                ],
            },
        )

    assert response.status_code == 200
    payload = response.json()
    content = payload["choices"][0]["message"]["content"]
    assert '"slot_key":"payee_name"' in content
    assert '"value":"小明"' in content
    assert '"slot_key":"amount"' in content
    assert '"value":"500"' in content
