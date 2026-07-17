"""Data contracts for Phoenix Stadium.

Every fact that reaches the LLM layer must pass through one of these
models first. The LLM never invents a gate, a wait time, or a hazard —
it only phrases what the engine already computed.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class Language(StrEnum):
    EN = "en"
    HI = "hi"
    ES = "es"
    FR = "fr"
    PT = "pt"


class Persona(StrEnum):
    FAN = "fan"
    OPS = "ops"


class AccessibilityNeed(StrEnum):
    NONE = "none"
    WHEELCHAIR = "wheelchair"
    VISUAL = "visual"
    HEARING = "hearing"


class GateStatus(BaseModel):
    gate_id: str
    name: str
    capacity_per_min: float = Field(gt=0)
    arrivals_per_min: float = Field(ge=0)
    servers_open: int = Field(ge=0)
    step_free: bool = True
    has_visual_display: bool = True  # LED/screen boards for hearing-impaired wayfinding
    has_audio_guidance: bool = True  # PA/audio system for visually-impaired wayfinding
    incident: str | None = None

    @field_validator("arrivals_per_min")
    @classmethod
    def _non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError("arrivals_per_min cannot be negative")  # pragma: no cover
        return v


class WaitEstimate(BaseModel):
    """Output of the deterministic queueing model — never produced by the LLM."""

    gate_id: str
    predicted_wait_minutes: float
    utilization: float
    congestion_level: str  # "low" | "moderate" | "high" | "critical"
    server_farm_saturated: bool


class UserQuery(BaseModel):
    persona: Persona
    language: Language = Language.EN
    raw_text: str = Field(min_length=1, max_length=500)
    accessibility_need: AccessibilityNeed = AccessibilityNeed.NONE
    location_hint: str | None = Field(default=None, max_length=100)


class ResolvedContext(BaseModel):
    """The ONLY thing handed to the LLM. Free text never reaches it directly
    except as an already-sanitized, already-bounded field."""

    intent: str
    recommended_gate: GateStatus | None = None
    wait_estimate: WaitEstimate | None = None
    alternate_gate: GateStatus | None = None
    alternate_wait: WaitEstimate | None = None
    accessible_route_available: bool = True
    safety_notice: str | None = None
    # Preserved so the LLM layer can tailor accessibility phrasing per need type
    accessibility_need: AccessibilityNeed = AccessibilityNeed.NONE

    sanitized_user_text: str
    language: Language


class AssistantReply(BaseModel):
    text: str
    intent: str
    grounded_facts: list[str]
    language: Language
