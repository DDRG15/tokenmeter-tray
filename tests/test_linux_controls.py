"""Calibration commands and desktop notification lifecycle."""
from __future__ import annotations
import importlib
import pytest
from tests.linux_files_app import make_runtime
from tokenmeter import operations
from tokenmeter.config import Config
from tokenmeter.ui.notifications import DesktopNotifier


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENMETER_HOME", str(tmp_path))
    app=make_runtime(tmp_path)
    try:
        yield app
    finally:
        app.shutdown()


def test_calibration_preserves_claude_scope_and_tracks_drift(runtime):
    assert runtime.snapshot().used_tokens == 120
    operations.calibrate_session(runtime,20)
    assert runtime.config.session_budget_tokens == 600
    assert runtime.config.is_calibrated
    assert runtime.config.calibrated_scope == 'claude'
    assert len(runtime.store.calibrations()) == 1
    operations.calibrate_session(runtime,25)
    assert len(runtime.store.calibrations()) == 2


def test_manual_budget_invalidates_only_session_calibration(runtime):
    operations.calibrate_session(runtime,20)
    runtime.config.weekly_calibrated_at=1
    operations.set_budget(runtime,2000)
    assert not runtime.config.is_calibrated
    assert runtime.config.weekly_is_calibrated
    assert Config.load().session_budget_tokens == 2000


@pytest.mark.parametrize('value',[0,-1,True,1000000001])
def test_bad_budget_does_not_change_configuration(runtime,value):
    before=Config.path().read_bytes()
    with pytest.raises(ValueError):
        operations.set_budget(runtime,value)
    assert Config.path().read_bytes() == before


def test_notification_uses_existing_service_and_recalls_only_its_ids(monkeypatch):
    pytest.importorskip('gi')
    GLib=importlib.import_module('gi.repository.GLib')
    Gio=importlib.import_module('gi.repository.Gio')
    monkeypatch.setenv('DBUS_SESSION_BUS_ADDRESS','test-only-no-real-connection')
    calls=[]
    class Bus:
        def call_sync(self,*args):
            calls.append(args)
            return GLib.Variant('(u)',(42,)) if args[3]=='Notify' else None
    notifier=DesktopNotifier()
    notifier._bus=Bus()
    assert notifier.notify('synthetic title','synthetic usage')
    notifier.close()
    assert [c[3] for c in calls] == ['Notify','CloseNotification']
    assert all(c[6] == Gio.DBusCallFlags.NO_AUTO_START for c in calls)
    assert calls[1][4].unpack() == (42,)
    assert not notifier._ids


def test_no_bus_never_activates_notifications(monkeypatch):
    monkeypatch.delenv('DBUS_SESSION_BUS_ADDRESS',raising=False)
    notifier=DesktopNotifier()
    assert notifier.notify('synthetic','synthetic') is False
    assert notifier._bus is None
    notifier.close()
