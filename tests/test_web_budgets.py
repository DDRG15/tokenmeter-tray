"""Personal budgets in the Windows window: the local endpoints and the real page."""
from __future__ import annotations

import json
import time

import pytest

from tests.test_dashboard import app_without_tk, fetch, server  # noqa: F401 - pytest fixture
from tokenmeter import budgets
from tokenmeter.models import UsageEvent
from tokenmeter.ui import webserver

FORM = {"name": "Daily tokens", "metric": "tokens", "token_basis": "io", "period": "day",
        "scope_kind": "all", "scope_provider": "", "scope_model": "", "limit": "1000",
        "thresholds": "60, 100"}


def post(srv, path, payload):
    status, body = fetch(srv, path, body=json.dumps(payload).encode())
    return status, json.loads(body)


def listing(srv):
    status, body = fetch(srv, "/api/budgets")
    assert status == 200
    return json.loads(body)


def use(app, tokens, key="a"):
    app.store.add_event(UsageEvent(ts=time.time() - 60, provider="anthropic", model="claude-opus-5",
                                   input_tokens=tokens, output_tokens=0, dedupe_key=key))


# -- endpoints ----------------------------------------------------------------


def test_the_budget_endpoints_need_the_token_like_every_other(server):
    srv, _ = server
    assert fetch(srv, "/api/budgets", token=False)[0] == 403
    for path in ("/api/budgets", "/api/budgets/toggle", "/api/budgets/delete"):
        assert fetch(srv, path, token=False, body=b"{}")[0] == 403
    assert fetch(srv, "/api/budgets", host="evil.example")[0] == 403


def test_a_budget_is_created_listed_with_its_period_and_opened_for_editing(server):
    srv, app = server
    use(app, 700)
    empty = listing(srv)
    assert empty["budgets"] == [] and empty["defaults"]["metric"] == "money"

    status, answer = post(srv, "/api/budgets", {"form": FORM})
    assert status == 200 and answer["ok"] is True
    (item,) = listing(srv)["budgets"]
    assert (item["id"], item["name"], item["summary"], item["state"]) == (
        answer["id"], "Daily tokens", "700 of 1,000 · 70%", "warning")
    assert item["form"] == FORM, "what the editor shows is what was typed"


def test_a_refused_form_says_why_and_stores_nothing(server):
    """The page shows the reason; the queue of actions could not return one."""
    srv, _ = server
    for change, reason in (({"limit": "lots"}, "not a number of tokens"),
                           ({"name": ""}, "needs a name"),
                           ({"thresholds": "85, 60"}, "thresholds"),
                           ({"scope_kind": "provider"}, "scope")):
        status, answer = post(srv, "/api/budgets", {"form": {**FORM, **change}})
        assert status == 400 and answer["ok"] is False and reason in answer["error"], change
    assert listing(srv)["budgets"] == []


@pytest.mark.parametrize("payload", [
    {}, {"form": "x"}, {"form": []}, {"form": {**FORM, "extra": "1"}},
    {"form": {**FORM, "limit": 5}}, {"form": {**FORM, "name": "x" * 201}}, [],
])
def test_a_request_that_is_not_the_form_is_refused(server, payload):
    srv, _ = server
    status, answer = post(srv, "/api/budgets", payload)
    assert status == 400 and answer["ok"] is False
    assert listing(srv)["budgets"] == []


def test_broken_json_is_refused(server):
    srv, _ = server
    assert fetch(srv, "/api/budgets", body=b"{not json")[0] == 400


def test_editing_keeps_the_budget_and_the_notices_already_given(server):
    srv, app = server
    use(app, 700)
    _status, created = post(srv, "/api/budgets", {"form": FORM})
    (stored,) = budgets.load_budgets(app.store)[0]
    assert budgets.due_alert(app.store, budgets.evaluate(app.store, stored)).threshold == 60

    status, answer = post(srv, "/api/budgets", {"id": created["id"],
                                                "form": {**FORM, "name": "Renamed", "limit": "1100"}})
    assert status == 200 and answer["id"] == created["id"]
    (edited,) = budgets.load_budgets(app.store)[0]
    assert (edited.name, edited.limit) == ("Renamed", 1100)
    # 700 of 1100 is still past 60%: already announced for this period.
    assert budgets.due_alert(app.store, budgets.evaluate(app.store, edited)) is None


def test_notices_are_switched_off_and_on_and_a_budget_is_deleted(server):
    srv, app = server
    use(app, 10)
    _status, created = post(srv, "/api/budgets", {"form": FORM})
    assert post(srv, "/api/budgets/toggle", {"id": created["id"]}) == (200, {"ok": True, "enabled": False})
    assert listing(srv)["budgets"][0]["enabled"] is False
    assert post(srv, "/api/budgets/toggle", {"id": created["id"]})[1]["enabled"] is True
    assert post(srv, "/api/budgets/delete", {"id": created["id"]}) == (200, {"ok": True})
    assert listing(srv)["budgets"] == []
    assert app.store.totals_since(0).calls == 1, "deleting a budget never touches the usage"


@pytest.mark.parametrize("path", ["/api/budgets/toggle", "/api/budgets/delete"])
@pytest.mark.parametrize("payload", [{}, {"id": "nope"}, {"id": 5}, {"id": None}, []])
def test_an_unknown_budget_is_a_404_not_a_silent_success(server, path, payload):
    srv, _ = server
    status, answer = post(srv, path, payload)
    assert status == 404 and answer["ok"] is False


def test_editing_a_budget_that_was_deleted_meanwhile_does_not_recreate_it(server):
    srv, _ = server
    status, answer = post(srv, "/api/budgets", {"id": "gone", "form": FORM})
    assert status == 404 and "no longer exists" in answer["error"]
    assert listing(srv)["budgets"] == []


def test_an_unreadable_stored_budget_is_counted_for_the_page(server):
    srv, app = server
    app.store.save_budget_row({**budgets.budget_from_form(FORM).to_row(), "limit_value": 0})
    data = listing(srv)
    assert data["budgets"] == [] and data["invalid"] == 1


# -- the page -----------------------------------------------------------------


@pytest.fixture
def page_with_app(tmp_path, chromium_browser):
    app = app_without_tk(tmp_path)
    use(app, 700)
    srv = webserver.DashboardServer(app)
    try:
        srv.start()
        assert srv.wait_ready(5)
        context = chromium_browser.new_context(viewport={"width": 440, "height": 1200})
        try:
            page = context.new_page()
            try:
                errors: list[str] = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.on("console", lambda m: m.type == "error" and "400" not in m.text
                        and errors.append(m.text))
                page.goto(srv.url)
                page.wait_for_selector("#budgetCard:not([hidden])", timeout=15_000)
                yield page, app, errors
            finally:
                page.goto("about:blank", timeout=5000)
        finally:
            context.close()
    finally:
        srv.stop()
        app.store.close()


def test_the_page_adds_edits_silences_and_deletes_a_budget(page_with_app):
    page, app, errors = page_with_app
    assert "No budgets yet" in page.inner_text("#bgNote")

    # Add. The fields the chosen measure and scope do not use are hidden.
    page.click("#bgAdd")
    assert page.locator("#bgBasisWrap").is_hidden() and page.locator("#bgProviderWrap").is_hidden()
    page.fill("#bgName", "Daily tokens")
    page.select_option("#bgMetric", "tokens")
    assert page.locator("#bgBasisWrap").is_visible()
    assert "tokens" in page.inner_text("#bgLimitLbl")
    page.select_option("#bgBasis", "io")
    page.select_option("#bgPeriod", "day")

    # A limit that is not a number is refused with the reason, and the form stays.
    page.fill("#bgLimit", "lots")
    page.click("#bgSave")
    page.wait_for_function("document.querySelector('#bgErr').textContent.includes('not a number of tokens')")
    assert page.locator("#bgForm").is_visible() and budgets.load_budgets(app.store)[0] == []

    page.fill("#bgLimit", "1k")
    page.click("#bgSave")
    page.wait_for_selector(".bg-row")
    assert page.locator("#bgForm").is_hidden()
    row = page.locator(".bg-row")
    assert row.locator(".bg-name").inner_text() == "Daily tokens"
    assert row.locator(".bg-use").inner_text() == "▲ 700 of 1,000 · 70%"
    assert row.locator(".bg-detail").inner_text() == "tokens (input + output) · all usage · this day"
    assert row.get_attribute("data-level") == "warn"
    assert row.locator(".track > i").evaluate("e => e.style.width") == "70%"

    # Edit: the editor opens with what was saved, and the same budget changes.
    (before,) = budgets.load_budgets(app.store)[0]
    row.get_by_role("button", name="Edit").click()
    assert page.input_value("#bgName") == "Daily tokens" and page.input_value("#bgLimit") == "1000"
    page.fill("#bgLimit", "500")
    page.click("#bgSave")
    page.wait_for_function("document.querySelector('.bg-use').textContent.includes('of 500')")
    assert page.locator(".bg-use").inner_text() == "■ 700 of 500 · 140%"
    assert page.locator(".bg-row").get_attribute("data-level") == "danger"
    assert page.locator(".bg-row .track > i").evaluate("e => e.style.width") == "100%", "the bar never overflows"
    (after,) = budgets.load_budgets(app.store)[0]
    assert (after.id, after.limit) == (before.id, 500)

    # Notices off and on.
    page.get_by_role("button", name="Turn notices off").click()
    page.wait_for_function("document.querySelector('.bg-name').textContent.includes('(notices off)')")
    page.get_by_role("button", name="Turn notices on").click()
    page.wait_for_function("!document.querySelector('.bg-name').textContent.includes('(notices off)')")

    # Delete asks for a second click.
    page.get_by_role("button", name="Delete", exact=True).click()
    assert len(budgets.load_budgets(app.store)[0]) == 1
    page.get_by_role("button", name="Confirm delete").click()
    page.wait_for_function("document.querySelectorAll('.bg-row').length === 0")
    assert budgets.load_budgets(app.store)[0] == []
    assert errors == []


def test_a_minimum_shows_its_reason_and_a_name_is_never_markup(page_with_app):
    page, app, errors = page_with_app
    app.store.add_event(UsageEvent(ts=time.time() - 30, provider="anthropic", model="claude-opus-5",
                                   input_tokens=0, output_tokens=0, dedupe_key="cut",
                                   usage_complete=False))
    hostile = '<img src=x onerror="window.pwned=1"> & co'
    budgets.save_budget(app.store, budgets.budget_from_form({**FORM, "name": hostile}))
    page.reload()
    page.wait_for_selector(".bg-row")
    assert page.locator(".bg-name").inner_text() == hostile
    assert page.locator(".bg-row img").count() == 0 and page.evaluate("window.pwned") is None
    assert page.locator(".bg-use").inner_text() == "▲ ≥ 700 of 1,000 · at least 70%"
    assert page.locator(".bg-cause").inner_text() == "Minimum: 1 call was cut before its usage arrived"
    assert errors == []


def test_the_scope_fields_follow_the_scope(page_with_app):
    page, app, errors = page_with_app
    page.click("#bgAdd")
    page.select_option("#bgScope", "provider")
    assert page.locator("#bgProviderWrap").is_visible() and page.locator("#bgModelWrap").is_hidden()
    page.select_option("#bgScope", "model")
    assert page.locator("#bgModelWrap").is_visible()
    page.fill("#bgName", "Opus only")
    page.fill("#bgProvider", "anthropic")
    page.fill("#bgModel", "claude-opus-5")
    page.fill("#bgLimit", "10")
    page.click("#bgSave")
    page.wait_for_selector(".bg-row")
    (stored,) = budgets.load_budgets(app.store)[0]
    assert (stored.metric, stored.scope_kind, stored.scope_provider, stored.scope_model) == (
        "money", "model", "anthropic", "claude-opus-5")
    assert "$10.00" in page.locator(".bg-use").inner_text()
    page.click("#bgAdd")
    page.click("#bgCancel")
    assert page.locator("#bgForm").is_hidden() and len(budgets.load_budgets(app.store)[0]) == 1
    assert errors == []
