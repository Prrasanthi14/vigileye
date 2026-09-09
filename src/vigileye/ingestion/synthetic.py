"""Synthetic wearable data with realistic physiology.

Public fitness-tracker datasets are a poor fit for VigilEye: the widely used
Fitbit ones carry sleep duration but no HRV or sleep staging, and none carry
aviation duty schedules. This generator produces the full signal set instead,
with the couplings a real tracker would show:

- Day-to-day persistence: yesterday's state carries into today (AR(1)), so a
  pilot degrades over a trip rather than resampling independently each day.
- Sleep debt drives recovery: accumulated short sleep and consecutive duty days
  suppress HRV and raise resting HR.
- Fixed per-pilot traits: baseline HRV and resting HR differ by individual, so
  a "low HRV" reading is only meaningful against that pilot's own norm.
- Night shifts degrade sleep and push report times toward the circadian low.
- Sleep apnea suppresses deep sleep and raises resting HR.
"""

import random
from dataclasses import dataclass
from datetime import date, timedelta

from .base import DailyReading, WearableProvider

PERSISTENCE = 0.75  # AR(1) weight on yesterday's state
DISRUPTION_RATE = 0.07  # chance per pilot-day of an acute disruption


@dataclass
class PilotProfile:
    driver_id: str
    shift_type: str
    medical_history: str
    baseline_hrv: float
    baseline_rhr: int
    sleep_need: float


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class SyntheticProvider(WearableProvider):
    def __init__(self, profiles: list[PilotProfile], seed: int = 42):
        self.profiles = {p.driver_id: p for p in profiles}
        self.rng = random.Random(seed)

    def name(self) -> str:
        return "Synthetic (physiologically modelled)"

    def _report_time(self, profile: PilotProfile) -> str:
        if profile.shift_type == "Night":
            hour = self.rng.choice([22, 23, 0, 1, 2, 3, 4, 5])
        elif profile.shift_type == "Rotating":
            hour = self.rng.choice([5, 6, 7, 14, 15, 22, 23, 2, 3])
        else:
            hour = self.rng.choice([6, 7, 8, 9, 10, 11, 13, 14])
        return f"{hour:02d}:{self.rng.choice(['00', '15', '30', '45'])}"

    def fetch_readings(self, driver_ids: list[str], start: date, end: date) -> list[DailyReading]:
        readings: list[DailyReading] = []

        for driver_id in driver_ids:
            profile = self.profiles[driver_id]
            apnea = "Apnea" in profile.medical_history
            insomnia = "Insomnia" in profile.medical_history

            # Latent "recovery" state in [0,1]; carries across days.
            state = self.rng.uniform(0.4, 0.8)
            sleep_debt = 0.0
            consecutive = self.rng.randint(0, 3)

            day = start
            while day <= end:
                shock = self.rng.gauss(0, 0.14)
                # Acute disruption — illness, a delayed duty, a lost rest opportunity.
                # Real no-go calls come from these, not from the steady state.
                if self.rng.random() < DISRUPTION_RATE:
                    shock -= self.rng.uniform(0.25, 0.5)
                state = _clamp(PERSISTENCE * state + (1 - PERSISTENCE) * 0.65 + shock, 0.05, 1.0)

                # Longer trips erode recovery.
                duty_penalty = min(consecutive, 7) * 0.035
                effective = _clamp(state - duty_penalty, 0.03, 1.0)

                sleep = profile.sleep_need - 2.6 * (1 - effective) + self.rng.gauss(0, 0.45)
                if profile.shift_type == "Night":
                    sleep -= self.rng.uniform(0.3, 1.1)
                if insomnia:
                    sleep -= self.rng.uniform(0.4, 1.2)
                sleep = _clamp(sleep, 2.0, 9.8)

                sleep_debt = _clamp(sleep_debt + (profile.sleep_need - sleep) * 0.5, 0, 12)

                # Sleep architecture: deep suffers first under debt and apnea.
                deep = 11.0 + 11.0 * effective - (4.0 if apnea else 0) + self.rng.gauss(0, 1.5)
                deep = _clamp(deep, 4.0, 26.0)
                rem = 22.0 * (0.72 + 0.28 * effective) + self.rng.gauss(0, 1.8)
                rem = _clamp(rem, 6.0, 29.0)
                awake = _clamp(4.0 + 9.0 * (1 - effective) + (3.0 if apnea else 0)
                               + self.rng.gauss(0, 1.2), 1.0, 22.0)
                light = _clamp(100.0 - deep - rem - awake, 15.0, 80.0)
                total_stage = deep + rem + awake + light
                deep, rem, awake, light = (x * 100.0 / total_stage for x in (deep, rem, awake, light))

                # HRV tracks recovery and sleep debt against the pilot's own baseline.
                hrv = profile.baseline_hrv * (0.55 + 0.55 * effective) - sleep_debt * 1.6
                hrv = _clamp(hrv + self.rng.gauss(0, 3.0), 8.0, 120.0)

                # Resting HR moves inversely to HRV.
                rhr = profile.baseline_rhr + (1 - effective) * 11 + sleep_debt * 0.55
                if apnea:
                    rhr += 4
                rhr = int(_clamp(rhr + self.rng.gauss(0, 2.0), 40, 105))

                report_time = self._report_time(profile)
                report_hour = int(report_time.split(":")[0])
                # Hours awake before reporting, longer on a degraded day.
                awake_hours = _clamp(self.rng.uniform(1.5, 5.0) + (1 - effective) * 12.0
                                     + (4.0 if report_hour in (0, 1, 2, 3, 4, 5) else 0.0),
                                     0.5, 23.0)

                readings.append(DailyReading(
                    driver_id=driver_id,
                    date=day,
                    report_time=report_time,
                    total_sleep_hours=round(sleep, 2),
                    deep_sleep_pct=round(deep, 1),
                    rem_sleep_pct=round(rem, 1),
                    light_sleep_pct=round(light, 1),
                    awake_during_sleep_pct=round(awake, 1),
                    hrv_ms=round(hrv, 1),
                    resting_hr=rhr,
                    time_awake_since_last_sleep=round(awake_hours, 1),
                    consecutive_duty_days=consecutive,
                ))

                # Trips run a few days then reset on a rest period.
                if consecutive >= self.rng.randint(4, 7):
                    consecutive = 0
                    sleep_debt = _clamp(sleep_debt - 3.0, 0, 12)
                    state = _clamp(state + 0.15, 0, 1)
                else:
                    consecutive += 1

                day += timedelta(days=1)

        return readings
