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


class PilotSnapshot(BaseModel):
    driver_id: str
    name: str
    role: str = ""
    shift_type: str = ""
    report_time: str = "--:--"
    medical_history: str = "None"
    last_sync_timestamp: date | None = None
    total_sleep_hours: float = 0.0
    deep_sleep_pct: float = 0.0
    rem_sleep_pct: float = 0.0
    hrv_ms: float = 0.0
    resting_hr: int = 65
    time_awake_since_last_sleep: float = 0.0
    consecutive_duty_days: int = 1
    seven_day_avg_sleep: float | None = None
    seven_day_avg_hrv: float | None = None
