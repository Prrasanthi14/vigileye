"""
Abstract data connector layer for the Patchamomma driver safety dashboard.

Supports multiple data sources including a local SQLite time-series database
simulating an enterprise data warehouse (e.g. BigQuery) with 30 days of history.
"""

import logging
import sqlite3
import pandas as pd
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)

SUPPORTED_SOURCES = {
    'sqlite': 'Enterprise Time-Series DB (Simulation)',
    'synthetic': 'Synthetic Data (Built-in test profiles)',
    'bigquery': 'Google BigQuery (Cloud Data Warehouse)',
    'firebase': 'Firebase Firestore Bridge (Health Connect)',
}

class DataConnector(ABC):
    @abstractmethod
    def fetch_latest_data(self, driver_id: str) -> dict:
        """Fetch the latest health/biometric payload for a driver."""
        pass
        
    @abstractmethod
    def get_pilot_history(self, driver_id: str, days: int = 30) -> pd.DataFrame:
        """Fetch historical time-series data for a specific pilot."""
        pass
        
    @abstractmethod
    def get_fleet_summary(self) -> pd.DataFrame:
        """Fetch a summary of all pilots and their most recent readiness data."""
        pass

    @abstractmethod
    def get_source_name(self) -> str:
        """Return human-readable name of this data source."""
        pass

    @abstractmethod
    def is_connected(self) -> bool:
        """Check if the data source is currently accessible."""
        pass

class SQLiteConnector(DataConnector):
    """
    Connects to the local SQLite database simulating a 30-day time-series 
    enterprise data warehouse (like BigQuery).
    """
    def __init__(self, db_path="patchamomma_fleet.db"):
        self.db_path = db_path

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def fetch_latest_data(self, driver_id: str) -> dict:
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT p.driver_id, p.name, p.role, p.shift_type, p.base_timezone as report_timezone,
                       p.medical_history,
                       d.date as last_sync_timestamp, d.report_time, d.total_sleep_hours, 
                       d.deep_sleep_pct, d.rem_sleep_pct, d.light_sleep_pct, d.awake_during_sleep_pct,
                       d.hrv_ms, d.resting_hr, d.time_awake_since_last_sleep, d.consecutive_duty_days
                FROM pilots p
                JOIN daily_readings d ON p.driver_id = d.driver_id
                WHERE p.driver_id = ?
                ORDER BY d.date DESC LIMIT 1
            ''', (driver_id,))
            row = cursor.fetchone()
            if not row:
                return {}
            return dict(row)

    def get_pilot_history(self, driver_id: str, days: int = 30) -> pd.DataFrame:
        with self._get_conn() as conn:
            query = '''
                SELECT date, total_sleep_hours, hrv_ms, resting_hr, consecutive_duty_days, report_time
                FROM daily_readings
                WHERE driver_id = ?
                ORDER BY date ASC
                LIMIT ?
            '''
            df = pd.read_sql_query(query, conn, params=(driver_id, days))
            return df

    def get_fleet_summary(self) -> pd.DataFrame:
        with self._get_conn() as conn:
            query = '''
                WITH RankedReadings AS (
                    SELECT p.driver_id, p.name, p.role, d.total_sleep_hours, d.hrv_ms, d.report_time, d.consecutive_duty_days,
                           ROW_NUMBER() OVER(PARTITION BY p.driver_id ORDER BY d.date DESC) as rn
                    FROM pilots p
                    JOIN daily_readings d ON p.driver_id = d.driver_id
                )
                SELECT driver_id, name, role, total_sleep_hours, hrv_ms, report_time, consecutive_duty_days
                FROM RankedReadings WHERE rn = 1
            '''
            df = pd.read_sql_query(query, conn)
            return df

    def get_source_name(self) -> str:
        return 'SQLite Time-Series DB (Enterprise Simulation)'

    def is_connected(self) -> bool:
        try:
            with self._get_conn() as conn:
                conn.execute("SELECT 1 FROM pilots LIMIT 1")
            return True
        except sqlite3.Error:
            return False

# Scaffold classes for future real integration
class BigQueryConnector(DataConnector):
    """
    Connects to Google Cloud BigQuery for production enterprise data.
    Requires GOOGLE_APPLICATION_CREDENTIALS in .env or the deployment environment.
    """
    def __init__(self, project_id: str, dataset_id: str = "patchamomma_fleet"):
        from google.cloud import bigquery
        self.project_id = project_id
        self.dataset_id = dataset_id
        try:
            self.client = bigquery.Client(project=project_id)
        except Exception as e:
            logger.error(f"Failed to initialize BigQuery client: {e}")
            self.client = None

    def fetch_latest_data(self, driver_id: str) -> dict:
        if not self.client: return {}
        query = f"""
            SELECT p.driver_id, p.name, p.role, p.shift_type, p.base_timezone as report_timezone,
                   p.medical_history,
                   d.date as last_sync_timestamp, d.report_time, d.total_sleep_hours, 
                   d.deep_sleep_pct, d.rem_sleep_pct, d.light_sleep_pct, d.awake_during_sleep_pct,
                   d.hrv_ms, d.resting_hr, d.time_awake_since_last_sleep, d.consecutive_duty_days
            FROM `{self.project_id}.{self.dataset_id}.pilots` p
            JOIN `{self.project_id}.{self.dataset_id}.daily_readings` d ON p.driver_id = d.driver_id
            WHERE p.driver_id = @driver_id
            ORDER BY d.date DESC LIMIT 1
        """
        from google.cloud import bigquery
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("driver_id", "STRING", driver_id)]
        )
        result = self.client.query(query, job_config=job_config).result()
        for row in result:
            return dict(row)
        return {}

    def get_pilot_history(self, driver_id: str, days: int = 30) -> pd.DataFrame:
        if not self.client: return pd.DataFrame()
        query = f"""
            SELECT date, total_sleep_hours, hrv_ms, resting_hr, consecutive_duty_days, report_time
            FROM `{self.project_id}.{self.dataset_id}.daily_readings`
            WHERE driver_id = @driver_id
            ORDER BY date ASC
            LIMIT @days
        """
        from google.cloud import bigquery
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("driver_id", "STRING", driver_id),
                bigquery.ScalarQueryParameter("days", "INT64", days)
            ]
        )
        return self.client.query(query, job_config=job_config).to_dataframe()

    def get_fleet_summary(self) -> pd.DataFrame:
        if not self.client: return pd.DataFrame()
        query = f"""
            WITH RankedReadings AS (
                SELECT p.driver_id, p.name, p.role, d.total_sleep_hours, d.hrv_ms, d.report_time, d.consecutive_duty_days,
                       ROW_NUMBER() OVER(PARTITION BY p.driver_id ORDER BY d.date DESC) as rn
                FROM `{self.project_id}.{self.dataset_id}.pilots` p
                JOIN `{self.project_id}.{self.dataset_id}.daily_readings` d ON p.driver_id = d.driver_id
            )
            SELECT driver_id, name, role, total_sleep_hours, hrv_ms, report_time, consecutive_duty_days
            FROM RankedReadings WHERE rn = 1
        """
        return self.client.query(query).to_dataframe()

    def get_source_name(self) -> str:
        return "Google BigQuery"

    def is_connected(self) -> bool:
        return self.client is not None

class FirebaseConnector(DataConnector):
    def fetch_latest_data(self, driver_id: str) -> dict: raise NotImplementedError()
    def get_pilot_history(self, driver_id: str, days: int = 30) -> pd.DataFrame: raise NotImplementedError()
    def get_fleet_summary(self) -> pd.DataFrame: raise NotImplementedError()
    def get_source_name(self) -> str: return "Firebase Firestore"
    def is_connected(self) -> bool: return False

def get_connector(source: str = 'sqlite') -> DataConnector:
    if source == 'bigquery':
        # Use the GCP project ID where the data was uploaded
        return BigQueryConnector(project_id="gen-lang-client-0469618448")
    elif source == 'firebase':
        return FirebaseConnector()
    else:
        return SQLiteConnector()
