"""Ingest biometrics from a medic-supplied spreadsheet (CSV or Excel).

This is an ingestion source, not an edit path. Readings arrive as a batch file
from the party that measured them and are loaded by an operator running a job;
they are never writable through the API, so a reading cannot be altered to
change a go/no-go outcome after the fact.

Expected columns (one row per pilot-day):
    driver_id, date, report_time, total_sleep_hours, deep_sleep_pct,
    rem_sleep_pct, light_sleep_pct, awake_during_sleep_pct, hrv_ms,
    resting_hr, time_awake_since_last_sleep, consecutive_duty_days
"""

import logging
from datetime import date
from pathlib import Path

import pandas as pd
from pydantic import ValidationError

from .base import DailyReading, WearableProvider

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = {
    "driver_id", "date", "total_sleep_hours", "deep_sleep_pct", "rem_sleep_pct",
    "light_sleep_pct", "awake_during_sleep_pct", "hrv_ms", "resting_hr",
    "time_awake_since_last_sleep", "consecutive_duty_days",
}


class SpreadsheetProvider(WearableProvider):
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def name(self) -> str:
        return f"Spreadsheet ({self.path.name})"

    def _load(self) -> pd.DataFrame:
        if self.path.suffix.lower() in {".xlsx", ".xls"}:
            return pd.read_excel(self.path)
        return pd.read_csv(self.path)

    def fetch_readings(self, driver_ids: list[str], start: date, end: date) -> list[DailyReading]:
        df = self._load()

        missing = REQUIRED_COLUMNS - set(df.columns)
        if missing:
            raise ValueError(f"{self.path.name} is missing columns: {sorted(missing)}")

        df["date"] = pd.to_datetime(df["date"]).dt.date
        wanted = set(driver_ids)
        readings, rejected = [], []

        for index, row in df.iterrows():
            record = row.to_dict()
            if wanted and record["driver_id"] not in wanted:
                continue
            if not (start <= record["date"] <= end):
                continue
            record.setdefault("report_time", "08:00")
            try:
                readings.append(DailyReading(**{
                    k: v for k, v in record.items()
                    if k in DailyReading.model_fields
                }))
            except ValidationError as exc:
                # Reject the row rather than coercing it: a biometric outside a
                # plausible range is a measurement error, and silently admitting
                # it would feed a bad go/no-go call.
                rejected.append((index + 2, record.get("driver_id"), exc.error_count()))

        if rejected:
            logger.warning(
                "Rejected %d invalid row(s); first few: %s", len(rejected), rejected[:5]
            )
        logger.info("Accepted %d reading(s) from %s", len(readings), self.path.name)
        return readings
