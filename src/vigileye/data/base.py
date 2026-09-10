"""Storage-agnostic interface for pilot biometric data."""

from abc import ABC, abstractmethod
from typing import Any

import pandas as pd

# Columns shared by the SQLite and BigQuery implementations, kept here so the
# two backends cannot drift apart.
LATEST_COLUMNS = (
    "p.driver_id, p.name, p.role, p.shift_type, p.base_timezone as report_timezone, "
    "p.medical_history, d.date as last_sync_timestamp, d.report_time, d.total_sleep_hours, "
    "d.deep_sleep_pct, d.rem_sleep_pct, d.light_sleep_pct, d.awake_during_sleep_pct, "
    "d.hrv_ms, d.resting_hr, d.time_awake_since_last_sleep, d.consecutive_duty_days"
)

HISTORY_COLUMNS = (
    "date, total_sleep_hours, hrv_ms, resting_hr, consecutive_duty_days, report_time"
)


class DataConnector(ABC):
    @abstractmethod
    def fetch_latest_data(self, driver_id: str) -> dict[str, Any]:
        """Most recent reading for one pilot, or {} if unknown."""

    @abstractmethod
    def get_pilot_history(self, driver_id: str, days: int = 30) -> pd.DataFrame:
        """Trailing daily readings, oldest first."""

    @abstractmethod
    def get_fleet_summary(self) -> pd.DataFrame:
        """Latest reading per pilot across the fleet."""

    @abstractmethod
    def pilot_exists(self, driver_id: str) -> bool:
        ...

    @abstractmethod
    def create_pilot(self, pilot: dict[str, Any]) -> None:
        ...

    @abstractmethod
    def update_pilot(self, driver_id: str, fields: dict[str, Any]) -> None:
        """Apply a partial roster update. `fields` must be non-empty."""

    @abstractmethod
    def delete_pilot(self, driver_id: str) -> None:
        """Remove a pilot and their readings."""

    @abstractmethod
    def get_source_name(self) -> str:
        ...

    @abstractmethod
    def is_connected(self) -> bool:
        ...
