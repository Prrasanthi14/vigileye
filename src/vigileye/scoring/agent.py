"""Gemini-backed readiness agent."""

import json
import logging
from typing import Any

from ..config import settings
from ..models import ReadinessEvaluation

logger = logging.getLogger(__name__)

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
        raise AgentUnavailable(str(exc)) from exc
