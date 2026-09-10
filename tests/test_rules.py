"""Deterministic scoring engine."""

import pytest

from vigileye.scoring.rules import (
    CIRCADIAN_WEIGHT,
    FACTORS,
    TOTAL_WEIGHT,
    evaluate_with_rules,
    reconcile,
    score_pilot,
    status_for,
)

PERFECT = dict(total_sleep_hours=8.5, deep_sleep_pct=25, rem_sleep_pct=25, hrv_ms=70,
               time_awake_since_last_sleep=2, report_time="09:00", resting_hr=55,
               consecutive_duty_days=1)

WORST = dict(total_sleep_hours=2.0, deep_sleep_pct=4, rem_sleep_pct=5, hrv_ms=10,
             time_awake_since_last_sleep=22, report_time="03:00", resting_hr=95,
             consecutive_duty_days=8)


def test_declared_weights_sum_above_one():
    """The band weights total 1.05, which is why normalization is required."""
    assert TOTAL_WEIGHT == pytest.approx(1.05)
    assert sum(f.weight for f in FACTORS) + CIRCADIAN_WEIGHT == pytest.approx(TOTAL_WEIGHT)


def test_perfect_pilot_scores_exactly_100_not_105():
    """Regression: unnormalized weights produced 105 and failed model validation."""
    score, risks, _ = score_pilot(PERFECT)
    assert score == 100
    assert risks == []


def test_worst_pilot_stays_within_range():
    score, risks, _ = score_pilot(WORST)
    assert 0 <= score <= 100
    assert risks


@pytest.mark.parametrize("data", [PERFECT, WORST])
def test_score_always_satisfies_model_bounds(data):
    evaluation = evaluate_with_rules(data)
    assert 0 <= evaluation.score <= 100


@pytest.mark.parametrize("score,expected", [
    (100, "CLEAR"), (70, "CLEAR"), (69, "PENDING_TEST"),
    (45, "PENDING_TEST"), (44, "GROUNDED"), (0, "GROUNDED"),
])
def test_status_thresholds(score, expected):
    assert status_for(score)[0] == expected


@pytest.mark.parametrize("report_time,expect_risk", [
    ("03:00", True), ("02:00", True), ("04:59", True),
    ("01:00", True), ("05:00", True),
    ("09:00", False), ("14:00", False), ("23:00", False),
])
def test_window_of_circadian_low(report_time, expect_risk):
    _, risks, note = score_pilot({**PERFECT, "report_time": report_time})
    assert any("Circadian" in r for r in risks) is expect_risk
    assert note


def test_unparseable_report_time_does_not_raise():
    score, _, note = score_pilot({**PERFECT, "report_time": "not-a-time"})
    assert 0 <= score <= 100
    assert "outside" in note


def test_missing_fields_fall_back_to_defaults():
    score, _, _ = score_pilot({})
    assert 0 <= score <= 100


def test_worse_biometrics_never_score_higher():
    assert score_pilot(PERFECT)[0] > score_pilot(WORST)[0]


def test_reasoning_names_the_score():
    evaluation = evaluate_with_rules(WORST)
    assert str(evaluation.score) in evaluation.reasoning


class TestMedicalHistory:
    """The fallback must not judge a pilot with a sleep disorder as healthy."""

    def test_a_condition_lowers_the_score_and_is_named(self):
        score, risks, _ = score_pilot({**PERFECT, "medical_history": "Narcolepsy (Treated)"})
        assert score < score_pilot(PERFECT)[0]
        assert any("Narcolepsy" in r for r in risks)

    @pytest.mark.parametrize("history", ["None", "", None])
    def test_no_condition_means_no_penalty(self, history):
        assert score_pilot({**PERFECT, "medical_history": history})[0] == score_pilot(PERFECT)[0]

    def test_apnea_compounds_with_low_deep_sleep(self):
        apnea = {**PERFECT, "medical_history": "Mild Sleep Apnea"}
        penalty_good_deep = score_pilot(PERFECT)[0] - score_pilot(apnea)[0]
        low_deep = {**PERFECT, "deep_sleep_pct": 10}
        penalty_low_deep = score_pilot(low_deep)[0] - score_pilot({**low_deep, **apnea, "deep_sleep_pct": 10})[0]
        assert penalty_low_deep > penalty_good_deep

    def test_score_never_goes_negative(self):
        assert score_pilot({**WORST, "medical_history": "Narcolepsy, Sleep Apnea, Insomnia"})[0] >= 0


class TestReconcile:
    """Gemini's label and score must never contradict; the stricter one wins."""

    @pytest.mark.parametrize("status,score,expected", [
        ("PENDING_TEST", 85, ("PENDING_TEST", 69)),   # cautious label kept, score moved into band
        ("GROUNDED", 45, ("GROUNDED", 44)),
        ("CLEAR", 30, ("GROUNDED", 30)),              # low score overrides a lenient label
        ("CLEAR", 90, ("CLEAR", 90)),                 # already consistent: untouched
        ("PENDING_TEST", 50, ("PENDING_TEST", 50)),
    ])
    def test_keeps_the_stricter_reading(self, status, score, expected):
        assert reconcile(status, score) == expected

    def test_every_combination_ends_consistent(self):
        for status in ("CLEAR", "PENDING_TEST", "GROUNDED"):
            for score in range(101):
                final, adjusted = reconcile(status, score)
                assert status_for(adjusted)[0] == final

    def test_gemini_output_is_reconciled_and_keeps_its_action_when_label_holds(self):
        from vigileye.models import ReadinessEvaluation
        from vigileye.scoring.agent import _consistent

        out = _consistent(ReadinessEvaluation(
            status="PENDING_TEST", score=85, reasoning="r", recommended_action="Test first",
            risk_factors=[], circadian_note=""))
        assert (out.status, out.score, out.recommended_action) == ("PENDING_TEST", 69, "Test first")
