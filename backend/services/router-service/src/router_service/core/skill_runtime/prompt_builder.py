from __future__ import annotations

import json
from typing import Any

from router_service.core.skill_runtime.models import SkillIndexEntry, SkillSessionState


class SkillPromptBuilder:
    """Build the controller context that would be sent to an LLM planner."""

    def build(
        self,
        *,
        agent_rules: str,
        user_profile: dict[str, Any],
        page_context: dict[str, Any],
        session: SkillSessionState,
        skill_index: list[SkillIndexEntry],
    ) -> str:
        index_payload = [
            {
                "skill_id": item.skill_id,
                "name": item.name,
                "description": item.description,
                "keywords": list(item.keywords),
                "risk_level": item.risk_level,
            }
            for item in skill_index
            if item.status == "active"
        ]
        sections = [
            "# Agent Rules",
            agent_rules.strip(),
            "# Injected Context",
            json.dumps(
                {
                    "user_profile": user_profile,
                    "page_context": page_context,
                    "session": {
                        "session_id": session.session_id,
                        "current_skill_id": session.current_skill_id,
                        "awaiting_slot": session.awaiting_slot,
                        "pending_confirmation": bool(session.pending_confirmation),
                        "summary": session.summary,
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            "# Skill Index",
            json.dumps(index_payload, ensure_ascii=False, sort_keys=True),
        ]
        return "\n\n".join(sections)
