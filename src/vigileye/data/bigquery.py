"""BigQuery-backed pilot data access."""

import logging
from typing import Any

import pandas as pd

from .base import HISTORY_COLUMNS, LATEST_COLUMNS, DataConnector

logger = logging.getLogger(__name__)


class BigQueryConnector(DataConnector):
    def __init__(self, project_id: str, dataset_id: str):
        from google.cloud import bigquery

        self.project_id = project_id
        self.dataset_id = dataset_id
        try:
            self.client = bigquery.Client(project=project_id)
        except Exception as exc:
            logger.error("Failed to initialize BigQuery client: %s", exc)
            self.client = None

    def _table(self, name: str) -> str:
        return f"`{self.project_id}.{self.dataset_id}.{name}`"

    def _run(self, query: str, **params: Any):
        from google.cloud import bigquery

        type_for = {str: "STRING", int: "INT64", float: "FLOAT64"}
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter(k, type_for[type(v)], v)
                for k, v in params.items()
            ]
        )
        return self.client.query(query, job_config=job_config)

    def fetch_latest_data(self, driver_id: str) -> dict[str, Any]:
        if not self.client:
            return {}
        query = f"""
            SELECT {LATEST_COLUMNS}
            FROM {self._table('pilots')} p
            JOIN {self._table('daily_readings')} d ON p.driver_id = d.driver_id
            WHERE p.driver_id = @driver_id
            ORDER BY d.date DESC LIMIT 1
        """
        for row in self._run(query, driver_id=driver_id).result():
            return dict(row)
        return {}

    def get_pilot_history(self, driver_id: str, days: int = 30) -> pd.DataFrame:
        if not self.client:
            return pd.DataFrame()
        query = f"""
            SELECT {HISTORY_COLUMNS}
            FROM {self._table('daily_readings')}
            WHERE driver_id = @driver_id
            ORDER BY date ASC
            LIMIT @days
        """
        return self._run(query, driver_id=driver_id, days=days).to_dataframe()

    def get_fleet_summary(self) -> pd.DataFrame:
        if not self.client:
            return pd.DataFrame()
        query = f"""
            WITH RankedReadings AS (
                SELECT p.driver_id, p.name, p.role, d.total_sleep_hours, d.hrv_ms,
                       d.report_time, d.consecutive_duty_days, d.deep_sleep_pct,
                       d.rem_sleep_pct, d.resting_hr, d.time_awake_since_last_sleep,
                       ROW_NUMBER() OVER(PARTITION BY p.driver_id ORDER BY d.date DESC) as rn
                FROM {self._table('pilots')} p
                JOIN {self._table('daily_readings')} d ON p.driver_id = d.driver_id
            )
            SELECT * EXCEPT(rn) FROM RankedReadings WHERE rn = 1
        """
        return self.client.query(query).to_dataframe()

    def get_source_name(self) -> str:
        return "Google BigQuery"

    def is_connected(self) -> bool:
        return self.client is not None
