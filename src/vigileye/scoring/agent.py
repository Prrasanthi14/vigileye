"""Gemini-backed readiness agent."""

import json
import logging
import os
import threading
import time
from collections import deque
from typing import Any

from ..config import settings
from ..models import ReadinessEvaluation

logger = logging.getLogger(__name__)

# Gemini enforces a per-model requests-per-minute quota (25 for Pro). Fleet-wide
# evaluation runs concurrently, so pace calls here rather than letting parallel
# workers trip the quota and silently degrade every pilot to the rules engine.
RPM_LIMIT = int(os.getenv("GEMINI_RPM", "20"))
MAX_RETRIES = 3

_lock = threading.Lock()
_recent_calls: deque[float] = deque()


def _throttle() -> None:
    while True:
        with _lock:
            now = time.monotonic()
            while _recent_calls and now - _recent_calls[0] > 60:
                _recent_calls.popleft()
            if len(_recent_calls) < RPM_LIMIT:
                _recent_calls.append(now)
                return
            wait = 60 - (now - _recent_calls[0]) + 0.05
        time.sleep(wait)

SYSTEM_PROMPT = """
You are an expert fatigue risk management system analyst for the aviation sector (VigilEye).
You evaluate pilot readiness against FAA fatigue risk management contexts and FMCSA hours-of-service rules.

Key scientific principles:
- Circadian rhythm: 02:00-05:00 is the Window of Circadian Low (WOCL), highly prone to fatigue.
- Sleep architecture: healthy deep sleep is >13% and REM sleep >15%.
- HRV: higher indicates better recovery; low HRV (<35ms) suggests poor recovery or stress.
- Wakefulness: beyond 16-17 hours awake, performance degrades comparably to alcohol intoxication.
- Cumulative fatigue: a 'seven_day_avg_sleep' below 6.0 hours indicates chronic sleep restriction,
  which dramatically amplifies the risk of any acute single-night sleep loss.
- Medical history: conditions such as Sleep Apnea, Insomnia or Narcolepsy MUST raise the assessed
  risk. Mild sleep apnea amplifies the danger of low deep sleep; if a condition is severe and the
  metrics are poor, ground the pilot.

You receive a JSON payload of pilot schedule and biometric data. Analyse it and return JSON
matching the requested schema exactly.
"""


class AgentUnavailable(RuntimeError):
    """The agent could not produce a verdict; caller should fall back."""


def evaluate_with_agent(data: dict[str, Any]) -> ReadinessEvaluation:
    if not settings.gemini_api_key:
        raise AgentUnavailable("GEMINI_API_KEY is not set")

    try:
        from google import genai
    except ImportError as exc:
        raise AgentUnavailable("google-genai is not installed") from exc

    client = genai.Client(api_key=settings.gemini_api_key)
    # default=str so BigQuery DATE values serialize instead of raising.
    payload = json.dumps(data, indent=2, default=str)

    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        _throttle()
        try:
            response = client.models.generate_content(
                model=settings.gemini_model,
                contents=f"Analyze the following pilot data and provide a readiness evaluation:\n{payload}",
                config={
                    "system_instruction": SYSTEM_PROMPT,
                    "response_mime_type": "application/json",
                    "response_schema": ReadinessEvaluation,
                },
            )
            return ReadinessEvaluation.model_validate_json(response.text)
        except Exception as exc:
            last_error = exc
            if "RESOURCE_EXHAUSTED" not in str(exc) or attempt == MAX_RETRIES - 1:
                break
            backoff = 20 * (attempt + 1)
            logger.warning("Rate limited; retrying in %ss (attempt %d).", backoff, attempt + 1)
            time.sleep(backoff)

    raise AgentUnavailable(str(last_error)) from last_error
