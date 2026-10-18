"""Width of the panel bars.

Tk cannot be brought up without a display in CI, so the pure function that
decides the width is tested — which is where the bug was.
"""

from tokenmeter.ui.panel import CONTENT_WIDTH, bar_width


class FakeCanvas:
    def __init__(self, width):
        self._width = width

    def winfo_width(self):
        return self._width


def test_an_unmapped_widget_reports_one_not_zero():
    """Tk returns 1 before laying the widget out: `or WIDTH` does not trigger with 1."""
    assert bar_width(FakeCanvas(1)) == CONTENT_WIDTH
    assert (1 or CONTENT_WIDTH) == 1, "this is why the previous code failed"


def test_zero_also_falls_back():
    assert bar_width(FakeCanvas(0)) == CONTENT_WIDTH


def test_a_real_width_is_respected():
    assert bar_width(FakeCanvas(308)) == 308


def test_the_fallback_matches_the_panel_padding():
    """The fallback width has to be the usable one, not the window's."""
    assert CONTENT_WIDTH == 340 - 2 * 16
