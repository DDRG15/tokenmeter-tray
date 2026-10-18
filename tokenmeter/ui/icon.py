"""Tray icon rendering: a progress ring with the percentage inside.

It is drawn at 4× and downscaled with LANCZOS. Windows scales the icon to 16 or
20 real pixels; without supersampling the arc shows very visible steps at that
size.
"""

from __future__ import annotations

import time
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

SUPERSAMPLE = 4

OK = (46, 204, 113, 255)
WARN = (241, 196, 15, 255)
DANGER = (231, 76, 60, 255)
IDLE = (150, 150, 150, 255)

# The icon is drawn on the taskbar, not on a background of its own: the number
# and the ring's track have to contrast with it. Fixed white text would be
# invisible with the Windows light theme.
TEXT_ON_DARK = (255, 255, 255, 255)
TEXT_ON_LIGHT = (28, 30, 34, 255)
TRACK_ON_DARK = (255, 255, 255, 60)
TRACK_ON_LIGHT = (0, 0, 0, 55)

_THEME_TTL = 5.0  # the user can change the theme while the app is open
_theme_cache: tuple[float, bool] = (0.0, False)

# Windows fonts, from most to least preferred.
FONT_CANDIDATES = ("segoeuib.ttf", "seguisb.ttf", "arialbd.ttf", "segoeui.ttf")


def taskbar_is_light() -> bool:
    """Does the taskbar use the light theme?

    `SystemUsesLightTheme` is the value that controls the taskbar and the tray;
    do not confuse it with `AppsUseLightTheme`, which only affects application
    windows and may have the opposite value.
    """
    global _theme_cache
    now = time.monotonic()
    stamp, value = _theme_cache
    if now - stamp < _THEME_TTL:
        return value

    result = False
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            result = bool(winreg.QueryValueEx(key, "SystemUsesLightTheme")[0])
    except (ImportError, OSError):
        result = False  # a dark taskbar is the Windows default

    _theme_cache = (now, result)
    return result


def state_color(percent: float, warn: int = 60, danger: int = 85) -> tuple[int, int, int, int]:
    if percent >= danger:
        return DANGER
    if percent >= warn:
        return WARN
    return OK


@lru_cache(maxsize=32)
def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _label(percent: float, has_activity: bool) -> str:
    if not has_activity:
        return "–"
    value = int(percent)
    if value >= 100:
        return "!"  # at three digits the number is unreadable; the color already shouts
    return str(value)


def render_icon(
    percent: float,
    *,
    size: int = 64,
    has_activity: bool = True,
    warn: int = 60,
    danger: int = 85,
    light_taskbar: bool | None = None,
) -> Image.Image:
    if light_taskbar is None:
        light_taskbar = taskbar_is_light()
    text_color = TEXT_ON_LIGHT if light_taskbar else TEXT_ON_DARK
    track_color = TRACK_ON_LIGHT if light_taskbar else TRACK_ON_DARK

    big = size * SUPERSAMPLE
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # In the real tray the icon ends up at 16-24 px. There a thick ring leaves
    # no room for two digits, so it is thinned and the number is enlarged.
    tiny = size <= 24
    stroke = max(2, int(big * (0.10 if tiny else 0.13)))
    pad = stroke // 2 + 1
    box = (pad, pad, big - pad - 1, big - pad - 1)

    color = IDLE if not has_activity else state_color(percent, warn, danger)

    # Full track, always visible: it gives the reference for "how much is left".
    draw.arc(box, 0, 360, fill=track_color, width=stroke)

    fraction = max(0.0, min(1.0, percent / 100.0))
    if has_activity and fraction > 0:
        # -90° so it starts at the top and advances clockwise.
        draw.arc(box, -90, -90 + 360 * fraction, fill=color, width=stroke)

    text = _label(percent, has_activity)
    if len(text) < 2:
        ratio = 0.72 if tiny else 0.62
    else:
        ratio = 0.60 if tiny else 0.50
    font = _font(int(big * ratio))
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    draw.text(
        ((big - (right + left)) / 2, (big - (bottom + top)) / 2),
        text,
        font=font,
        fill=text_color if has_activity else IDLE,
    )

    return img.resize((size, size), Image.LANCZOS)


def app_icon(size: int = 256) -> Image.Image:
    """Application icon (shortcut, .exe, Alt+Tab).

    Unlike the tray one it carries no number: it is identity, not data. It is
    drawn on a rounded square with its own background so it looks the same on
    the desktop, in the explorer and on the taskbar.
    """
    big = size * SUPERSAMPLE
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    draw.rounded_rectangle(
        (0, 0, big - 1, big - 1), radius=int(big * 0.22), fill=(31, 32, 35, 255)
    )

    stroke = int(big * 0.11)
    pad = int(big * 0.22)
    box = (pad, pad, big - pad - 1, big - pad - 1)
    draw.arc(box, 0, 360, fill=(255, 255, 255, 45), width=stroke)
    draw.arc(box, -90, -90 + 360 * 0.72, fill=OK, width=stroke)

    font = _font(int(big * 0.30))
    left, top, right, bottom = draw.textbbox((0, 0), "T", font=font)
    draw.text(
        ((big - (right + left)) / 2, (big - (bottom + top)) / 2),
        "T", font=font, fill=TEXT_ON_DARK,
    )
    return img.resize((size, size), Image.LANCZOS)


def write_ico(path, sizes=(16, 24, 32, 48, 64, 128, 256)) -> None:
    """Saves the multi-resolution .ico used by the shortcut and the .exe."""
    base = app_icon(256)
    base.save(path, format="ICO", sizes=[(s, s) for s in sizes])


def tooltip(snapshot, *, app_name: str = "TokenMeter") -> str:
    """Tooltip text. Windows truncates it to 127 characters."""
    if not snapshot.has_activity:
        return f"{app_name} — no active session"
    totals = snapshot.totals
    return (
        f"In {_compact(totals.input_tokens)} · "
        f"Out {_compact(totals.output_tokens)} · "
        f"{snapshot.percent:.0f}% · "
        f"resets in {snapshot.remaining_text()}"
    )


def _compact(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)
