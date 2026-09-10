"""Token accounting: the saving calculation, the budget guard, and what reaches Gemini."""

import json
from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from vigileye.config import settings
from vigileye.models import Evaluation
from vigileye.scoring import agent, service, usage

SNAPSHOT = dict(driver_id="PAT-001", name="Yara Haddad", last_sync_timestamp=date(2026, 9, 10),
                total_sleep_hours=8.2, deep_sleep_pct=21, rem_sleep_pct=22, hrv_ms=72,
                resting_hr=52, time_awake_since_last_sleep=2.0, report_time="09:00",
                consecutive_duty_days=1)


class TestSavingCalculation:
    def test_saving_is_store_hits_priced_at_the_measured_average(self):
        report = usage.summarize({
            "model_call": dict(n=4, prompt=2000, thinking=6000, output=1600, total=9600),
            "store_hit": dict(n=12, prompt=0, thinking=0, output=0, total=0),
        }, budget=100_000)

        assert report["avg_tokens_per_call"] == 2400
        assert report["tokens_saved_by_store"] == 12 * 2400
        assert report["saving_pct"] == 75.0
        assert report["budget_remaining"] == 90_400
        assert report["tokens_by_part"] == {"prompt": 2000, "thinking": 6000, "output": 1600}

    def test_no_activity_reports_zeros_not_errors(self):
        report = usage.summarize({}, budget=0)
        assert report["model_calls"] == 0
        assert report["tokens_saved_by_store"] == 0
        assert report["saving_pct"] == 0.0
        assert report["daily_budget"] is None

    def test_budget_remaining_never_goes_negative(self):
        report = usage.summarize({"model_call": dict(n=1, total=5000)}, budget=1000)
        assert report["budget_remaining"] == 0


class TestBudgetGuard:
    def test_exhausted_budget_blocks_the_model_call(self, monkeypatch):
        monkeypatch.setattr(agent, "settings",
                            replace(settings, gemini_api_key="test-key", daily_token_budget=1000))
        monkeypatch.setattr(usage, "settings", replace(settings, daily_token_budget=1000))
        monkeypatch.setattr(usage, "tokens_used_today", lambda: 1500)

        with pytest.raises(agent.AgentUnavailable, match="budget"):
            agent.evaluate_with_agent(SNAPSHOT)

    def test_zero_budget_means_no_cap(self, monkeypatch):
        monkeypatch.setattr(usage, "settings", replace(settings, daily_token_budget=0))
        monkeypatch.setattr(usage, "tokens_used_today", lambda: 10**9)
        assert usage.budget_exhausted() == (False, 0)

    def test_budget_fallback_is_labelled_with_the_reason(self, monkeypatch):
        monkeypatch.setattr(service.cache, "get_verdict", lambda *a: None)

        def over_budget(data):
            raise agent.AgentUnavailable("Daily token budget reached (1,500 of 1,000 tokens)")

        monkeypatch.setattr(service, "evaluate_with_agent", over_budget)
        result = service.evaluate_readiness(SNAPSHOT)

        assert result.source == "rules"
        assert "budget" in result.fallback_reason


class TestWhatReachesGemini:
    def test_identity_is_never_sent(self):
        body = agent.build_payload(SNAPSHOT)
        assert "PAT-001" not in body
        assert "Yara" not in body

    def test_payload_is_compact_and_still_complete(self):
        body = agent.build_payload(SNAPSHOT)
        assert "\n" not in body and ": " not in body
        decoded = json.loads(body)
        assert decoded["hrv_ms"] == 72
        assert decoded["last_sync_timestamp"] == "2026-09-10"


def test_a_stored_verdict_is_recorded_as_a_zero_cost_reuse(monkeypatch, ledger):
    stored = Evaluation(status="CLEAR", score=90, reasoning="stored", recommended_action="a",
                        risk_factors=[], circadian_note="", source="agent",
                        evaluated_at=datetime.now(timezone.utc))
    monkeypatch.setattr(service.cache, "get_verdict", lambda d, dt: stored)
    monkeypatch.setattr(service, "evaluate_with_agent",
                        lambda d: pytest.fail("a fresh stored verdict must not call the model"))

    service.evaluate_readiness(SNAPSHOT)
    assert ledger == [("hit", "PAT-001")]


def test_usage_endpoint_reports_today(client):
    body = client.get("/api/v1/usage").json()
    assert body["model_calls"] == 0
    assert "tokens_saved_by_store" in body
