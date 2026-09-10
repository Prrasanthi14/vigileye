"""Persisted agent verdicts.

A readiness verdict is a fact about one pilot on one day's biometrics, so it is
worth computing once and storing. That keeps the fleet grid and the pilot page
showing the same agent decision instead of the grid falling back to a cheaper
engine, and caps cost at one model call per pilot per day rather than one per
page view.
"""

import json
import logging
from datetime import date
from typing import Any

from ..config import settings
from ..models import Evaluation

logger = logging.getLogger(__name__)

TABLE = "readiness_verdicts"

SCHEMA = [
    ("driver_id", "STRING"), ("reading_date", "DATE"), ("status", "STRING"),
    ("score", "INTEGER"), ("source", "STRING"), ("reasoning", "STRING"),
    ("recommended_action", "STRING"), ("risk_factors", "STRING"),
    ("circadian_note", "STRING"), ("evaluated_at", "TIMESTAMP"),
]


def _client():
    from google.cloud import bigquery

    return bigquery.Client(project=settings.gcp_project_id)


def _table_id() -> str:
    return f"{settings.gcp_project_id}.{settings.bigquery_dataset}.{TABLE}"


def ensure_table() -> None:
    from google.cloud import bigquery

    client = _client()
    table = bigquery.Table(
        _table_id(), schema=[bigquery.SchemaField(n, t) for n, t in SCHEMA]
    )
    client.create_table(table, exists_ok=True)


def get_verdict(driver_id: str, reading_date: date) -> Evaluation | None:
    """Return a stored verdict for this pilot-day, if one exists."""
    from google.cloud import bigquery

    query = f"""
        SELECT status, score, source, reasoning, recommended_action,
               risk_factors, circadian_note, evaluated_at
        FROM `{_table_id()}`
        WHERE driver_id = @driver_id AND reading_date = @reading_date
        ORDER BY evaluated_at DESC LIMIT 1
    """
    job_config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("driver_id", "STRING", driver_id),
        bigquery.ScalarQueryParameter("reading_date", "DATE", reading_date),
    ])
    try:
        for row in _client().query(query, job_config=job_config).result():
            return Evaluation(
                status=row.status, score=row.score, source=row.source,
                reasoning=row.reasoning, recommended_action=row.recommended_action,
                risk_factors=json.loads(row.risk_factors or "[]"),
                circadian_note=row.circadian_note or "",
                evaluated_at=row.evaluated_at,
            )
    except Exception as exc:
        logger.warning("Verdict cache read failed: %s", exc)
    return None


def put_verdict(driver_id: str, reading_date: date, evaluation: Evaluation) -> None:
    """Store a verdict, replacing any existing one for the same pilot-day."""
    from google.cloud import bigquery

    query = f"""
        MERGE `{_table_id()}` T
        USING (SELECT @driver_id AS driver_id, @reading_date AS reading_date) S
        ON T.driver_id = S.driver_id AND T.reading_date = S.reading_date
        WHEN MATCHED THEN UPDATE SET
            status=@status, score=@score, source=@source, reasoning=@reasoning,
            recommended_action=@action, risk_factors=@risks,
            circadian_note=@note, evaluated_at=CURRENT_TIMESTAMP()
        WHEN NOT MATCHED THEN INSERT
            (driver_id, reading_date, status, score, source, reasoning,
             recommended_action, risk_factors, circadian_note, evaluated_at)
            VALUES (@driver_id, @reading_date, @status, @score, @source, @reasoning,
                    @action, @risks, @note, CURRENT_TIMESTAMP())
    """
    params: list[Any] = [
        bigquery.ScalarQueryParameter("driver_id", "STRING", driver_id),
        bigquery.ScalarQueryParameter("reading_date", "DATE", reading_date),
        bigquery.ScalarQueryParameter("status", "STRING", evaluation.status),
        bigquery.ScalarQueryParameter("score", "INT64", evaluation.score),
        bigquery.ScalarQueryParameter("source", "STRING", evaluation.source),
        bigquery.ScalarQueryParameter("reasoning", "STRING", evaluation.reasoning),
        bigquery.ScalarQueryParameter("action", "STRING", evaluation.recommended_action),
        bigquery.ScalarQueryParameter("risks", "STRING", json.dumps(evaluation.risk_factors)),
        bigquery.ScalarQueryParameter("note", "STRING", evaluation.circadian_note),
    ]
    try:
        _client().query(query, job_config=bigquery.QueryJobConfig(query_parameters=params)).result()
    except Exception as exc:
        logger.warning("Verdict cache write failed: %s", exc)


def invalidate(driver_id: str, reading_date: date | None = None) -> None:
    """Drop stored verdicts for a pilot.

    Called whenever a pilot's biometrics or roster change: the stored verdict
    describes data that no longer exists, and serving it would show a go/no-go
    call that was never made about the current readings.
    """
    from google.cloud import bigquery

    query = f"DELETE FROM `{_table_id()}` WHERE driver_id = @driver_id"
    params = [bigquery.ScalarQueryParameter("driver_id", "STRING", driver_id)]
    if reading_date:
        query += " AND reading_date = @reading_date"
        params.append(bigquery.ScalarQueryParameter("reading_date", "DATE", reading_date))

    try:
        _client().query(query, job_config=bigquery.QueryJobConfig(query_parameters=params)).result()
    except Exception as exc:
        logger.warning("Verdict invalidation failed: %s", exc)


def get_fleet_verdicts() -> dict[str, dict]:
    """Latest stored verdict per pilot, keyed by driver_id."""
    query = f"""
        SELECT driver_id, status, score, source, reading_date, evaluated_at
        FROM `{_table_id()}`
        QUALIFY ROW_NUMBER() OVER (PARTITION BY driver_id
                                   ORDER BY reading_date DESC, evaluated_at DESC) = 1
    """
    try:
        return {
            row.driver_id: {
                "status": row.status, "score": row.score,
                "source": row.source, "reading_date": row.reading_date,
                "evaluated_at": row.evaluated_at,
            }
            for row in _client().query(query).result()
        }
    except Exception as exc:
        logger.warning("Fleet verdict read failed: %s", exc)
        return {}
