"""Verdict provenance and the agent-to-rules fallback."""

from datetime import date, datetime, timedelta, timezone

import pytest

from vigileye.models import Evaluation, ReadinessEvaluation
from vigileye.scoring import service
from vigileye.scoring.agent import AgentUnavailable

SNAPSHOT = dict(driver_id="PAT-001", last_sync_timestamp=date(2026, 9, 10),
                total_sleep_hours=8.2, deep_sleep_pct=21, rem_sleep_pct=22,
                hrv_ms=72, resting_hr=52, time_awake_since_last_sleep=2.0,
                report_time="09:00", consecutive_duty_days=1)


@pytest.fixture
def no_cache(monkeypatch):
    monkeypatch.setattr(service.cache, "get_verdict", lambda *a: None)
    monkeypatch.setattr(service.cache, "put_verdict", lambda *a: None)


def test_agent_result_is_labelled_agent(monkeypatch, no_cache):
    monkeypatch.setattr(service, "evaluate_with_agent", lambda data: ReadinessEvaluation(
        status="CLEAR", score=90, reasoning="fine", recommended_action="fly",
        risk_factors=[], circadian_note=""))
    result = service.evaluate_readiness(SNAPSHOT)
    assert result.source == "agent"
    assert result.fallback_reason is None


def test_fallback_is_labelled_rules_and_carries_the_reason(monkeypatch, no_cache):
    def unavailable(data):
        raise AgentUnavailable("429 RESOURCE_EXHAUSTED")

    monkeypatch.setattr(service, "evaluate_with_agent", unavailable)
    result = service.evaluate_readiness(SNAPSHOT)

    assert result.source == "rules"
    assert "429" in result.fallback_reason


def test_fallback_still_produces_a_usable_verdict(monkeypatch, no_cache):
    monkeypatch.setattr(service, "evaluate_with_agent",
                        lambda d: (_ for _ in ()).throw(AgentUnavailable("no key")))
    result = service.evaluate_readiness(SNAPSHOT)
    assert result.status in {"CLEAR", "PENDING_TEST", "GROUNDED"}
    assert 0 <= result.score <= 100


def test_agent_verdict_is_cached_and_reused(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(service.cache, "get_verdict",
                        lambda d, dt: store.get((d, dt)))
    monkeypatch.setattr(service.cache, "put_verdict",
                        lambda d, dt, ev: store.__setitem__((d, dt), ev))

    calls = []

    def agent(data):
        calls.append(1)
        return ReadinessEvaluation(status="CLEAR", score=88, reasoning="r",
                                   recommended_action="a", risk_factors=[],
                                   circadian_note="")

    monkeypatch.setattr(service, "evaluate_with_agent", agent)

    first = service.evaluate_readiness(SNAPSHOT)
    second = service.evaluate_readiness(SNAPSHOT)

    assert len(calls) == 1, "second call should be served from the store"
    assert (first.status, first.score) == (second.status, second.score)


def test_rules_fallback_is_not_cached(monkeypatch):
    """A degraded verdict must be retried, not frozen in the store."""
    store: dict = {}
    monkeypatch.setattr(service.cache, "get_verdict", lambda d, dt: store.get((d, dt)))
    monkeypatch.setattr(service.cache, "put_verdict",
                        lambda d, dt, ev: store.__setitem__((d, dt), ev))
    monkeypatch.setattr(service, "evaluate_with_agent",
                        lambda d: (_ for _ in ()).throw(AgentUnavailable("down")))

    service.evaluate_readiness(SNAPSHOT)
    assert store == {}


def test_refresh_bypasses_the_store(monkeypatch):
    store = {("PAT-001", date(2026, 9, 10)): Evaluation(
        status="CLEAR", score=10, reasoning="stale", recommended_action="a",
        risk_factors=[], circadian_note="", source="agent")}
    monkeypatch.setattr(service.cache, "get_verdict", lambda d, dt: store.get((d, dt)))
    monkeypatch.setattr(service.cache, "put_verdict", lambda *a: None)
    monkeypatch.setattr(service, "evaluate_with_agent", lambda d: ReadinessEvaluation(
        status="GROUNDED", score=20, reasoning="fresh", recommended_action="a",
        risk_factors=[], circadian_note=""))

    assert service.evaluate_readiness(SNAPSHOT, use_cache=True).reasoning == "stale"
    assert service.evaluate_readiness(SNAPSHOT, use_cache=False).reasoning == "fresh"


@pytest.mark.parametrize("score", [95, 70, 50, 20])
def test_pvt_simulation_is_internally_consistent(score):
    result = service.simulate_pvt_test(score)
    assert result["result"] in {"PASS", "FAIL"}
    assert result["fastest_ms"] <= result["mean_reaction_ms"] <= result["slowest_ms"]


class TestVerdictStaleness:
    """A verdict has a shelf life: hours awake climb whether the stored reading
    changes or not, so an old verdict is re-run rather than served."""

    def _stored(self, minutes_old: float) -> Evaluation:
        return Evaluation(
            status="CLEAR", score=95, reasoning="stored", recommended_action="a",
            risk_factors=[], circadian_note="", source="agent",
            evaluated_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_old),
        )

    @pytest.mark.parametrize("age,expected", [(1, False), (119, False),
                                              (121, True), (600, True)])
    def test_staleness_threshold(self, age, expected):
        assert service.is_stale(self._stored(age)) is expected

    def test_verdict_without_timestamp_is_not_stale(self):
        evaluation = self._stored(1)
        evaluation.evaluated_at = None
        assert service.is_stale(evaluation) is False

    def test_naive_timestamp_is_read_as_utc(self):
        """Verdicts are stored in UTC, so a timestamp that arrives without a
        timezone is UTC rather than local wall-clock time."""
        evaluation = self._stored(1)
        naive_utc = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=30)
        evaluation.evaluated_at = naive_utc
        assert service.verdict_age_minutes(evaluation) == pytest.approx(30, abs=1)

    def test_fresh_verdict_is_served_without_calling_the_agent(self, monkeypatch):
        stored = self._stored(10)
        monkeypatch.setattr(service.cache, "get_verdict", lambda d, dt: stored)
        monkeypatch.setattr(service.cache, "put_verdict", lambda *a: None)
        monkeypatch.setattr(service, "evaluate_with_agent",
                            lambda d: pytest.fail("agent must not run for a fresh verdict"))
        assert service.evaluate_readiness(SNAPSHOT).reasoning == "stored"

    def test_stale_verdict_triggers_a_fresh_agent_run(self, monkeypatch):
        monkeypatch.setattr(service.cache, "get_verdict",
                            lambda d, dt: self._stored(180))
        monkeypatch.setattr(service.cache, "put_verdict", lambda *a: None)
        monkeypatch.setattr(service, "evaluate_with_agent", lambda d: ReadinessEvaluation(
            status="GROUNDED", score=30, reasoning="re-run", recommended_action="a",
            risk_factors=[], circadian_note=""))
        result = service.evaluate_readiness(SNAPSHOT)
        assert result.reasoning == "re-run"
        assert result.evaluated_at is not None

    def test_fresh_agent_verdict_is_stamped(self, monkeypatch):
        monkeypatch.setattr(service.cache, "get_verdict", lambda *a: None)
        monkeypatch.setattr(service.cache, "put_verdict", lambda *a: None)
        monkeypatch.setattr(service, "evaluate_with_agent", lambda d: ReadinessEvaluation(
            status="CLEAR", score=90, reasoning="r", recommended_action="a",
            risk_factors=[], circadian_note=""))
        assert service.evaluate_readiness(SNAPSHOT).evaluated_at is not None
