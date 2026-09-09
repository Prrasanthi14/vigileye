"""Deterministic readiness scoring.

Used both as the fallback when the agent is unavailable and as the fleet-wide
screen, so a pilot's grid status and detail verdict can never disagree.
"""

from dataclasses import dataclass
from datetime import datetime
from operator import ge, gt, le, lt
from typing import Any, Callable

from ..models import ReadinessEvaluation

Band = tuple[Callable[[Any, Any], bool], float, int, str | None]


@dataclass(frozen=True)
class Factor:
    key: str
    weight: float
    default: float
    bands: tuple[Band, ...]
    floor: tuple[int, str | None]

    def evaluate(self, data: dict[str, Any]) -> tuple[float, str | None]:
        value = float(data.get(self.key, self.default) or self.default)
        for op, threshold, score, risk in self.bands:
            if op(value, threshold):
                return score, risk and risk.format(value=value)
        score, risk = self.floor
        return score, risk and risk.format(value=value)


FACTORS = (
    Factor("total_sleep_hours", 0.25, 0, (
        (gt, 7, 100, None),
        (ge, 6, 80, None),
        (ge, 5, 55, "Suboptimal sleep duration (5-6h)"),
        (ge, 4, 20, "Insufficient sleep duration (4-5h)"),
    ), (0, "Severe sleep deprivation (<4h)")),
    Factor("deep_sleep_pct", 0.15, 0, (
        (gt, 20, 100, None),
        (ge, 13, 75, None),
        (ge, 10, 50, "Low deep sleep % (10-13%)"),
    ), (20, "Critically low deep sleep (<10%)")),
    Factor("rem_sleep_pct", 0.10, 0, (
        (gt, 20, 100, None),
        (ge, 15, 75, None),
        (ge, 10, 50, "Low REM sleep % (10-15%)"),
    ), (20, "Critically low REM sleep (<10%)")),
    Factor("hrv_ms", 0.20, 0, (
        (gt, 65, 100, None),
        (ge, 50, 85, None),
        (ge, 35, 60, None),
        (ge, 25, 30, "Low HRV (25-35ms) indicating poor recovery"),
    ), (0, "Very low HRV (<25ms) indicating severe stress or lack of recovery")),
    Factor("time_awake_since_last_sleep", 0.15, 0, (
        (lt, 5, 100, None),
        (le, 12, 85, None),
        (le, 17, 60, None),
        (le, 19, 20, "Extended time awake ({value}h)"),
    ), (0, "Dangerous time awake ({value}h)")),
    Factor("consecutive_duty_days", 0.05, 1, (
        (ge, 5, 20, "Extended consecutive duty ({value:.0f} days)"),
        (ge, 3, 60, None),
    ), (100, None)),
    Factor("resting_hr", 0.05, 65, (
        (gt, 80, 20, "Elevated resting HR ({value:.0f} bpm)"),
        (gt, 72, 60, None),
    ), (100, None)),
)

CIRCADIAN_WEIGHT = 0.10
TOTAL_WEIGHT = sum(f.weight for f in FACTORS) + CIRCADIAN_WEIGHT


def _circadian(report_time: str) -> tuple[int, str | None, str]:
    """Score the report time against the Window of Circadian Low (02:00-05:00)."""
    try:
        if "T" in report_time:
            hour = datetime.fromisoformat(report_time).hour
        else:
            hour = int(report_time.split(":")[0])
    except (ValueError, IndexError, AttributeError):
        hour = 12

    if 2 <= hour < 5:
        return (20, "Reporting during Window of Circadian Low (02:00-05:00)",
                "Report time falls directly within the 02:00-05:00 Window of Circadian Low (WOCL).")
    if hour in (1, 5):
        return (50, "Reporting adjacent to Window of Circadian Low",
                "Report time is within 1 hour of the circadian low window.")
    return (100, None, "Report time is outside the circadian low window.")


def score_pilot(data: dict[str, Any]) -> tuple[int, list[str], str]:
    """Return (0-100 score, risk factors, circadian note)."""
    weighted = 0.0
    risks: list[str] = []

    for factor in FACTORS:
        score, risk = factor.evaluate(data)
        weighted += score * factor.weight
        if risk:
            risks.append(risk)

    circadian_score, circadian_risk, note = _circadian(data.get("report_time") or "12:00")
    weighted += circadian_score * CIRCADIAN_WEIGHT
    if circadian_risk:
        risks.append(circadian_risk)

    # Weights total 1.05, not 1.0 — normalize so the score stays within 0-100.
    return int(round(weighted / TOTAL_WEIGHT)), risks, note


def status_for(score: int) -> tuple[str, str]:
    if score >= 70:
        return "CLEAR", "Proceed with scheduled duties."
    if score >= 45:
        return "PENDING_TEST", "Administer PVT (Psychomotor Vigilance Test) before duty."
    return "GROUNDED", "Remove from duty. Minimum 8 hours continuous rest required."


def evaluate_with_rules(data: dict[str, Any]) -> ReadinessEvaluation:
    score, risks, circadian_note = score_pilot(data)
    status, recommended_action = status_for(score)
    reasoning = f"Driver scored {score}/100. " + (
        "No major risk factors detected." if not risks
        else f"Identified risks: {', '.join(risks)}."
    )
    return ReadinessEvaluation(
        status=status,
        score=score,
        reasoning=reasoning,
        recommended_action=recommended_action,
        risk_factors=risks,
        circadian_note=circadian_note,
    )
