"""VigilEye readiness API."""

import logging
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, FastAPI, HTTPException

from ..config import settings
from ..data import get_connector
from ..models import Evaluation
from ..scoring import cache
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
    """Latest reading per pilot.

    Uses the stored agent verdict where one exists, so the grid shows the same
    decision as the pilot page; pilots not yet evaluated fall back to the rules
    screen and are marked as such.
    """
    df = get_connector().get_fleet_summary()
    verdicts = cache.get_fleet_verdicts()

    rows = []
    for record in df.to_dict("records"):
        verdict = verdicts.get(record["driver_id"])
        if verdict:
            rows.append({**record, "score": verdict["score"],
                         "status": verdict["status"], "source": verdict["source"]})
        else:
            score, _, _ = score_pilot(record)
            status, _ = status_for(score)
            rows.append({**record, "score": score, "status": status, "source": "rules"})
    return rows


@router.post("/fleet/evaluate")
def evaluate_fleet(limit: int = 100, workers: int = 6) -> dict:
    """Run the agent across the fleet and store each verdict.

    Intended for a scheduled run (once per biometric sync), not per page view:
    it costs one model call per pilot whose current reading has no verdict.
    Calls run concurrently because a single Pro evaluation takes ~20s, which
    would otherwise exceed the request deadline well before the fleet is done.
    """
    cache.ensure_table()
    connector = get_connector()
    verdicts = cache.get_fleet_verdicts()

    pending, already_current = [], 0
    for record in connector.get_fleet_summary().to_dict("records")[:limit]:
        snapshot = connector.fetch_latest_data(record["driver_id"])
        if not snapshot:
            continue
        existing = verdicts.get(record["driver_id"])
        if existing and existing["reading_date"] == snapshot.get("last_sync_timestamp"):
            already_current += 1
        else:
            pending.append(snapshot)

    evaluated, failed = 0, 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for result in pool.map(lambda s: evaluate_readiness(s, use_cache=False), pending):
            if result.source == "agent":
                evaluated += 1
            else:
                failed += 1

    return {"evaluated": evaluated, "already_current": already_current, "agent_failed": failed}


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
