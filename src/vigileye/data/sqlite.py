"""SQLite-backed pilot data access, used for local development."""

import sqlite3
from typing import Any

import pandas as pd

from .base import HISTORY_COLUMNS, LATEST_COLUMNS, DataConnector


class SQLiteConnector(DataConnector):
    def __init__(self, db_path: str = "vigileye_fleet.db"):
        self.db_path = db_path

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def fetch_latest_data(self, driver_id: str) -> dict[str, Any]:
        with self._get_conn() as conn:
            cursor = conn.execute(
                f"""
                SELECT {LATEST_COLUMNS}
                FROM pilots p
                JOIN daily_readings d ON p.driver_id = d.driver_id
                WHERE p.driver_id = ?
                ORDER BY d.date DESC LIMIT 1
                """,
                (driver_id,),
            )
            row = cursor.fetchone()
            return dict(row) if row else {}

    def get_pilot_history(self, driver_id: str, days: int = 30) -> pd.DataFrame:
        with self._get_conn() as conn:
            # Most recent `days` rows, returned oldest-first for plotting.
            return pd.read_sql_query(
                f"""
                SELECT * FROM (
                    SELECT {HISTORY_COLUMNS} FROM daily_readings
                    WHERE driver_id = ? ORDER BY date DESC LIMIT ?
                ) ORDER BY date ASC
                """,
                conn,
                params=(driver_id, days),
            )

    def get_fleet_summary(self) -> pd.DataFrame:
        with self._get_conn() as conn:
            return pd.read_sql_query(
                """
                WITH RankedReadings AS (
                    SELECT p.driver_id, p.name, p.role, d.total_sleep_hours, d.hrv_ms,
                           d.report_time, d.consecutive_duty_days, d.deep_sleep_pct,
                           d.rem_sleep_pct, d.resting_hr, d.time_awake_since_last_sleep,
                           ROW_NUMBER() OVER(PARTITION BY p.driver_id ORDER BY d.date DESC) as rn
                    FROM pilots p
                    JOIN daily_readings d ON p.driver_id = d.driver_id
                )
                SELECT driver_id, name, role, total_sleep_hours, hrv_ms, report_time,
                       consecutive_duty_days, deep_sleep_pct, rem_sleep_pct, resting_hr,
                       time_awake_since_last_sleep
                FROM RankedReadings WHERE rn = 1
                """,
                conn,
            )

    def pilot_exists(self, driver_id: str) -> bool:
        with self._get_conn() as conn:
            return conn.execute(
                "SELECT 1 FROM pilots WHERE driver_id = ? LIMIT 1", (driver_id,)
            ).fetchone() is not None

    def create_pilot(self, pilot: dict[str, Any]) -> None:
        columns = ", ".join(pilot)
        placeholders = ", ".join("?" for _ in pilot)
        with self._get_conn() as conn:
            conn.execute(
                f"INSERT INTO pilots ({columns}) VALUES ({placeholders})",
                tuple(pilot.values()),
            )

    def update_pilot(self, driver_id: str, fields: dict[str, Any]) -> None:
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self._get_conn() as conn:
            conn.execute(
                f"UPDATE pilots SET {assignments} WHERE driver_id = ?",
                (*fields.values(), driver_id),
            )

    def delete_pilot(self, driver_id: str) -> None:
        with self._get_conn() as conn:
            conn.execute("DELETE FROM daily_readings WHERE driver_id = ?", (driver_id,))
            conn.execute("DELETE FROM pilots WHERE driver_id = ?", (driver_id,))

    def upsert_reading(self, driver_id: str, reading: dict[str, Any]) -> None:
        row = {"driver_id": driver_id, **reading}
        row["date"] = str(row["date"])
        columns = ", ".join(row)
        placeholders = ", ".join("?" for _ in row)
        with self._get_conn() as conn:
            conn.execute(
                "DELETE FROM daily_readings WHERE driver_id = ? AND date = ?",
                (driver_id, row["date"]),
            )
            conn.execute(
                f"INSERT INTO daily_readings ({columns}) VALUES ({placeholders})",
                tuple(row.values()),
            )

    def get_source_name(self) -> str:
        return "SQLite (local development)"

    def is_connected(self) -> bool:
        try:
            with self._get_conn() as conn:
                conn.execute("SELECT 1 FROM pilots LIMIT 1")
            return True
        except sqlite3.Error:
            return False
