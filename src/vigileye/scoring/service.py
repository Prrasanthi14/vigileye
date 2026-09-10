"""Readiness evaluation entry point: agent first, deterministic rules as fallback."""

import logging
import random
from typing import Any

from ..models import Evaluation
from . import cache
from .agent import AgentUnavailable, evaluate_with_agent
from .rules import evaluate_with_rules

logger = logging.getLogger(__name__)


def evaluate_readiness(data: dict[str, Any], use_cache: bool = True) -> Evaluation:
    """Evaluate one pilot, recording which engine produced the verdict.

    A fallback is never presented as agent output: `source` and `fallback_reason`
    travel with the result so the dashboard can label it honestly.

    An agent verdict for a given pilot-day is stored and reused, so the fleet
    grid and the pilot page report the same decision. Rules fallbacks are not
    cached — they are a degraded result that should be retried.
    """
    driver_id = data.get("driver_id")
    reading_date = data.get("last_sync_timestamp")

    if use_cache and driver_id and reading_date:
        cached = cache.get_verdict(driver_id, reading_date)
        if cached:
            return cached

    try:
        result = evaluate_with_agent(data)
        evaluation = Evaluation(**result.model_dump(), source="agent")
        if driver_id and reading_date:
            cache.put_verdict(driver_id, reading_date, evaluation)
        return evaluation
    except AgentUnavailable as exc:
        logger.warning("Agent unavailable (%s); using deterministic rules.", exc)
        result = evaluate_with_rules(data)
        return Evaluation(**result.model_dump(), source="rules", fallback_reason=str(exc))


def simulate_pvt_test(readiness_score: int) -> dict[str, Any]:
    """Simulate a Psychomotor Vigilance Test outcome for a given readiness score."""
    if readiness_score >= 80:
        mean_rt, lapses = random.uniform(220, 280), random.randint(0, 1)
    elif readiness_score >= 60:
        mean_rt, lapses = random.uniform(260, 320), random.randint(0, 2)
    elif readiness_score >= 40:
        mean_rt, lapses = random.uniform(320, 420), random.randint(2, 5)
    else:
        mean_rt, lapses = random.uniform(450, 600), random.randint(5, 10)

    passed = mean_rt < 350 and lapses < 3
    return {
        "mean_reaction_ms": round(mean_rt, 2),
        "lapses": lapses,
        "fastest_ms": round(max(150.0, mean_rt - random.uniform(20, 50)), 2),
        "slowest_ms": round(mean_rt + random.uniform(50, 150), 2),
        "result": "PASS" if passed else "FAIL",
        "recommendation": (
            "Cognitive function is adequate for duty." if passed
            else "Cognitive impairment detected. Unfit for duty."
        ),
    }
