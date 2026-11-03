"""XDG separation, override compatibility and cross-directory restore."""
import json
from pathlib import Path
import pytest
from tokenmeter import config
from tokenmeter.config import Config, data_dir, config_dir, state_dir
from tokenmeter.backups import create_backup, restore_backup
from tokenmeter.files import output_destination
from tokenmeter.models import UsageEvent
from tokenmeter.store import Store


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    monkeypatch.delenv("TOKENMETER_HOME", raising=False)
    monkeypatch.setattr(config.sys, "platform", "linux")
    for name in ("DATA", "CONFIG", "STATE"):
        monkeypatch.setenv("XDG_"+name+"_HOME", str(tmp_path/name.lower()))
    return tmp_path


def test_xdg_paths_are_distinct_and_relative_values_ignored(xdg, monkeypatch):
    assert data_dir() == xdg/"data/tokenmeter"
    assert Config.path() == xdg/"config/tokenmeter/config.json"
    assert state_dir() == xdg/"state/tokenmeter"
    monkeypatch.setenv("XDG_DATA_HOME", "relative")
    assert data_dir() == Path.home()/".local/share/tokenmeter"


def test_override_groups_all_paths(xdg, monkeypatch):
    monkeypatch.setenv("TOKENMETER_HOME", str(xdg/"isolated"))
    assert data_dir() == config_dir() == state_dir() == xdg/"isolated"


def test_split_config_and_logs_are_protected(xdg):
    for path in (Config.path(), state_dir()/"tokenmeter.log"):
        with pytest.raises(ValueError, match="internal data"):
            output_destination(path, data_dir()/"usage.db")


def test_restore_uses_xdg_config_and_preserves_previous_config(xdg):
    cfg = Config(session_budget_tokens=1234)
    cfg.save()
    store = Store(data_dir()/"usage.db")
    archive = create_backup(store, cfg, xdg/"snapshot.zip")
    cfg.session_budget_tokens = 5678
    cfg.save()
    store.close()
    previous = restore_backup(archive, data_dir())
    assert previous.exists()
    assert Config.load().session_budget_tokens == 1234
    assert not (data_dir()/"config.json").exists()
    assert not list(config_dir().glob("restore-config-*"))


def test_config_swap_failure_rolls_back_both_directories(xdg, monkeypatch):
    from tokenmeter import backups
    cfg = Config(session_budget_tokens=1234)
    cfg.save()
    store = Store(data_dir()/"usage.db")
    archive = create_backup(store, cfg, xdg/"snapshot.zip")
    cfg.session_budget_tokens = 5678
    cfg.save()
    store.add_event(UsageEvent(ts=1, provider="anthropic", model="synthetic", input_tokens=1, output_tokens=2))
    store.close()
    original = backups.os.replace
    def fail(source, target):
        if Path(target) == Config.path():
            raise OSError("synthetic config swap failure")
        return original(source, target)
    monkeypatch.setattr(backups.os, "replace", fail)
    with pytest.raises(OSError):
        restore_backup(archive, data_dir())
    assert Config.load().session_budget_tokens == 5678
    restored = Store(data_dir()/"usage.db")
    try:
        assert restored.totals_since(0).calls == 1
    finally:
        restored.close()
