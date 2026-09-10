"""Test fixtures.

Tests run against SQLite and a stubbed verdict store, so the suite needs no
GCP credentials, makes no BigQuery calls and never calls the Gemini API.
"""

import os
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.update(
    DATA_SOURCE="sqlite",
    GCP_PROJECT_ID="test-project",
    BIGQUERY_DATASET="test_dataset",
    GEMINI_API_KEY="",
)

SCHEMA = """
CREATE TABLE pilots (
    driver_id TEXT PRIMARY KEY, name TEXT, role TEXT,
    shift_type TEXT, base_timezone TEXT, medical_history TEXT
);
CREATE TABLE daily_readings (
    driver_id TEXT, date TEXT, report_time TEXT, total_sleep_hours REAL,
    deep_sleep_pct REAL, rem_sleep_pct REAL, light_sleep_pct REAL,
    awake_during_sleep_pct REAL, hrv_ms REAL, resting_hr INTEGER,
    time_awake_since_last_sleep REAL, consecutive_duty_days INTEGER
);
"""

RESTED = dict(report_time="09:00", total_sleep_hours=8.2, deep_sleep_pct=21.0,
              rem_sleep_pct=22.0, light_sleep_pct=53.0, awake_during_sleep_pct=4.0,
              hrv_ms=72.0, resting_hr=52, time_awake_since_last_sleep=2.0,
              consecutive_duty_days=1)

EXHAUSTED = dict(report_time="03:30", total_sleep_hours=3.1, deep_sleep_pct=7.0,
                 rem_sleep_pct=8.0, light_sleep_pct=71.0, awake_during_sleep_pct=14.0,
                 hrv_ms=17.0, resting_hr=89, time_awake_since_last_sleep=19.0,
                 consecutive_duty_days=7)


@pytest.fixture
def db_path(tmp_path) -> str:
    path = tmp_path / "test_fleet.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)

    conn.execute("INSERT INTO pilots VALUES (?,?,?,?,?,?)",
                 ("PAT-001", "Rested Pilot", "Commercial Pilot", "Day", "UTC", "None"))
    conn.execute("INSERT INTO pilots VALUES (?,?,?,?,?,?)",
                 ("PAT-002", "Tired Pilot", "Commercial Pilot", "Night", "UTC",
                  "Mild Sleep Apnea"))

    # 10 days of history each, so ?days=N has something to slice.
    for offset in range(10):
        day = (date.today() - timedelta(days=offset)).isoformat()
        for driver_id, values in (("PAT-001", RESTED), ("PAT-002", EXHAUSTED)):
            conn.execute(
                "INSERT INTO daily_readings VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (driver_id, day, values["report_time"], values["total_sleep_hours"],
                 values["deep_sleep_pct"], values["rem_sleep_pct"], values["light_sleep_pct"],
                 values["awake_during_sleep_pct"], values["hrv_ms"], values["resting_hr"],
                 values["time_awake_since_last_sleep"], values["consecutive_duty_days"]),
            )
    conn.commit()
    conn.close()
    return str(path)


@pytest.fixture
def client(db_path, monkeypatch):
    """TestClient wired to the temp SQLite db, with the verdict store stubbed."""
    from vigileye.data import get_connector
    from vigileye.data.sqlite import SQLiteConnector
    from vigileye.scoring import cache

    # GEMINI_API_KEY is blanked in the environment above, before settings are
    # built, so the agent reports itself unavailable and evaluation uses rules.
    get_connector.cache_clear()
    monkeypatch.setattr("vigileye.data.get_connector",
                        lambda: SQLiteConnector(db_path))
    monkeypatch.setattr("vigileye.api.main.get_connector",
                        lambda: SQLiteConnector(db_path))

    store: dict = {}
    monkeypatch.setattr(cache, "get_verdict", lambda d, dt: store.get((d, dt)))
    monkeypatch.setattr(cache, "put_verdict",
                        lambda d, dt, ev: store.__setitem__((d, dt), ev))
    monkeypatch.setattr(cache, "invalidate",
                        lambda d, dt=None: [store.pop(k) for k in list(store)
                                            if k[0] == d and (dt is None or k[1] == dt)])
    monkeypatch.setattr(cache, "get_fleet_verdicts", dict)
    monkeypatch.setattr(cache, "ensure_table", lambda: None)

    from fastapi.testclient import TestClient
    from vigileye.api.main import app

    yield TestClient(app)
    get_connector.cache_clear()
