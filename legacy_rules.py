"""
Legacy rules-based evaluation engine for the Patchamomma driver safety dashboard.

This module provides a deterministic fallback mechanism for evaluating driver readiness
when the AI (Gemini) API is unavailable.
"""

from datetime import datetime
from typing import Dict, Any

# Assuming ReadinessEvaluation is defined in agent_evaluator or a shared models file.
# We will import it from agent_evaluator to avoid circular dependencies if we define models there,
# or we can redefine the type here if needed. For now, let's import it.
from agent_evaluator import ReadinessEvaluation

def evaluate_with_rules(driver_data: Dict[str, Any]) -> ReadinessEvaluation:
    """Fallback rules-based evaluation using a weighted scoring algorithm."""
    
    # Extract data with defaults
    total_sleep = float(driver_data.get('total_sleep_hours', 0))
    deep_sleep_pct = float(driver_data.get('deep_sleep_pct', 0))
    rem_sleep_pct = float(driver_data.get('rem_sleep_pct', 0))
    hrv = float(driver_data.get('hrv_ms', 0))
    time_awake = float(driver_data.get('time_awake_since_last_sleep', 0))
    report_time_str = driver_data.get('report_time', '12:00')

    score_components = []
    risk_factors = []

    resting_hr = int(driver_data.get('resting_hr', 65))
    consecutive_days = int(driver_data.get('consecutive_duty_days', 1))

    # Total sleep: 25% weight
    if total_sleep > 7:
        sleep_score = 100
    elif total_sleep >= 6:
        sleep_score = 80
    elif total_sleep >= 5:
        sleep_score = 55
        risk_factors.append("Suboptimal sleep duration (5-6h)")
    elif total_sleep >= 4:
        sleep_score = 20
        risk_factors.append("Insufficient sleep duration (4-5h)")
    else:
        sleep_score = 0
        risk_factors.append("Severe sleep deprivation (<4h)")
    score_components.append(sleep_score * 0.25)

    # Deep sleep %: 15% weight
    if deep_sleep_pct > 20:
        deep_score = 100
    elif deep_sleep_pct >= 13:
        deep_score = 75
    elif deep_sleep_pct >= 10:
        deep_score = 50
        risk_factors.append("Low deep sleep % (10-13%)")
    else:
        deep_score = 20
        risk_factors.append("Critically low deep sleep (<10%)")
    score_components.append(deep_score * 0.15)

    # REM sleep %: 10% weight
    if rem_sleep_pct > 20:
        rem_score = 100
    elif rem_sleep_pct >= 15:
        rem_score = 75
    elif rem_sleep_pct >= 10:
        rem_score = 50
        risk_factors.append("Low REM sleep % (10-15%)")
    else:
        rem_score = 20
        risk_factors.append("Critically low REM sleep (<10%)")
    score_components.append(rem_score * 0.10)

    # HRV: 20% weight
    if hrv > 65:
        hrv_score = 100
    elif hrv >= 50:
        hrv_score = 85
    elif hrv >= 35:
        hrv_score = 60
    elif hrv >= 25:
        hrv_score = 30
        risk_factors.append("Low HRV (25-35ms) indicating poor recovery")
    else:
        hrv_score = 0
        risk_factors.append("Very low HRV (<25ms) indicating severe stress or lack of recovery")
    score_components.append(hrv_score * 0.20)

    # Time awake: 15% weight
    if time_awake < 5:
        awake_score = 100
    elif time_awake <= 12:
        awake_score = 85
    elif time_awake <= 17:
        awake_score = 60
    elif time_awake <= 19:
        awake_score = 20
        risk_factors.append(f"Extended time awake ({time_awake}h)")
    else:
        awake_score = 0
        risk_factors.append(f"Dangerous time awake ({time_awake}h)")
    score_components.append(awake_score * 0.15)

    # Circadian alignment: 10% weight
    try:
        if 'T' in report_time_str:
            report_hour = datetime.fromisoformat(report_time_str).hour
        else:
            report_hour = int(report_time_str.split(':')[0])
    except (ValueError, IndexError):
        report_hour = 12

    circadian_note = "Report time is outside the circadian low window."
    if 2 <= report_hour < 5:
        circadian_score = 20
        risk_factors.append("Reporting during Window of Circadian Low (02:00-05:00)")
        circadian_note = "Report time falls directly within the 02:00-05:00 Window of Circadian Low (WOCL)."
    elif report_hour == 1 or report_hour == 5:
        circadian_score = 50
        risk_factors.append("Reporting adjacent to Window of Circadian Low")
        circadian_note = "Report time is within 1 hour of the circadian low window."
    else:
        circadian_score = 100
    score_components.append(circadian_score * 0.10)

    # Consecutive duty days: 5% weight
    if consecutive_days >= 5:
        duty_score = 20
        risk_factors.append(f"Extended consecutive duty ({consecutive_days} days)")
    elif consecutive_days >= 3:
        duty_score = 60
    else:
        duty_score = 100
    score_components.append(duty_score * 0.05)

    # Resting heart rate stress indicator: 5% weight
    if resting_hr > 80:
        hr_score = 20
        risk_factors.append(f"Elevated resting HR ({resting_hr} bpm)")
    elif resting_hr > 72:
        hr_score = 60
    else:
        hr_score = 100
    score_components.append(hr_score * 0.05)

    # Final score
    # Component weights above (25+15+10+20+15+10+5+5) sum to 1.05, not 1.0,
    # so normalize by the actual total weight to keep the score in 0-100.
    total_weight = 0.25 + 0.15 + 0.10 + 0.20 + 0.15 + 0.10 + 0.05 + 0.05
    final_score = int(round(sum(score_components) / total_weight))

    # Determine status and recommendations
    if final_score >= 70:
        status = 'CLEAR'
        recommended_action = "Proceed with scheduled duties."
    elif final_score >= 45:
        status = 'PENDING_TEST'
        recommended_action = "Administer PVT (Psychomotor Vigilance Test) before duty."
    else:
        status = 'GROUNDED'
        recommended_action = "Remove from duty. Minimum 8 hours continuous rest required."

    reasoning = (
        f"Driver scored {final_score}/100. "
        + ("No major risk factors detected." if not risk_factors else f"Identified risks: {', '.join(risk_factors)}.")
    )

    return ReadinessEvaluation(
        status=status,
        score=final_score,
        reasoning=reasoning,
        recommended_action=recommended_action,
        risk_factors=risk_factors,
        circadian_note=circadian_note
    )
