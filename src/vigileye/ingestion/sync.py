"""Load readings from any WearableProvider into BigQuery."""

import logging
from datetime import date

from ..config import settings
from ..scoring import cache
from .base import DailyReading, WearableProvider

logger = logging.getLogger(__name__)

READINGS_SCHEMA = [
    ("driver_id", "STRING"), ("date", "DATE"), ("report_time", "STRING"),
    ("total_sleep_hours", "FLOAT"), ("deep_sleep_pct", "FLOAT"), ("rem_sleep_pct", "FLOAT"),
    ("light_sleep_pct", "FLOAT"), ("awake_during_sleep_pct", "FLOAT"), ("hrv_ms", "FLOAT"),
    ("resting_hr", "INTEGER"), ("time_awake_since_last_sleep", "FLOAT"),
    ("consecutive_duty_days", "INTEGER"),
]


def sync_to_bigquery(
    provider: WearableProvider,
    driver_ids: list[str],
    start: date,
    end: date,
    table: str = "daily_readings",
    replace: bool = False,
) -> int:
    """Fetch from `provider` and write into BigQuery. Returns rows written."""
    from google.cloud import bigquery

    readings = provider.fetch_readings(driver_ids, start, end)
    if not readings:
        logger.warning("Provider %s returned no readings.", provider.name())
        return 0

    client = bigquery.Client(project=settings.gcp_project_id)
    table_id = f"{settings.gcp_project_id}.{settings.bigquery_dataset}.{table}"

    if not replace:
        # Upsert per pilot-day. A tracker re-syncing a corrected reading must
        # replace that day: two rows for one pilot-day would leave the verdict
        # depending on which row the "latest reading" query happened to pick.
        _delete_existing(client, table_id, readings)

    job_config = bigquery.LoadJobConfig(
        schema=[bigquery.SchemaField(n, t) for n, t in READINGS_SCHEMA],
        write_disposition="WRITE_TRUNCATE" if replace else "WRITE_APPEND",
    )
    rows = [_to_row(r) for r in readings]
    client.load_table_from_json(rows, table_id, job_config=job_config).result()

    # Any verdict about a pilot-day we just overwrote describes readings that no
    # longer exist. Drop it, so the next request re-runs the agent against the
    # data the tracker actually delivered.
    for driver_id in {r.driver_id for r in readings}:
        cache.invalidate(driver_id)

    logger.info("Wrote %d readings from %s to %s", len(rows), provider.name(), table_id)
    return len(rows)


def _delete_existing(client, table_id: str, readings: list[DailyReading]) -> None:
    """Remove any rows already stored for the pilots and dates about to be written."""
    from google.cloud import bigquery

    driver_ids = sorted({r.driver_id for r in readings})
    dates = [r.date for r in readings]

    query = f"""
        DELETE FROM `{table_id}`
        WHERE driver_id IN UNNEST(@driver_ids)
          AND date BETWEEN @start AND @end
    """
    job_config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ArrayQueryParameter("driver_ids", "STRING", driver_ids),
        bigquery.ScalarQueryParameter("start", "DATE", min(dates)),
        bigquery.ScalarQueryParameter("end", "DATE", max(dates)),
    ])
    client.query(query, job_config=job_config).result()


def _to_row(reading: DailyReading) -> dict:
    row = reading.model_dump()
    row["date"] = reading.date.isoformat()
    return row
