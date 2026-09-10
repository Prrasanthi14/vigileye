"""API contract: roster writes, validation, and the biometric write boundary."""

import pytest

READING = {
    "date": "2026-09-10", "total_sleep_hours": 9.0, "deep_sleep_pct": 25,
    "rem_sleep_pct": 25, "light_sleep_pct": 45, "awake_during_sleep_pct": 5,
    "hrv_ms": 90, "resting_hr": 45, "time_awake_since_last_sleep": 1,
    "consecutive_duty_days": 0,
}


def test_health_reports_data_source(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["data_connected"] is True


def test_fleet_returns_every_pilot_with_a_verdict(client):
    rows = client.get("/api/v1/fleet").json()
    assert len(rows) == 2
    assert all(row["status"] in {"CLEAR", "PENDING_TEST", "GROUNDED"} for row in rows)
    assert all("score" in row and "source" in row for row in rows)


def test_pilot_history_honours_days(client):
    assert len(client.get("/api/v1/pilots/PAT-001?days=7").json()["history"]) == 7
    assert len(client.get("/api/v1/pilots/PAT-001?days=3").json()["history"]) == 3


def test_history_returns_the_most_recent_window(client):
    """Regression: ORDER BY date ASC LIMIT n returned the oldest days instead."""
    week = client.get("/api/v1/pilots/PAT-001?days=7").json()["history"]
    full = client.get("/api/v1/pilots/PAT-001?days=10").json()["history"]
    assert week[-1]["date"] == full[-1]["date"]


@pytest.mark.parametrize("days", [0, -1, 400])
def test_days_outside_bounds_rejected(client, days):
    assert client.get(f"/api/v1/pilots/PAT-001?days={days}").status_code == 422


def test_unknown_pilot_is_404(client):
    assert client.get("/api/v1/pilots/NOPE-999").status_code == 404


class TestBiometricWriteBoundary:
    """Readings are evidence; no HTTP verb may create or alter them."""

    @pytest.mark.parametrize("method", ["put", "post", "patch"])
    def test_no_write_path_for_readings(self, client, method):
        response = getattr(client, method)(
            "/api/v1/pilots/PAT-001/readings", json=READING
        )
        assert response.status_code in (404, 405)

    def test_no_delete_path_for_readings(self, client):
        assert client.delete("/api/v1/pilots/PAT-001/readings").status_code in (404, 405)

    def test_roster_update_cannot_smuggle_biometrics(self, client):
        client.patch("/api/v1/pilots/PAT-001", json={"hrv_ms": 999, "name": "Renamed"})
        snapshot = client.get("/api/v1/pilots/PAT-001").json()["snapshot"]
        assert snapshot["hrv_ms"] != 999


class TestRosterWrites:
    def test_create_then_delete(self, client):
        created = client.post("/api/v1/pilots",
                              json={"driver_id": "PAT-900", "name": "New Pilot"})
        assert created.status_code == 201
        assert client.delete("/api/v1/pilots/PAT-900").status_code == 200

    def test_duplicate_id_is_409(self, client):
        assert client.post("/api/v1/pilots",
                           json={"driver_id": "PAT-001", "name": "Clash"}).status_code == 409

    @pytest.mark.parametrize("payload", [
        {"driver_id": "has space", "name": "X"},
        {"driver_id": "PAT-901", "name": "X", "shift_type": "Weekend"},
        {"driver_id": "PAT-901"},
        {"driver_id": "", "name": "X"},
    ])
    def test_invalid_create_payloads_rejected(self, client, payload):
        assert client.post("/api/v1/pilots", json=payload).status_code == 422

    def test_patch_unknown_pilot_is_404(self, client):
        assert client.patch("/api/v1/pilots/NOPE-999",
                            json={"name": "X"}).status_code == 404

    def test_patch_with_no_fields_is_400(self, client):
        assert client.patch("/api/v1/pilots/PAT-001", json={}).status_code == 400

    def test_patch_updates_only_supplied_fields(self, client):
        client.patch("/api/v1/pilots/PAT-001", json={"role": "Captain"})
        snapshot = client.get("/api/v1/pilots/PAT-001").json()["snapshot"]
        assert snapshot["role"] == "Captain"
        assert snapshot["name"] == "Rested Pilot"

    def test_delete_unknown_pilot_is_404(self, client):
        assert client.delete("/api/v1/pilots/NOPE-999").status_code == 404
