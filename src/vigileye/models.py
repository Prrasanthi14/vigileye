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


class ReadingInput(BaseModel):
    """One day of biometrics for a pilot."""

    date: date
    report_time: str = Field(default="08:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    total_sleep_hours: float = Field(ge=0, le=24)
    deep_sleep_pct: float = Field(ge=0, le=100)
    rem_sleep_pct: float = Field(ge=0, le=100)
    light_sleep_pct: float = Field(ge=0, le=100)
    awake_during_sleep_pct: float = Field(ge=0, le=100)
    hrv_ms: float = Field(ge=0, le=300)
    resting_hr: int = Field(ge=25, le=200)
    time_awake_since_last_sleep: float = Field(ge=0, le=48)
    consecutive_duty_days: int = Field(ge=0, le=60)


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
