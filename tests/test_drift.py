"""Calibration drift and multi-point fit."""

import pytest

from tokenmeter.calibration import fit_session, measure_drift, predict_percent


# -- prediction error ------------------------------------------------------


def test_no_drift_when_the_budget_predicts_the_observation():
    d = measure_drift(observed_percent=24.0, counted_tokens=83_854, budget_tokens=349_392)
    assert d.predicted_percent == pytest.approx(24.0, abs=0.01)
    assert abs(d.points) < 0.1
    assert d.significant is False


def test_drift_is_signed_so_it_says_in_which_direction():
    # The meter falls short: the web shows more than we predicted.
    short = measure_drift(observed_percent=40.0, counted_tokens=100_000, budget_tokens=350_000)
    assert short.points > 0
    assert "higher" in short.summary()

    # The meter overshoots: we predicted more than the web shows.
    long = measure_drift(observed_percent=20.0, counted_tokens=100_000, budget_tokens=350_000)
    assert long.points < 0
    assert "lower" in long.summary()


def test_small_differences_are_not_flagged():
    """The web rounds to an integer: half a point is not a signal of anything."""
    d = measure_drift(observed_percent=24.0, counted_tokens=84_000, budget_tokens=349_392)
    assert d.significant is False


def test_a_large_error_is_flagged():
    d = measure_drift(observed_percent=45.0, counted_tokens=100_000, budget_tokens=350_000)
    assert d.significant is True
    assert d.relative > 0.10


def test_drift_needs_something_to_compare():
    assert measure_drift(24.0, 0, 350_000) is None
    assert measure_drift(24.0, 1000, 0) is None


def test_predict_percent_is_the_inverse_of_calibrate():
    from tokenmeter.calibration import calibrate

    cal = calibrate(24, 83_854)
    assert predict_percent(83_854, cal.budget_tokens) == pytest.approx(24.0, abs=0.01)


# -- multi-point fit -------------------------------------------------------


def test_two_clean_points_recover_the_budget():
    """Without invisible tokens, the line passes through the origin."""
    fit = fit_session([(100_000, 20.0), (200_000, 40.0)])
    assert fit.budget_tokens == 500_000
    assert fit.unseen_tokens == 0
    assert fit.exact is True


def test_the_intercept_reveals_usage_the_meter_cannot_see():
    """Browser spend: the web counts 50k tokens that TokenMeter does not see.

    With a 500,000 quota and 50,000 invisible:
      100,000 counted → (100,000+50,000)/500,000 = 30%
      200,000 counted → (200,000+50,000)/500,000 = 50%
    """
    fit = fit_session([(100_000, 30.0), (200_000, 50.0)])
    assert fit.budget_tokens == 500_000
    assert fit.unseen_tokens == 50_000


def test_three_points_expose_the_residual():
    fit = fit_session([(100_000, 20.0), (200_000, 40.0), (300_000, 61.0)])
    assert fit.points == 3
    assert fit.exact is False
    assert fit.max_residual_points > 0
    assert fit.budget_tokens == pytest.approx(490_000, rel=0.05)


def test_a_negative_intercept_is_reported_not_hidden():
    """Real project data: two calibrations from the same session.

    24% with 83,854 tokens and 76% with 189,404. The line crosses the axis below
    zero: TokenMeter counted tokens the web does not attribute to that session,
    almost certainly because the two 5 h windows do not start at the same time.
    """
    fit = fit_session([(83_854, 24.0), (189_404, 76.0)])
    assert fit.intercept_points < 0
    assert fit.misaligned is True
    assert fit.unseen_tokens == 0, "the negative is clipped, but not silently"
    assert "do not start at the same time" in fit.summary()


def test_a_slightly_negative_intercept_is_just_noise():
    """Half a point below zero is the web's rounding, not misalignment."""
    fit = fit_session([(100_000, 19.9), (200_000, 40.0)])
    assert fit.intercept_points < 0
    assert fit.misaligned is False
    assert "do not start at the same time" not in fit.summary()


def test_a_single_point_cannot_be_fitted():
    assert fit_session([(100_000, 20.0)]) is None
    assert fit_session([]) is None


def test_repeated_observations_at_the_same_token_count_are_not_a_line():
    assert fit_session([(100_000, 20.0), (100_000, 21.0)]) is None


def test_a_decreasing_percentage_is_rejected_instead_of_fitted():
    """Spending more cannot lower the percentage: the fit would make no sense."""
    assert fit_session([(100_000, 40.0), (200_000, 20.0)]) is None


def test_duplicate_points_are_collapsed_not_double_weighted():
    one = fit_session([(100_000, 20.0), (200_000, 40.0)])
    two = fit_session([(100_000, 20.0), (100_000, 20.0), (200_000, 40.0)])
    assert two.points == one.points == 2
    assert two.budget_tokens == one.budget_tokens


# -- persistence -----------------------------------------------------------


def save(store, **kw):
    base = dict(ts=1000.0, observed_percent=24.0, counted_tokens=83_854, basis="io",
                budget_tokens=349_392, session_start_ts=900.0, predicted_percent=None)
    base.update(kw)
    store.add_calibration(**base)


def test_calibrations_are_kept_not_overwritten(tmp_path):
    from tokenmeter.store import Store

    store = Store(tmp_path / "c.db")
    save(store, ts=1000.0, observed_percent=24.0)
    save(store, ts=2000.0, observed_percent=40.0)

    rows = store.calibrations()
    assert len(rows) == 2
    assert rows[0]["observed_percent"] == 40.0, "the most recent first"


def test_only_points_from_the_same_session_are_fitted_together(tmp_path):
    """The invisible tokens change in every session: mixing them falsifies the fit."""
    from tokenmeter.store import Store

    store = Store(tmp_path / "c.db")
    save(store, session_start_ts=900.0, counted_tokens=100_000, observed_percent=30.0)
    save(store, session_start_ts=900.0, counted_tokens=200_000, observed_percent=50.0)
    save(store, session_start_ts=99_000.0, counted_tokens=50_000, observed_percent=10.0)

    of_the_session = store.calibrations_for_session(900.0)
    assert len(of_the_session) == 2

    fit = fit_session([(r["counted_tokens"], r["observed_percent"]) for r in of_the_session])
    assert fit.budget_tokens == 500_000
    assert fit.unseen_tokens == 50_000


def test_the_recorded_prediction_lets_us_measure_the_previous_error(tmp_path):
    from tokenmeter.store import Store

    store = Store(tmp_path / "c.db")
    save(store, observed_percent=40.0, predicted_percent=28.6)

    row = store.calibrations()[0]
    d = measure_drift(row["observed_percent"], row["counted_tokens"],
                      row["budget_tokens"])
    assert row["predicted_percent"] == 28.6
    assert d is not None


def test_summary_mentions_unseen_tokens_only_when_there_are_any():
    with_unseen = fit_session([(100_000, 30.0), (200_000, 50.0)]).summary()
    without = fit_session([(100_000, 20.0), (200_000, 40.0)]).summary()
    assert "does not see" in with_unseen
    assert "does not see" not in without
