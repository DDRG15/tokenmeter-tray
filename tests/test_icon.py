from tokenmeter.session import Snapshot
from tokenmeter.store import Totals
from tokenmeter.ui.icon import (
    DANGER,
    OK,
    TEXT_ON_DARK,
    TEXT_ON_LIGHT,
    WARN,
    render_icon,
    state_color,
    tooltip,
)


def test_state_color_thresholds():
    assert state_color(0) == OK
    assert state_color(59.9) == OK
    assert state_color(60) == WARN
    assert state_color(84.9) == WARN
    assert state_color(85) == DANGER
    assert state_color(150) == DANGER


def test_custom_thresholds_are_respected():
    assert state_color(50, warn=40, danger=80) == WARN
    assert state_color(50, warn=90, danger=95) == OK


def corner_and_center_pixels(img):
    w, _ = img.size
    return img.getpixel((w // 2, w // 2))


def test_text_flips_with_the_taskbar_theme():
    """Fixed white text disappears on the light taskbar."""
    dark = render_icon(50, size=64, light_taskbar=False)
    light = render_icon(50, size=64, light_taskbar=True)
    assert dark.getpixel((32, 32)) != light.getpixel((32, 32))

    # The icon's center falls inside the glyph or the gap; the set of colors
    # present is compared instead, which does tell both themes apart.
    def has_near(img, target, tol=40):
        return any(
            all(abs(px[i] - target[i]) <= tol for i in range(3)) and px[3] > 200
            for px in img.getdata()
        )

    assert has_near(dark, TEXT_ON_DARK), "on a dark taskbar the number is light"
    assert has_near(light, TEXT_ON_LIGHT), "on a light taskbar the number is dark"
    assert not has_near(light, TEXT_ON_DARK, tol=10), "no white text must remain"


def test_icon_is_transparent_outside_the_ring():
    img = render_icon(50, size=64, light_taskbar=False)
    assert img.getpixel((0, 0))[3] == 0, "the corner must be transparent"


def test_sizes_render_without_error():
    for size in (16, 20, 24, 32, 64):
        img = render_icon(37, size=size)
        assert img.size == (size, size)
        assert img.mode == "RGBA"


def test_idle_icon_differs_from_zero_percent():
    idle = render_icon(0, size=64, has_activity=False)
    active = render_icon(0, size=64, has_activity=True)
    assert list(idle.getdata()) != list(active.getdata())


def snapshot(percent=35.0, has_activity=True, inp=1234, out=5678):
    totals = Totals(input_tokens=inp, output_tokens=out, calls=3)
    return Snapshot(
        now_ts=0.0, start_ts=0.0, end_ts=18000.0, remaining_seconds=8340.0,
        totals=totals, used_tokens=inp + out, budget_tokens=100000,
        percent=percent, percent_basis="io", has_activity=has_activity,
    )


def test_tooltip_is_compact_and_within_the_windows_limit():
    text = tooltip(snapshot())
    assert text == "In 1.2k · Out 5.7k · 35% · resets in 2h 19m"
    assert len(text) <= 127, "Windows truncates the tooltip to 127 characters"


def test_tooltip_scales_units():
    assert "Out 1.5M" in tooltip(snapshot(out=1_500_000))
    assert "In 999" in tooltip(snapshot(inp=999))


def test_tooltip_when_there_is_no_session():
    assert tooltip(snapshot(has_activity=False)) == "TokenMeter — no active session"
