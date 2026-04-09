from __future__ import annotations

import json
import logging
from typing import Protocol

from router_core.llm_client import JsonLLMClient, llm_exception_is_retryable
from router_core.prompt_templates import (
    DEFAULT_V2_PROACTIVE_RECOMMENDATION_HUMAN_PROMPT,
    DEFAULT_V2_PROACTIVE_RECOMMENDATION_SYSTEM_PROMPT,
    build_v2_proactive_recommendation_prompt,
)
from router_core.v2_domain import (
    ProactiveRecommendationPayload,
    ProactiveRecommendationRouteDecision,
    ProactiveRecommendationRouteMode,
)


logger = logging.getLogger(__name__)


class ProactiveRecommendationRouter(Protocol):
    async def decide(
        self,
        *,
        message: str,
        proactive_recommendation: ProactiveRecommendationPayload,
    ) -> ProactiveRecommendationRouteDecision: ...


class NullProactiveRecommendationRouter:
    async def decide(
        self,
        *,
        message: str,
        proactive_recommendation: ProactiveRecommendationPayload,
    ) -> ProactiveRecommendationRouteDecision:
        del message, proactive_recommendation
        return ProactiveRecommendationRouteDecision(
            route_mode=ProactiveRecommendationRouteMode.SWITCH_TO_FREE_DIALOG,
            reason="推荐模式语义分流器未启用，退回自由对话模式",
        )


class LLMProactiveRecommendationRouter:
    def __init__(
        self,
        llm_client: JsonLLMClient,
        *,
        model: str | None = None,
        fallback: ProactiveRecommendationRouter | None = None,
        system_prompt_template: str = DEFAULT_V2_PROACTIVE_RECOMMENDATION_SYSTEM_PROMPT,
        human_prompt_template: str = DEFAULT_V2_PROACTIVE_RECOMMENDATION_HUMAN_PROMPT,
    ) -> None:
        self.llm_client = llm_client
        self.model = model
        self.fallback = fallback or NullProactiveRecommendationRouter()
        self.prompt = build_v2_proactive_recommendation_prompt(
            system_prompt=system_prompt_template,
            human_prompt=human_prompt_template,
        )

    async def decide(
        self,
        *,
        message: str,
        proactive_recommendation: ProactiveRecommendationPayload,
    ) -> ProactiveRecommendationRouteDecision:
        try:
            raw_payload = await self.llm_client.run_json(
                prompt=self.prompt,
                variables={
                    "message": message,
                    "intro_text": proactive_recommendation.intro_text or "",
                    "recommendation_items_json": json.dumps(
                        [item.model_dump(mode="json", by_alias=True) for item in proactive_recommendation.items],
                        ensure_ascii=False,
                        indent=2,
                    ),
                },
                model=self.model,
            )
            decision = ProactiveRecommendationRouteDecision.model_validate(raw_payload)
        except Exception as exc:
            if llm_exception_is_retryable(exc):
                raise
            logger.warning("Proactive recommendation router failed, falling back", exc_info=True)
            return await self.fallback.decide(
                message=message,
                proactive_recommendation=proactive_recommendation,
            )

        selected_ids = {item.recommendation_item_id for item in proactive_recommendation.items}
        decision.selected_recommendation_ids = [
            recommendation_id
            for recommendation_id in decision.selected_recommendation_ids
            if recommendation_id in selected_ids
        ]
        if (
            decision.route_mode in {
                ProactiveRecommendationRouteMode.DIRECT_EXECUTE,
                ProactiveRecommendationRouteMode.INTERACTIVE_GRAPH,
            }
            and not decision.selected_recommendation_ids
        ):
            decision.route_mode = ProactiveRecommendationRouteMode.NO_SELECTION
            decision.reason = decision.reason or "未识别到明确的推荐项选择"
        if not decision.reason:
            decision.reason = decision.route_mode.value
        return decision
