"""Load readings from any WearableProvider into BigQuery."""

import logging
from datetime import date

from ..config import settings
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

    job_config = bigquery.LoadJobConfig(
        schema=[bigquery.SchemaField(n, t) for n, t in READINGS_SCHEMA],
        write_disposition="WRITE_TRUNCATE" if replace else "WRITE_APPEND",
    )
    rows = [_to_row(r) for r in readings]
    client.load_table_from_json(rows, table_id, job_config=job_config).result()

    logger.info("Wrote %d readings from %s to %s", len(rows), provider.name(), table_id)
    return len(rows)


def _to_row(reading: DailyReading) -> dict:
    row = reading.model_dump()
    row["date"] = reading.date.isoformat()
    return row
