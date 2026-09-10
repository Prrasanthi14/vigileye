"""VigilEye readiness API."""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from fastapi import APIRouter, FastAPI, HTTPException, Query

from ..config import settings
from ..data import get_connector
from ..models import Evaluation, PilotCreate, PilotUpdate
from ..scoring import cache, usage
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

    now = datetime.now(timezone.utc)
    rows = []
    for record in df.to_dict("records"):
        verdict = verdicts.get(record["driver_id"])
        if verdict:
            stamped = verdict.get("evaluated_at")
            age = (now - stamped).total_seconds() / 60 if stamped else None
            rows.append({
                **record,
                "score": verdict["score"],
                "status": verdict["status"],
                "source": verdict["source"],
                "evaluated_at": stamped.isoformat() if stamped else None,
                "age_minutes": round(age) if age is not None else None,
                "stale": age is not None and age > settings.verdict_max_age_minutes,
            })
        else:
            score, _, _ = score_pilot(record)
            status, _ = status_for(score)
            rows.append({**record, "score": score, "status": status, "source": "rules",
                         "evaluated_at": None, "age_minutes": None, "stale": True})
    return rows


@router.get("/usage")
def token_usage() -> dict:
    """Gemini tokens spent today (UTC), what stored verdicts saved, and budget left."""
    return usage.summary()


@router.post("/fleet/evaluate")
def evaluate_fleet(limit: int = 100, workers: int = 6) -> dict:
    """Run the agent across the fleet and store each verdict.

    Operator-triggered — nothing calls this on a timer. It costs one model call
    per pilot whose verdict is missing, stale, or about superseded readings;
    pilots already judged on their current data are skipped.

    Calls run concurrently because a single Pro evaluation takes ~20s. At fleet
    sizes beyond a few hundred pilots this whole-fleet sweep stops being the
    right shape: evaluating each pilot shortly before their report time spreads
    the same work across the day and keeps every verdict fresh at the moment it
    is used.
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
        stamped = existing.get("evaluated_at") if existing else None
        fresh = stamped is not None and (
            (datetime.now(timezone.utc) - stamped).total_seconds() / 60
            <= settings.verdict_max_age_minutes
        )
        if existing and fresh and existing["reading_date"] == snapshot.get("last_sync_timestamp"):
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


def _pilot_payload(driver_id: str, days: int = 30) -> dict:
    """Snapshot + history. A plain function, so other routes can reuse it —
    calling a route directly would pass FastAPI's Query object as `days`.
    """
    connector = get_connector()
    data = connector.fetch_latest_data(driver_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"No data for pilot {driver_id}")

    history = connector.get_pilot_history(driver_id, days=days)
    recent = history.tail(7)
    if not recent.empty:
        data["seven_day_avg_sleep"] = round(float(recent["total_sleep_hours"].mean()), 1)
        data["seven_day_avg_hrv"] = round(float(recent["hrv_ms"].mean()), 1)

    return {"snapshot": data, "history": history.to_dict("records")}


@router.get("/pilots/{driver_id}")
def pilot(driver_id: str, days: int = Query(default=30, ge=1, le=365)) -> dict:
    """Latest snapshot plus `days` of history (e.g. ?days=7 for one week)."""
    return _pilot_payload(driver_id, days)


@router.post("/pilots", status_code=201)
def create_pilot(pilot: PilotCreate) -> dict:
    connector = get_connector()
    if connector.pilot_exists(pilot.driver_id):
        raise HTTPException(status_code=409, detail=f"Pilot {pilot.driver_id} already exists")
    connector.create_pilot(pilot.model_dump())
    return {"created": pilot.driver_id}


@router.patch("/pilots/{driver_id}")
def update_pilot(driver_id: str, update: PilotUpdate) -> dict:
    connector = get_connector()
    if not connector.pilot_exists(driver_id):
        raise HTTPException(status_code=404, detail=f"Pilot {driver_id} not found")

    fields = update.model_dump(exclude_unset=True, exclude_none=True)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields supplied to update")

    connector.update_pilot(driver_id, fields)
    # Medical history and shift type feed the verdict, so it is no longer valid.
    cache.invalidate(driver_id)
    return {"updated": driver_id, "fields": sorted(fields), "verdicts_invalidated": True}


@router.delete("/pilots/{driver_id}")
def delete_pilot(driver_id: str) -> dict:
    connector = get_connector()
    if not connector.pilot_exists(driver_id):
        raise HTTPException(status_code=404, detail=f"Pilot {driver_id} not found")
    connector.delete_pilot(driver_id)
    cache.invalidate(driver_id)
    return {"deleted": driver_id}


# NOTE: there is deliberately no endpoint to write or edit biometric readings.
# Readings are evidence for a go/no-go call, so a human-facing write path would
# let an official clear a grounded pilot by editing the evidence. Readings enter
# only through the ingestion pipeline (vigileye.ingestion), which runs as a job
# under GCP credentials and records the source of every row.


@router.post("/pilots/{driver_id}/evaluate", response_model=Evaluation)
def evaluate(driver_id: str, refresh: bool = Query(default=False)) -> Evaluation:
    """Fetch this pilot's current readings and return a go/no-go verdict.

    Always reads the latest row the tracker has landed. A verdict already held
    for that same reading is reused; `?refresh=true` forces a fresh agent run.
    """
    return evaluate_readiness(_pilot_payload(driver_id)["snapshot"], use_cache=not refresh)


@router.post("/pilots/{driver_id}/pvt")
def pvt(driver_id: str) -> dict:
    return simulate_pvt_test(evaluate(driver_id, refresh=False).score)


app.include_router(router)
