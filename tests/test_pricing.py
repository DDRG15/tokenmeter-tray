import pytest

from tokenmeter import pricing
from tokenmeter.pricing import PRICES, Price, cost_usd, format_usd, normalize


def test_normalize_strips_platform_prefixes_and_date_suffixes():
    assert normalize("anthropic.claude-opus-5") == "claude-opus-5"
    assert normalize("claude-haiku-4-5-20251001") == "claude-haiku-4-5"
    assert normalize("claude-opus-4-5@20251101") == "claude-opus-4-5"
    assert normalize("CLAUDE-OPUS-5") == "claude-opus-5"


def test_cost_uses_published_rates():
    # 1M input + 1M output on Opus 5 = $5 + $25
    assert cost_usd("claude-opus-5", 1_000_000, 1_000_000, on_date="2026-08-12") == 30.0


def test_cache_read_is_a_tenth_of_input():
    assert cost_usd("claude-opus-5", 0, 0, cache_read_tokens=1_000_000,
                    on_date="2026-08-12") == 0.5


def test_cache_write_ttl_changes_the_price_by_60_percent():
    """Measured: Claude Code writes 1 h cache, which costs 2x, not 1.25x."""
    five_min = cost_usd("claude-opus-5", 0, 0, cache_write_tokens=1_000_000,
                         on_date="2026-08-12")
    one_hour = cost_usd("claude-opus-5", 0, 0, cache_write_tokens=1_000_000,
                        cache_write_1h_tokens=1_000_000, on_date="2026-08-12")
    assert five_min == 6.25
    assert one_hour == 10.0
    assert one_hour / five_min == 1.6


def test_mixed_ttl_prices_each_part_at_its_rate():
    cost = cost_usd("claude-opus-5", 0, 0, cache_write_tokens=1_000_000,
                     cache_write_1h_tokens=400_000, on_date="2026-08-12")
    # 600k at 1.25x + 400k at 2x, on $5/MTok
    assert cost == pytest.approx(600_000 * 5e-6 * 1.25 + 400_000 * 5e-6 * 2.0)


def test_an_incoherent_breakdown_never_reduces_the_cost():
    """A `1h` larger than the total cannot subtract tokens at 1.25x."""
    cost = cost_usd("claude-opus-5", 0, 0, cache_write_tokens=1_000,
                     cache_write_1h_tokens=999_999, on_date="2026-08-12")
    assert cost == 1_000 * 5e-6 * 2.0


def test_intro_pricing_applies_only_until_its_end_date(monkeypatch):
    monkeypatch.setitem(PRICES, "claude-promo-test", Price(
        3.00, 15.00, intro_input_per_mtok=2.00, intro_output_per_mtok=10.00,
        intro_until="2026-08-31"))
    inside = cost_usd("claude-promo-test", 1_000_000, 0, on_date="2026-08-31")
    outside = cost_usd("claude-promo-test", 1_000_000, 0, on_date="2026-09-01")
    assert inside == 2.0
    assert outside == 3.0


def test_sonnet_5_is_priced_at_its_standard_rate_on_every_date():
    """The $2 / $10 launch price became permanent; no increase on 2026-09-01."""
    for day in ("2026-08-31", "2026-09-01", "2026-11-12"):
        assert cost_usd("claude-sonnet-5", 1_000_000, 1_000_000, on_date=day) == 12.0


def test_unknown_model_returns_none_never_a_guess():
    assert cost_usd("gpt-5", 1_000_000, 1_000_000) is None
    assert cost_usd("llama3.2", 1_000, 1_000) is None
    assert format_usd(None) == "—"


def test_format_keeps_precision_on_small_amounts():
    assert format_usd(0.0031) == "$0.0031"
    assert format_usd(12.5) == "$12.50"


def test_normalize_openrouter_ids():
    """`anthropic/claude-haiku-4.5` used to come out as `—` in the API equivalent."""
    assert normalize("anthropic/claude-haiku-4.5") == "claude-haiku-4-5"
    assert normalize("anthropic/claude-sonnet-4.5") == "claude-sonnet-4-5"
    assert normalize("anthropic/claude-opus-4.6:thinking") == "claude-opus-4-6"
    assert normalize("openai/gpt-4o") == "openai/gpt-4o"   # no price: still `—`
    assert cost_usd("openai/gpt-4o", 1, 1) is None


def test_openrouter_cost_prices_the_cache_write():
    # 94 input + 2 output + 100 written at 5 min on Haiku 4.5 ($1 / $5)
    cost = cost_usd("anthropic/claude-haiku-4.5", 94, 2, cache_write_tokens=100,
                     on_date="2026-10-14")
    assert cost == pytest.approx((94 + 2 * 5 + 100 * 1.25) / 1_000_000)


def test_a_version_segment_is_not_a_date_suffix():
    """`claude-opus-5-5` is its own model, not a snapshot of `claude-opus-5`."""
    assert normalize("claude-opus-5-5") == "claude-opus-5-5"
    assert normalize("claude-sonnet-5-5") == "claude-sonnet-5-5"
    assert normalize("claude-haiku-5-5") == "claude-haiku-5-5"
    assert normalize("claude-opus-5-5-20261001") == "claude-opus-5-5"


def test_opus_5_5_uses_its_own_rates_not_opus_5():
    cost = cost_usd("claude-opus-5-5", 1_000_000, 1_000_000, on_date="2026-11-12")
    assert cost == 24.0  # $4 + $20
    assert cost != cost_usd("claude-opus-5", 1_000_000, 1_000_000, on_date="2026-11-12")


def test_sonnet_5_5_and_haiku_5_5_are_priced():
    assert cost_usd("claude-sonnet-5-5", 1_000_000, 1_000_000,
                    on_date="2026-11-12") == 12.0  # $2 + $10
    assert cost_usd("claude-haiku-5-5", 1_000_000, 1_000_000,
                    on_date="2026-11-12") == pytest.approx(0.60)  # $0.10 + $0.50


def test_fable_and_mythos_versions_are_priced_separately():
    assert cost_usd("claude-fable-5-2", 1_000_000, 1_000_000, on_date="2026-11-12") == 48.0
    assert cost_usd("claude-mythos-5-2", 1_000_000, 1_000_000, on_date="2026-11-12") == 48.0
    assert cost_usd("claude-fable-5-1", 1_000_000, 1_000_000, on_date="2026-11-12") == 60.0
    assert cost_usd("claude-mythos-5-1", 1_000_000, 1_000_000, on_date="2026-11-12") == 60.0
    assert cost_usd("claude-fable-5", 1_000_000, 1_000_000, on_date="2026-11-12") == 60.0


def test_cache_read_multiplier_is_per_model():
    """Cache hits: 0.1x by default, 0.05x on Opus/Sonnet 5.5, 0.025x on Fable 5.1+."""
    one_m = dict(input_tokens=0, output_tokens=0, cache_read_tokens=1_000_000,
                 on_date="2026-11-12")
    assert cost_usd("claude-opus-5", **one_m) == pytest.approx(0.50)
    assert cost_usd("claude-opus-5-5", **one_m) == pytest.approx(0.20)
    assert cost_usd("claude-sonnet-5-5", **one_m) == pytest.approx(0.10)
    assert cost_usd("claude-haiku-5-5", **one_m) == pytest.approx(0.01)
    assert cost_usd("claude-fable-5-2", **one_m) == pytest.approx(0.20)
    assert cost_usd("claude-fable-5-1", **one_m) == pytest.approx(0.25)
    assert cost_usd("claude-fable-5", **one_m) == pytest.approx(1.00)


def test_a_dated_snapshot_still_resolves():
    assert normalize("claude-haiku-4-5-20251001") == "claude-haiku-4-5"
    assert cost_usd("claude-haiku-4-5-20251001", 1_000_000, 0,
                    on_date="2026-11-12") == 1.0


def test_an_unknown_model_with_version_segments_is_never_priced():
    assert normalize("claude-foo-9-9") == "claude-foo-9-9"
    assert cost_usd("claude-foo-9-9", 1_000_000, 1_000_000) is None
    assert cost_usd("claude-opus-9-9", 1_000_000, 1_000_000) is None
    assert cost_usd("claude-opus-5-5-foo", 1_000_000, 1_000_000) is None


def test_platform_aliases_resolve_to_the_exact_version():
    assert normalize("anthropic.claude-opus-5-5") == "claude-opus-5-5"
    assert normalize("global.anthropic.claude-fable-5-2") == "claude-fable-5-2"
    assert normalize("us.anthropic.claude-haiku-4-5-20251001-v1:0") == "claude-haiku-4-5"
    assert normalize("claude-haiku-4-5@20251001") == "claude-haiku-4-5"
    assert normalize("claude-opus-5-5@20261001") == "claude-opus-5-5"
    assert normalize("anthropic/claude-opus-5.5") == "claude-opus-5-5"
    assert normalize("anthropic/claude-sonnet-5.5:thinking") == "claude-sonnet-5-5"
    assert normalize("anthropic/claude-haiku-5.5") == "claude-haiku-5-5"


def test_verified_date_is_iso_and_not_in_the_future():
    from datetime import date
    assert date.fromisoformat(pricing.VERIFIED_ON) <= date.today()
