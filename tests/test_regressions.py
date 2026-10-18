"""Cases from a pre-release review; synthetic data and no real accounts."""

import csv
import io
import json
import time
from dataclasses import replace
from datetime import date, timedelta

import pytest

from tokenmeter.calibration import calibrate, fit_session
from tokenmeter.collectors.codex import event_from_record
from tokenmeter.collectors.parsers import UsageSink
from tokenmeter.config import Config
from tokenmeter.dashboard import _models, build_state
from tokenmeter.history import daily_series, local_midnight
from tokenmeter.models import UsageEvent
from tokenmeter.session import SessionTracker
from tokenmeter.store import Store, ModelTotals


def event(now, **kw):
    data = dict(ts=now, provider="anthropic", model="m", input_tokens=10,
                output_tokens=20, dedupe_key="req:a", billing="subscription")
    data.update(kw)
    return UsageEvent(**data)


@pytest.mark.parametrize("chunk", [1, 7, 10000])
@pytest.mark.parametrize("kind", ["response.completed", "response.incomplete"])
def test_responses_nested_usage(chunk, kind):
    sink = UsageSink("openai", path="/v1/responses", content_type="text/event-stream")
    body = ("data: " + json.dumps({"type": kind, "response": {
        "id": "resp_x", "model": "model-x", "usage": {"input_tokens": 100,
        "input_tokens_details": {"cached_tokens": 30}, "output_tokens": 25}}}) + "\n\n").encode()
    for i in range(0, len(body), chunk):
        sink.feed(body[i:i+chunk])
    result = sink.finish()
    assert (result.input_tokens, result.cache_read_tokens, result.output_tokens) == (70, 30, 25)
    assert result.model == "model-x" and result.dedupe_key == "msg:resp_x"
    assert result.usage_complete == (kind == "response.completed")


def test_only_other_provider_remains_visible(tmp_path):
    from tests.test_dashboard import app_without_tk
    app = app_without_tk(tmp_path)
    app.store.add_event(event(time.time()-60, provider="openai", source="codex"))
    state = build_state(app)
    assert not state["session"]["has_activity"]
    assert state["tokens"]["io"] == 30
    assert state["models"][0]["provider"] == "openai"
    app.store.close()


def test_web_anchor_wins_over_earlier_session(tmp_path):
    s = Store(tmp_path/"u.db")
    now = time.time()
    s.add_event(event(now-4*3600))
    tracker = SessionTracker(s, Config())
    tracker.set_reset_from_web(now+3*3600)
    result = tracker.snapshot(now)
    assert result.start_ts == now-2*3600
    assert result.used_tokens == 0
    s.close()


def test_continuous_usage_has_no_moving_lookback(tmp_path):
    s = Store(tmp_path/"u.db")
    now = time.time()
    for h in range(31):
        s.add_event(event(now-(30-h)*3600, dedupe_key=str(h)))
    tracker = SessionTracker(s, Config())
    assert tracker.anchor(now) == tracker.anchor(now+1)
    s.close()


def test_multipoint_offset_survives_restart_but_not_new_session(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENMETER_HOME", str(tmp_path))
    now = time.time()
    s = Store(tmp_path/"u.db")
    s.add_event(event(now-60, input_tokens=100000, output_tokens=0))
    cfg = Config()
    tr = SessionTracker(s, cfg)
    start = tr.anchor(now)[0]
    fit = fit_session([(50000,20),(100000,30)])
    cfg.apply_calibration(replace(calibrate(30,100000),budget_tokens=fit.budget_tokens))
    cfg.calibrated_session_start, cfg.calibrated_unseen_tokens = start, fit.unseen_tokens
    cfg.save()
    assert SessionTracker(s, Config.load()).snapshot(now).percent == 30
    s.add_event(event(now+6*3600, dedupe_key="next"))
    assert SessionTracker(s, Config.load()).snapshot(now+6*3600).estimated_unseen_tokens == 0
    s.close()


def test_basis_invalidates_both_calibrations_and_fit_filters(tmp_path):
    cfg = Config(calibrated_at=1, weekly_calibrated_at=1, calibrated_unseen_tokens=99)
    cfg.set_basis("all")
    assert not cfg.is_calibrated and not cfg.weekly_is_calibrated
    assert cfg.calibrated_unseen_tokens == 0
    s = Store(tmp_path/"u.db")
    for basis, scope in (("io","claude"),("all","claude"),("io","all")):
        s.add_calibration(ts=1,observed_percent=10,counted_tokens=100,basis=basis,
                          budget_tokens=1000,session_start_ts=1,predicted_percent=None,scope=scope)
    assert len(s.calibrations_for_session(1,"io","claude")) == 1
    s.close()


@pytest.mark.parametrize("reverse", [False, True])
def test_complete_observation_wins_without_adding_again(tmp_path, reverse):
    s = Store(tmp_path/"u.db")
    a = event(100, output_tokens=0, usage_complete=False)
    b = replace(a, output_tokens=80, usage_complete=True, source="transcript")
    for item in ([b,a] if reverse else [a,b]):
        s.add_event(item)
    totals = s.totals_since(0)
    assert totals.calls == 1 and totals.io_tokens == 90 and totals.incomplete_calls == 0
    s.close()


@pytest.mark.parametrize("reverse", [False, True])
def test_codex_proxy_aliases_and_request_id_converge(tmp_path, reverse):
    s = Store(tmp_path/"u.db")
    codex = event_from_record({"timestamp":"2026-10-17T12:00:00Z", "payload": {
        "response_id":"resp_x", "usage":{"input_tokens":100,"output_tokens":25}}},
        "model-x", "api", "")
    sink = UsageSink("openai",path="/v1/responses",content_type="application/json",request_id="req_x")
    sink.feed(json.dumps({"id":"resp_x","model":"model-x","usage":{"input_tokens":100,"output_tokens":25}}).encode())
    proxy = replace(sink.finish(ts=codex.ts),billing="api")
    for item in ([proxy,codex] if reverse else [codex,proxy]):
        s.add_event(item)
    s.close()
    s = Store(tmp_path/"u.db")
    s.add_event(proxy)
    assert s.totals_since(0).calls == 1 and s.totals_since(0).io_tokens == 125
    # The same ID under another provider name IS the same generation: the
    # upstream name is chosen by the user; real IDs from different providers
    # do not collide (msg_01…, chatcmpl-…, resp_…, gen-…).
    s.add_event(replace(proxy,provider="my-openai"))
    assert s.totals_since(0).calls == 1
    s.close()


@pytest.mark.parametrize("payload", [[], None, {"session_window_hours":"five"},
    {"upstreams":None}, {"retain_days":0}, {"proxy_port":True},
    {"weekly_window_days":float("nan")}, {"alert_thresholds":[True]},
    {"proxy_host":[]}, {"proxy_host":{}}, {"session_budget_tokens":10**400},
    {"session_window_hours":10**100}, {"session_window_hours":2**62},
    {"codex_poll_seconds":2**62}, {"calibrated_at":2**62},
    {"billing_overrides":{"anthropic":[]}}, {"upstreams":{"x":"https://u:p@example.org"}}])
def test_invalid_config_is_preserved_and_recovers(tmp_path, monkeypatch, payload):
    monkeypatch.setenv("TOKENMETER_HOME",str(tmp_path))
    raw = json.dumps(payload)
    (tmp_path/"config.json").write_text(raw,encoding="utf-8")
    cfg = Config.load()
    assert cfg.window_seconds > 0
    backups = list(tmp_path.glob("config-invalid-*.json"))
    assert backups and backups[0].read_text(encoding="utf-8") == raw
    cfg.save()
    Config.from_dict(json.loads((tmp_path/"config.json").read_text(encoding="utf-8")),strict=True)


def test_one_second_does_not_establish_zero_for_whole_day(tmp_path):
    s = Store(tmp_path/"u.db")
    day = date.today()-timedelta(days=1)
    begin = local_midnight(day)
    s.mark_covered(begin+10,begin+11,source="proxy",gap=0)
    bars = daily_series(s,2,expected_sources=("proxy",))
    assert bars[0].tokens is None and bars[0].coverage == "partial"
    s.mark_covered(begin,local_midnight(day+timedelta(days=1)),source="proxy",gap=0)
    assert daily_series(s,2,expected_sources=("proxy",))[0].tokens == 0
    assert daily_series(s,2,expected_sources=("proxy","codex"))[0].tokens is None
    s.close()


def test_retention_applies_to_import_after_prune(tmp_path):
    s = Store(tmp_path/"u.db")
    s.prune(90)
    assert not s.add_event(event(time.time()-100*86400))
    assert s.add_event(event(time.time()))
    s.close()


def test_unknown_billing_cannot_be_a_certain_api_zero():
    _, costs, _ = _models([ModelTotals(model="claude-opus-5",input_tokens=1000,calls=1)])
    assert costs["api_spend"]["value"] is None
    assert costs["api_spend"]["partial"] is True


def test_rescan_respects_overrides(tmp_path, monkeypatch):
    from tokenmeter.__main__ import rescan
    monkeypatch.setenv("TOKENMETER_HOME",str(tmp_path))
    monkeypatch.setattr("tokenmeter.collectors.transcripts.CachedBilling.get",lambda self:("unknown",""))
    root = tmp_path/"projects"
    root.mkdir()
    (root/"a.jsonl").write_text(json.dumps({"type":"assistant","requestId":"x",
        "message":{"model":"m","usage":{"input_tokens":10,"output_tokens":2}}})+"\n",encoding="utf-8")
    assert rescan(Config(transcripts_root=str(root),billing_overrides={"anthropic":"api"})) == 0
    s = Store(tmp_path/"usage.db")
    assert s.totals_by_model(0)[0].billing == "api"
    s.close()


@pytest.mark.parametrize("order", ["transcript_first", "proxy_first"])
def test_upstream_with_a_free_name_does_not_duplicate_the_call(tmp_path, order):
    """Identity (`req:`) is global; `my-claude` and `anthropic` are the same call.

    Regression from looking aliases up per provider: the proxy records under the
    name the user gave the upstream and the transcript under `anthropic`.
    """
    s = Store(tmp_path / "u.db")
    now = time.time()
    t = event(now, provider="anthropic", source="transcript", dedupe_key="req:req_011ABC")
    p = event(now, provider="my-claude", source="proxy", dedupe_key="req:req_011ABC")
    first, second = (t, p) if order == "transcript_first" else (p, t)
    assert s.add_event(first) is True
    assert s.add_event(second) is False
    assert s.totals_since(0).calls == 1
    s.close()
