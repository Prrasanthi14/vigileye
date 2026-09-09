"""VigilEye readiness API."""

import logging

from fastapi import APIRouter, FastAPI, HTTPException

from ..config import settings
from ..data import get_connector
from ..models import Evaluation
from ..scoring.rules import score_pilot, status_for
from ..scoring.service import evaluate_readiness, simulate_pvt_test

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="VigilEye Readiness API", version="1.0.0")
router = APIRouter(prefix="/api/v1")


@app.get("/health")
def health() -> dict:
    connector = get_connector()
    return {
        "status": "ok",
        "data_source": connector.get_source_name(),
        "data_connected": connector.is_connected(),
        "agent_model": settings.gemini_model,
        "agent_key_configured": bool(settings.gemini_api_key),
    }


@router.get("/fleet")
def fleet() -> list[dict]:
    """Latest reading per pilot, screened by the same rules engine as the detail view."""
    df = get_connector().get_fleet_summary()
    rows = []
    for record in df.to_dict("records"):
        score, risks, _ = score_pilot(record)
        status, _ = status_for(score)
        rows.append({**record, "score": score, "status": status, "risk_count": len(risks)})
    return rows


@router.get("/pilots/{driver_id}")
def pilot(driver_id: str) -> dict:
    connector = get_connector()
    data = connector.fetch_latest_data(driver_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"No data for pilot {driver_id}")

    history = connector.get_pilot_history(driver_id, days=30)
    recent = history.tail(7)
    if not recent.empty:
        data["seven_day_avg_sleep"] = round(float(recent["total_sleep_hours"].mean()), 1)
        data["seven_day_avg_hrv"] = round(float(recent["hrv_ms"].mean()), 1)

    return {"snapshot": data, "history": history.to_dict("records")}


@router.post("/pilots/{driver_id}/evaluate", response_model=Evaluation)
def evaluate(driver_id: str) -> Evaluation:
    return evaluate_readiness(pilot(driver_id)["snapshot"])


@router.post("/pilots/{driver_id}/pvt")
def pvt(driver_id: str) -> dict:
    return simulate_pvt_test(evaluate(driver_id).score)


app.include_router(router)
