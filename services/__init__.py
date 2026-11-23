"""服务层：LLM / 生图客户端与提示词。"""
from .llm import LLMClient, MockLLM
from .prompts import (
    ASSET_BASE_RULES,
    ASSET_DECORATION_HINT,
    ASSET_HERO_SUFFIX,
    ASSET_KIND_HINTS,
    BUILDER_SYSTEM,
    LAYOUT_CONTRACT,
    PLANNER_SYSTEM,
    PRIMITIVE_CATALOG,
    REVIEWER_SYSTEM,
    REWORK_SYSTEM,
    VISION_REVIEW_BASE,
    VISION_REVIEW_SYSTEM,
    asset_prompt,
    planner_user,
    rework_user,
    vision_review_system,
    vision_review_user,
)

__all__ = [
    "LLMClient", "MockLLM", "PLANNER_SYSTEM", "REWORK_SYSTEM", "BUILDER_SYSTEM",
    "REVIEWER_SYSTEM", "VISION_REVIEW_SYSTEM", "VISION_REVIEW_BASE",
    "LAYOUT_CONTRACT", "PRIMITIVE_CATALOG",
    "ASSET_HERO_SUFFIX", "ASSET_DECORATION_HINT", "ASSET_BASE_RULES", "ASSET_KIND_HINTS",
    "planner_user", "rework_user", "asset_prompt",
    "vision_review_system", "vision_review_user",
]
