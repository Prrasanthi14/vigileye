"""
Agentic evaluation engine for the Patchamomma driver safety dashboard.

This module evaluates a driver's physiological and schedule data to determine their 
readiness for duty. It employs a dual-mode approach: 
1. An AI-based evaluation using Gemini (when available).
2. A deterministic rules-based evaluation as a fallback mechanism.
"""

import os
import json
import random
import logging
from datetime import datetime
from typing import Literal, Dict, Any, List

from dotenv import load_dotenv
from pydantic import BaseModel, Field

try:
    from google import genai
except ImportError:
    genai = None

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")


class ReadinessEvaluation(BaseModel):
    status: Literal['CLEAR', 'PENDING_TEST', 'GROUNDED']
    score: int = Field(ge=0, le=100)
    reasoning: str
    recommended_action: str
    risk_factors: List[str]
    circadian_note: str


def _evaluate_with_gemini(driver_data: Dict[str, Any]) -> ReadinessEvaluation:
    """Uses Gemini to evaluate driver readiness based on biometric and schedule data."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY not found in environment variables.")
    if genai is None:
        raise ImportError("google.genai package is not installed.")

    client = genai.Client(api_key=api_key)
    
    system_prompt = """
    You are an expert fatigue risk management system analyst for the aviation and commercial driving sectors (Patchamomma).
    You evaluate driver readiness based on FAA fatigue risk management contexts and FMCSA hours-of-service rules.
    
    Key Scientific Principles:
    - Circadian Rhythm: The window between 02:00 and 05:00 is the Window of Circadian Low (WOCL), highly prone to fatigue.
    - Sleep Architecture: Healthy deep sleep should be >13% and REM sleep >15%.
    - HRV (Heart Rate Variability): Higher HRV indicates better recovery; low HRV (<35ms) suggests poor recovery or stress.
    - Wakefulness: Time awake beyond 16-17 hours severely degrades performance, akin to alcohol intoxication.
    - Cumulative Fatigue (Enterprise Feature): You will receive a '7_day_avg_sleep' metric. If this is <6.0 hours, the pilot is suffering from cumulative chronic sleep restriction, which dramatically amplifies the risk of any acute (single-night) sleep loss. Pay close attention to this.
    - Pre-existing Medical Conditions: You will receive a 'medical_history' field. If the pilot has conditions like Sleep Apnea, Insomnia, or Narcolepsy, you MUST factor this into your risk assessment. For example, mild sleep apnea amplifies the danger of low deep sleep. If a condition is severe and metrics are poor, recommend grounding immediately.
    
    You will receive a JSON payload containing driver shift and biometric data.
    Carefully analyze this data and return a JSON object with the exact schema requested.
    """

    # default=str so BigQuery DATE values (datetime.date) serialize instead of raising.
    user_prompt = f"Analyze the following driver data and provide a readiness evaluation:\n{json.dumps(driver_data, indent=2, default=str)}"

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=user_prompt,
        config={
            'system_instruction': system_prompt,
            'response_mime_type': 'application/json',
            'response_schema': ReadinessEvaluation,
        }
    )
    
    return ReadinessEvaluation.model_validate_json(response.text)


from legacy_rules import evaluate_with_rules as _evaluate_with_rules
def evaluate_readiness(driver_data: Dict[str, Any]) -> ReadinessEvaluation:
    """
    Evaluates driver readiness.
    Attempts Gemini-based evaluation first; falls back to rules-based evaluation on failure.
    """
    logger.info("Starting readiness evaluation.")
    try:
        logger.info("Attempting Gemini API evaluation.")
        result = _evaluate_with_gemini(driver_data)
        logger.info("Gemini evaluation successful.")
        return result
    except Exception as e:
        logger.warning(f"Gemini evaluation failed ({e}). Falling back to rules-based evaluation.")
        result = _evaluate_with_rules(driver_data)
        logger.info("Rules-based evaluation completed.")
        return result


def simulate_pvt_test(readiness_score: int) -> Dict[str, Any]:
    """
    Simulates a Psychomotor Vigilance Test (PVT) based on readiness score.
    Returns simulated reaction times and pass/fail result.
    """
    if readiness_score >= 80:
        mean_rt = random.uniform(220, 280)
        lapses = random.randint(0, 1)
    elif readiness_score >= 60:
        mean_rt = random.uniform(260, 320)
        lapses = random.randint(0, 2)
    elif readiness_score >= 40:
        mean_rt = random.uniform(320, 420)
        lapses = random.randint(2, 5)
    else:
        mean_rt = random.uniform(450, 600)
        lapses = random.randint(5, 10)

    # Generate plausible min/max around the mean
    fastest_ms = max(150.0, mean_rt - random.uniform(20, 50))
    slowest_ms = mean_rt + random.uniform(50, 150)

    # PVT criteria: PASS if mean < 350ms and lapses < 3
    if mean_rt < 350 and lapses < 3:
        result = 'PASS'
        recommendation = "Cognitive function is adequate for duty."
    else:
        result = 'FAIL'
        recommendation = "Cognitive impairment detected. Unfit for duty."

    return {
        "mean_reaction_ms": round(mean_rt, 2),
        "lapses": lapses,
        "fastest_ms": round(fastest_ms, 2),
        "slowest_ms": round(slowest_ms, 2),
        "result": result,
        "recommendation": recommendation
    }
