"""Puts the project root on `sys.path`.

`test_integration_dedupe` imports a helper from `tests.test_proxy`, and that
only works if the root is importable no matter which directory pytest is
launched from.
"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def chromium_browser(tmp_path):
    """Private browser/profile; Linux browser launch errors fail the test."""
    from playwright.sync_api import Error, sync_playwright
    options = {"headless": True, "downloads_path": str(tmp_path / "downloads")}
    channel = os.environ.get("TOKENMETER_TEST_BROWSER_CHANNEL")
    if channel is None and sys.platform == "win32":
        channel = "msedge"
    if channel:
        options["channel"] = channel
    with sync_playwright() as driver:
        try:
            browser = driver.chromium.launch(**options)
        except Error as exc:
            if sys.platform == "win32" and channel == "msedge":
                pytest.skip(f"Edge is not available: {exc}")
            raise
        try:
            yield browser
        finally:
            browser.close()
