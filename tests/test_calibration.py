import pytest

from tokenmeter.calibration import CalibrationError, calibrate


def test_derives_the_budget_from_a_real_observation():
    """The case actually measured: claude.ai showed 24% with 83,854 tokens."""
    cal = calibrate(24, 83_854, basis="io")
    assert cal.budget_tokens == 349_392
    assert cal.reliable is True


def test_the_rounding_range_brackets_the_estimate():
    cal = calibrate(24, 83_854)
    # The web rounds: 24% is really [23.5%, 24.5%).
    assert cal.budget_low < cal.budget_tokens < cal.budget_high
    assert cal.budget_low == round(83_854 / 0.245)
    assert cal.budget_high == round(83_854 / 0.235)


def test_a_low_observed_percent_is_flagged_as_unreliable():
    cal = calibrate(3, 10_000)
    assert cal.reliable is False
    assert "rounding" in cal.summary()
    # With 3% observed the range is huge relative to the estimated value.
    assert cal.uncertainty_percent > 30


def test_a_high_observed_percent_is_tight():
    cal = calibrate(80, 400_000)
    assert cal.reliable is True
    assert cal.uncertainty_percent < 2


def test_rejects_impossible_percentages():
    for bad in (0, -5, 101, 250):
        with pytest.raises(CalibrationError):
            calibrate(bad, 1000)


def test_rejects_calibrating_with_nothing_counted():
    with pytest.raises(CalibrationError) as exc:
        calibrate(24, 0)
    assert "has not counted any tokens" in str(exc.value)


def test_summary_states_the_inputs_so_the_number_is_auditable():
    text = calibrate(50, 200_000).summary()
    assert "200,000" in text
    assert "50%" in text
    assert "400,000" in text
