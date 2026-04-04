from __future__ import annotations

from langchain_core.prompts import ChatPromptTemplate


DEFAULT_RECOGNIZER_SYSTEM_PROMPT = (
    "你是一个多意图识别器。"
    "只能从已注册 intent 中选择，不能虚构新的 intent_code。"
    "你可以返回多个意图，但必须保持谨慎。"
    "confidence 必须在 0 到 1 之间。"
    "如果当前消息与某个已注册意图明显匹配，不要返回空列表。"
    "优先依据每个 intent 的 description、examples 和关键词边界来判断，而不是只看字面重合。"
)

DEFAULT_RECOGNIZER_HUMAN_PROMPT = (
    "当前消息:\n{message}\n\n"
    "最近对话(JSON):\n{recent_messages_json}\n\n"
    "长期记忆(JSON):\n{long_term_memory_json}\n\n"
    "已注册意图清单(JSON):\n{intents_json}"
)


def build_recognizer_prompt(*, system_prompt: str, human_prompt: str) -> ChatPromptTemplate:
    return ChatPromptTemplate.from_messages(
        [
            ("system", system_prompt),
            ("human", human_prompt),
        ]
    )
