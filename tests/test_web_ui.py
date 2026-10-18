"""The real page, in Edge, against the real server with synthetic data.

The server tests validate the JSON; these validate that the page consumes it:
no JS errors, with the filters sent to the API and with the export actions
queued with their period and filters. They are skipped if Playwright or Edge is
missing.
"""

from __future__ import annotations

import time

import pytest

from tests.test_dashboard import app_without_tk, call
from tokenmeter.models import UsageEvent
from tokenmeter.ui import webserver

playwright_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def web_page(tmp_path):
    app = app_without_tk(tmp_path)
    call(app, 50_000, "req:a")
    app.store.add_event(UsageEvent(ts=time.time() - 120, provider="openai", model="gpt-x",
                                   input_tokens=3_000, output_tokens=100, source="codex",
                                   dedupe_key="codex:r1", billing="subscription", plan="plus"))
    srv = webserver.DashboardServer(app)
    srv.start()
    assert srv.wait_ready(5)
    with playwright_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="msedge", headless=True)
        except Exception as exc:  # no Edge or no Playwright browser
            srv.stop()
            pytest.skip(f"no browser available for Playwright: {exc}")
        page = browser.new_page(viewport={"width": 440, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: m.type == "error" and errors.append(m.text))
        page.goto(srv.url)
        page.wait_for_function(
            "document.querySelector('#filterProvider').options.length > 1", timeout=15_000)
        yield page, app, errors
        # Closing with the page alive hung Edge: navigate away first.
        page.goto("about:blank")
        page.context.close()
        browser.close()
    srv.stop()


def test_loads_without_errors_and_with_real_data(web_page):
    page, _app, errors = web_page
    page.wait_for_timeout(1_500)  # a couple of /api/state polls
    assert errors == []
    options = page.eval_on_selector_all("#filterProvider option", "os => os.map(o => o.value)")
    assert {"anthropic", "openai"} <= set(options)
    assert "DEMO" not in page.inner_text("header")


def test_the_provider_filter_reaches_the_api(web_page):
    page, _app, errors = web_page
    with page.expect_request(lambda r: "/api/state" in r.url and "provider=openai" in r.url,
                             timeout=10_000):
        page.select_option("#filterProvider", "openai")
    assert errors == []


def test_export_queues_the_action_with_period_and_filters(web_page):
    page, app, errors = web_page
    page.select_option("#filterProvider", "openai")
    page.click("#tabSeg button[data-tab=history]")
    page.click("[data-action=export_csv]")
    end = time.time() + 5
    while time.time() < end and not any(isinstance(a, dict) for a in app.queued):
        page.wait_for_timeout(100)
    (action,) = [a for a in app.queued if isinstance(a, dict)]
    assert action["action"] == "export_csv" and action["provider"] == "openai"
    assert action["days"] in (7, 30, 90)
    assert errors == []


def test_export_does_not_block_if_the_foreground_bridge_does_not_respond(web_page):
    page, app, errors = web_page
    page.evaluate("""window.pywebview = {api: {
        allow_foreground: () => new Promise(() => {})
    }}""")
    page.click("#tabSeg button[data-tab=history]")
    with page.expect_response(lambda r: r.url.endswith('/api/action') and
                              r.request.method == 'POST', timeout=5000) as response:
        page.click("[data-action=export_json]")
    assert response.value.status == 200
    assert app.queued[-1]["action"] == "export_json"
    assert errors == []


def test_hiding_costs_keeps_tokens_on_both_tabs(web_page):
    page, app, errors = web_page
    app.config.show_cost = False
    page.wait_for_function("document.body.classList.contains('hide-cost')")
    assert not page.locator("#costVal").is_visible()
    assert page.locator("#used").is_visible()
    page.click("#tabSeg button[data-tab=history]")
    assert not page.locator("#hCostVal").is_visible()
    app.config.show_cost = True
    page.wait_for_function("!document.body.classList.contains('hide-cost')")
    assert page.locator("#hCostVal").is_visible()
    assert errors == []
