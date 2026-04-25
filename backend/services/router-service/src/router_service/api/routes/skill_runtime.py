from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, model_validator

from router_service.api.dependencies import get_skill_runtime
from router_service.core.skill_runtime.models import SkillRuntimeInput
from router_service.core.skill_runtime.runtime import SkillRuntimeController


router = APIRouter(tags=["router-v4-skill-runtime"])


class SkillRuntimeMessageRequest(BaseModel):
    """Request contract for the markdown-first v4 Skill runtime."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    session_id: str = Field(alias="sessionId", min_length=1)
    message: str = Field(min_length=1)
    user_profile: dict[str, Any] = Field(default_factory=dict, alias="userProfile")
    page_context: dict[str, Any] = Field(default_factory=dict, alias="pageContext")
    business_apis: dict[str, str] = Field(default_factory=dict, alias="businessApis")

    @model_validator(mode="after")
    def normalize(self) -> "SkillRuntimeMessageRequest":
        self.message = self.message.strip()
        if not self.message:
            raise ValueError("message is required")
        return self


@router.post("/v4/message", response_model=None)
async def post_skill_runtime_message(
    request: SkillRuntimeMessageRequest,
    runtime: SkillRuntimeController = Depends(get_skill_runtime),
) -> dict[str, Any]:
    """Process one markdown Skill runtime turn."""
    output = runtime.handle(
        SkillRuntimeInput(
            session_id=request.session_id,
            message=request.message,
            user_profile=dict(request.user_profile),
            page_context=dict(request.page_context),
            business_apis=dict(request.business_apis),
        )
    )
    return output.to_dict()
