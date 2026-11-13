"""Ambiguous local credentials must not turn a paid gateway into free usage."""

from contextlib import contextmanager
import json
import threading
import time

import pytest

from tests.test_dashboard import app_without_tk
from tests.test_proxy import ANTHROPIC_JSON, post
from tests.test_proxy import upstream  # noqa: F401 - pytest fixture
from tokenmeter import dashboard
from tokenmeter.billing import (
    UNKNOWN,
    apply_override,
    classify_request,
    is_placeholder_credential,
)
from tokenmeter.collectors.proxy import ProxyCollector
from tokenmeter.config import Config
from tokenmeter.models import UsageEvent


AMBIGUOUS_KEYS = ["sk-1234", "x", "a" * 19]
LOCAL_UPSTREAMS = [
    "http://127.0.0.1:4000",
    "http://localhost:1234",
    "http://[::1]:8080",
]


@pytest.mark.parametrize("key", AMBIGUOUS_KEYS)
def test_short_unlisted_values_are_not_known_placeholders(key):
    # A gateway can use a short real secret; only explicit placeholders prove nothing is billed.
    assert not is_placeholder_credential(key)


@pytest.mark.parametrize("key", AMBIGUOUS_KEYS)
@pytest.mark.parametrize("url", LOCAL_UPSTREAMS)
@pytest.mark.parametrize("carrier", ["authorization", "x-api-key", "api-key", "x-goog-api-key", "query"])
def test_short_unlisted_local_credentials_stay_unknown(key, url, carrier):
    headers, query = {}, None
    if carrier == "authorization":
        headers["Authorization"] = f"Bearer {key}"
    elif carrier == "query":
        query = {"key": key}
    else:
        headers[carrier] = key

    # A local destination is not evidence that its upstream inference is free.
    assert classify_request(headers, url, query) == UNKNOWN


@pytest.mark.parametrize(
    "headers,query",
    [
        ({"x-api-key": "ollama", "Authorization": "Bearer sk-1234"}, None),
        ({"x-api-key": "sk-1234"}, {"key": "ollama"}),
    ],
)
def test_a_known_placeholder_does_not_hide_an_ambiguous_local_credential(headers, query):
    # Every credential must be harmless before a request can be classified as local.
    assert classify_request(headers, "http://localhost:4000", query) == UNKNOWN


@pytest.mark.parametrize(
    "credential,expected",
    [("sk-proj-a", "api"), ("b" * 20, "api"), ("eyJ.e30.x", "subscription")],
)
def test_a_certain_credential_resolves_an_ambiguous_local_request(credential, expected):
    headers = {"x-api-key": "sk-1234"}
    url = "http://localhost:4000"
    assert classify_request(headers, url) == UNKNOWN

    # Adding a recognizable credential supplies evidence; it must not become free or unknown.
    headers["Authorization"] = f"Bearer {credential}"
    assert classify_request(headers, url) == (expected, "")


def test_a_short_key_is_only_ambiguous_for_a_local_destination():
    headers = {"Authorization": "Bearer sk-1234"}
    assert classify_request(headers, "http://127.0.0.1:4000") == UNKNOWN
    # A remote key keeps the existing API classification.
    assert classify_request(headers, "https://api.openai.com") == ("api", "")


def test_existing_config_override_can_resolve_a_local_key_but_not_a_certain_api_key(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("TOKENMETER_HOME", str(tmp_path))
    cfg = Config(
        upstreams={"gateway": "http://127.0.0.1:4000"},
        billing_overrides={"gateway": "local"},
    )
    cfg.save()
    loaded = Config.load()
    inferred = classify_request({"Authorization": "Bearer sk-1234"}, loaded.upstreams["gateway"])
    assert inferred == UNKNOWN
    assert apply_override("gateway", inferred, {}) == UNKNOWN
    assert apply_override("gateway", inferred, loaded.billing_overrides) == ("local", "")

    # An explicit preference only fills unknown billing; it cannot hide a recognized API key.
    certain = classify_request({"Authorization": "Bearer sk-proj-a"}, loaded.upstreams["gateway"])
    assert apply_override("gateway", certain, loaded.billing_overrides) == ("api", "")


@pytest.mark.parametrize("invalid", [{"gateway": "free"}, {"gateway": []}, ["local"]])
def test_invalid_billing_override_recovers_without_making_an_ambiguous_gateway_free(
    tmp_path, monkeypatch, invalid
):
    monkeypatch.setenv("TOKENMETER_HOME", str(tmp_path))
    payload = {
        "session_budget_tokens": 12345,
        "upstreams": {"gateway": "http://127.0.0.1:4000"},
        "billing_overrides": invalid,
    }
    raw = json.dumps(payload)
    (tmp_path / "config.json").write_text(raw, encoding="utf-8")
    loaded = Config.load()
    assert loaded.session_budget_tokens == 12345
    assert loaded.billing_overrides == {}
    backups = list(tmp_path.glob("config-invalid-*.json"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == raw

    # Recovering invalid settings must not silently certify a short-key gateway as free.
    inferred = classify_request({"Authorization": "Bearer sk-1234"}, loaded.upstreams["gateway"])
    assert apply_override("gateway", inferred, loaded.billing_overrides) == UNKNOWN


@contextmanager
def running_gateway(app, cfg, monkeypatch):
    recorded = threading.Event()
    original_add = app.store.add_event

    def add_and_signal(event):
        try:
            return original_add(event)
        finally:
            recorded.set()

    monkeypatch.setattr(app.store, "add_event", add_and_signal)
    collector = ProxyCollector(
        app.store, "127.0.0.1", 0, cfg.upstreams, billing_overrides=cfg.billing_overrides
    )
    app.proxy = collector
    try:
        collector.start()
        assert collector.wait_ready(5), collector.bind_error
        yield collector, recorded
    finally:
        collector.stop()
        assert not collector.running


def test_proxy_and_dashboard_show_partial_until_local_billing_is_explicit(
    tmp_path, monkeypatch, upstream
):
    monkeypatch.setenv("TOKENMETER_HOME", str(tmp_path / "config"))
    monkeypatch.setitem(ANTHROPIC_JSON, "model", "synthetic-local-model")
    host, port = upstream.server_address

    # Compare automatic inference and the existing explicit per-provider override end to end.
    for explicit in (False, True):
        cfg = Config(
            upstreams={"gateway": f"http://{host}:{port}"},
            billing_overrides={"gateway": "local"} if explicit else {},
        )
        cfg.save()
        cfg = Config.load()
        app = app_without_tk(tmp_path / ("explicit" if explicit else "automatic"))
        try:
            with running_gateway(app, cfg, monkeypatch) as (collector, recorded):
                response, _ = post(
                    collector, "/gateway/v1/messages", headers={"Authorization": "Bearer sk-1234"}
                )
                assert response.status == 200
                assert recorded.wait(5), "the proxy did not persist its synthetic response"

                # A priced API contribution makes a partial subtotal visible instead of just a dash.
                app.store.add_event(UsageEvent(
                    ts=time.time() - 60, provider="anthropic", model="claude-opus-5",
                    input_tokens=1000, output_tokens=0, source="proxy", billing="api",
                    dedupe_key="req:known-api",
                ))
                state = dashboard.build_state(app)
                (gateway,) = [row for row in state["models"] if row["model"] == "synthetic-local-model"]
                assert gateway["billing"] == ("local" if explicit else "unknown")
                assert state["tokens"]["calls"] == 2
                if explicit:
                    assert gateway["cost"] == 0
                    assert gateway["cost_text"] == "local"
                    assert state["cost"]["partial"] is False
                    assert state["cost"]["api_spend"]["partial"] is False
                else:
                    assert gateway["cost"] is None
                    assert state["cost"]["value"] > 0
                    assert state["cost"]["partial"] is True
                    assert state["cost"]["text"].endswith(" (partial)")
                    assert state["cost"]["api_spend"]["partial"] is True
                    assert state["cost"]["api_spend"]["text"].endswith(" (partial)")
                    assert any(row["kind"] == "unknown" for row in state["billing"])
        finally:
            app.store.close()


def test_ambiguous_local_billing_leaves_the_api_equivalent_exact_but_the_spend_partial(tmp_path):
    app = app_without_tk(tmp_path)
    try:
        inferred = classify_request(
            {"Authorization": "Bearer sk-1234"}, "http://127.0.0.1:4000"
        )
        assert inferred == UNKNOWN
        app.store.add_event(UsageEvent(
            ts=time.time() - 60, provider="gateway", model="claude-opus-5",
            input_tokens=1000, output_tokens=0, source="proxy", billing=inferred[0],
            dedupe_key="req:ambiguous-priced-model",
        ))
        state = dashboard.build_state(app)
        # The overall amount is the API equivalent: what this work costs at the
        # model's published rate, whoever paid. A known tariff makes it exact.
        # "(partial)" means "at least this much", so it must not flag doubt about
        # the payer. That doubt belongs to the split below and to the note.
        assert state["cost"]["value"] > 0
        assert state["cost"]["partial"] is False
        assert not state["cost"]["text"].endswith(" (partial)")
        assert "unknown billing" in state["cost"]["note"]
        assert state["cost"]["api_spend"]["partial"] is True
        assert state["cost"]["subscription_equivalent"]["partial"] is True
    finally:
        app.store.close()
