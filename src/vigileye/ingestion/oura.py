"""Oura Ring adapter.

Chosen as the first real vendor because its v2 API exposes exactly the signals
VigilEye scores — sleep staging plus HRV (RMSSD) — behind a personal access
token, so it can be developed against without an OAuth consent review. Other
vendors (Google Health/Fitbit, Garmin, Whoop) slot in behind the same
`WearableProvider` interface.

Auth: set OURA_ACCESS_TOKEN. One token maps to one ring owner, so a fleet
deployment needs a token per pilot; pass that mapping as `tokens`.
"""

import logging
import os
from datetime import date

import httpx

from .base import DailyReading, WearableProvider

logger = logging.getLogger(__name__)

BASE_URL = "https://api.ouraring.com/v2/usercollection"


class OuraProvider(WearableProvider):
    def __init__(self, tokens: dict[str, str] | None = None, report_times: dict[str, str] | None = None):
        self.tokens = tokens or {}
        self.report_times = report_times or {}
        self._default_token = os.getenv("OURA_ACCESS_TOKEN")

    def name(self) -> str:
        return "Oura Ring (v2 API)"

    def _token_for(self, driver_id: str) -> str | None:
        return self.tokens.get(driver_id) or self._default_token

    def fetch_readings(self, driver_ids: list[str], start: date, end: date) -> list[DailyReading]:
        readings: list[DailyReading] = []

        for driver_id in driver_ids:
            token = self._token_for(driver_id)
            if not token:
                logger.warning("No Oura token for %s; skipping.", driver_id)
                continue

            try:
                response = httpx.get(
                    f"{BASE_URL}/sleep",
                    params={"start_date": start.isoformat(), "end_date": end.isoformat()},
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=30.0,
                )
                response.raise_for_status()
                documents = response.json().get("data", [])
            except httpx.HTTPError as exc:
                logger.error("Oura fetch failed for %s: %s", driver_id, exc)
                continue

            for doc in documents:
                reading = self._to_reading(driver_id, doc)
                if reading:
                    readings.append(reading)

        return readings

    def _to_reading(self, driver_id: str, doc: dict) -> DailyReading | None:
        total_seconds = doc.get("total_sleep_duration") or 0
        if total_seconds <= 0:
            return None

        def pct(key: str) -> float:
            return round((doc.get(key) or 0) * 100.0 / total_seconds, 1)

        return DailyReading(
            driver_id=driver_id,
            date=date.fromisoformat(doc["day"]),
            report_time=self.report_times.get(driver_id, "08:00"),
            total_sleep_hours=round(total_seconds / 3600.0, 2),
            deep_sleep_pct=pct("deep_sleep_duration"),
            rem_sleep_pct=pct("rem_sleep_duration"),
            light_sleep_pct=pct("light_sleep_duration"),
            awake_during_sleep_pct=pct("awake_time"),
            hrv_ms=float(doc.get("average_hrv") or 0),
            resting_hr=int(doc.get("lowest_heart_rate") or doc.get("average_heart_rate") or 60),
            # Oura reports sleep, not duty; the roster system owns these two.
            time_awake_since_last_sleep=0.0,
            consecutive_duty_days=0,
        )
