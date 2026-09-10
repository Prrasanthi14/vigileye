"""Shared domain models for pilot readiness evaluation."""

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

ReadinessStatus = Literal["CLEAR", "PENDING_TEST", "GROUNDED"]


class ReadinessEvaluation(BaseModel):
    status: ReadinessStatus
    score: int = Field(ge=0, le=100)
    reasoning: str
    recommended_action: str
    risk_factors: list[str]
    circadian_note: str


class Evaluation(ReadinessEvaluation):
    """A readiness verdict plus provenance of which engine produced it.

    The dashboard labels AI verdicts differently from deterministic ones, so a
    silent fallback to the rules engine can never be presented as agent output.
    """

    source: Literal["agent", "rules"]
    fallback_reason: str | None = None


class PilotCreate(BaseModel):
    driver_id: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=120)
    role: str = Field(default="Commercial Pilot", max_length=80)
    shift_type: Literal["Day", "Night", "Rotating"] = "Day"
    base_timezone: str = Field(default="UTC", max_length=64)
    medical_history: str = Field(default="None", max_length=200)


class PilotUpdate(BaseModel):
    """Partial roster update; omitted fields are left unchanged."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    role: str | None = Field(default=None, max_length=80)
    shift_type: Literal["Day", "Night", "Rotating"] | None = None
    base_timezone: str | None = Field(default=None, max_length=64)
    medical_history: str | None = Field(default=None, max_length=200)


