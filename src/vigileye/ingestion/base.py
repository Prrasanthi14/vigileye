"""Provider-agnostic wearable ingestion.

Every biometric source — a real tracker vendor or the synthetic generator —
implements `WearableProvider`, so swapping vendors is a config change rather
than a rewrite of the pipeline.
"""

from abc import ABC, abstractmethod
from datetime import date

from pydantic import BaseModel, Field


class DailyReading(BaseModel):
    """One pilot-day of biometrics, normalized across vendors."""

    driver_id: str
    date: date
    report_time: str
    total_sleep_hours: float = Field(ge=0, le=24)
    deep_sleep_pct: float = Field(ge=0, le=100)
    rem_sleep_pct: float = Field(ge=0, le=100)
    light_sleep_pct: float = Field(ge=0, le=100)
    awake_during_sleep_pct: float = Field(ge=0, le=100)
    hrv_ms: float = Field(ge=0)
    resting_hr: int = Field(ge=25, le=200)
    time_awake_since_last_sleep: float = Field(ge=0)
    consecutive_duty_days: int = Field(ge=0)


class WearableProvider(ABC):
    """A source of pilot biometrics."""

    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    def fetch_readings(self, driver_ids: list[str], start: date, end: date) -> list[DailyReading]:
        """Readings for the given pilots over an inclusive date range."""
