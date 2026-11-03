"""Filtered exports, protected files and native backup dialogs on Linux."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

from tests.linux_files_app import make_runtime
from tokenmeter.backups import create_backup, inspect_backup, restore_backup
from tokenmeter.operations import save_export
from tokenmeter.store import Store

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENMETER_HOME", str(tmp_path))
    app = make_runtime(tmp_path)
    try:
        yield app
    finally:
        app.shutdown()


def test_filtered_export_matches_shared_history(runtime, tmp_path):
    path = save_export(runtime, tmp_path / "usage.json", 7, source="codex")
    result = json.loads(path.read_text())
    assert result["filters"] == {"provider": "", "source": "codex"}
    assert result["summary"]["calls"] == 1
    assert len(result["events"]) == 1 and result["events"][0]["provider"] == "openai"
    assert not list(tmp_path.glob("export-*.tmp"))


@pytest.mark.parametrize("name", ["config.json", "usage.db", "usage.db-wal", "quit.request"])
def test_export_cannot_overwrite_live_state(runtime, tmp_path, name):
    path = tmp_path / name
    before = path.read_bytes() if path.exists() else None
    with pytest.raises(ValueError, match="internal data"):
        save_export(runtime, path)
    assert (path.read_bytes() if path.exists() else None) == before
    assert runtime.store.totals_since(0).calls == 2


def test_failed_export_preserves_original_file_and_removes_temp(runtime, tmp_path, monkeypatch):
    path = tmp_path / "export.json"
    path.write_bytes(b"original")
    def fail(*_):
        raise OSError("synthetic replacement failure")
    monkeypatch.setattr("tokenmeter.operations.os.replace", fail)
    with pytest.raises(OSError, match="synthetic"):
        save_export(runtime, path)
    assert path.read_bytes() == b"original"
    assert not list(tmp_path.glob("export-*.tmp"))


def test_backup_and_restore_preserve_both_snapshots(runtime, tmp_path):
    backup = create_backup(runtime.store, runtime.config, tmp_path / "snapshot.zip")
    assert inspect_backup(backup)["calls"] == 2
    runtime.store._conn.execute("DELETE FROM usage_events")
    runtime.store._conn.commit()
    runtime.shutdown()
    previous = restore_backup(backup, tmp_path)
    assert inspect_backup(previous)["calls"] == 0
    store = Store(tmp_path / "usage.db")
    try:
        assert store.totals_since(0).calls == 2
        assert store._conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        store.close()


@pytest.mark.skipif(sys.platform != "linux" or not os.environ.get("DISPLAY"),
                    reason="requires a Linux graphical test session")
@pytest.mark.parametrize("scenario", ["roundtrip", "cancel-export", "protected-export", "invalid-backup", "dialog-sigterm", "restore-failure", "cancel-restore", "settings", "cancel-settings", "invalid-settings"])
def test_native_history_and_file_dialogs_are_isolated_and_close(tmp_path, scenario):
    for name in ("data", "cache", "config", "share", "state", "tmp"):
        (tmp_path / name).mkdir()
    env = dict(os.environ, TOKENMETER_HOME=str(tmp_path / "data"),
               XDG_CACHE_HOME=str(tmp_path / "cache"), XDG_CONFIG_HOME=str(tmp_path / "config"),
               XDG_DATA_HOME=str(tmp_path / "share"), XDG_STATE_HOME=str(tmp_path / "state"),
               TMPDIR=str(tmp_path / "tmp"), PYTHONDONTWRITEBYTECODE="1",
               GSETTINGS_BACKEND="memory", GIO_USE_VFS="local", NO_AT_BRIDGE="1")
    output = tmp_path / "result.json"
    with (tmp_path / "child.log").open("w") as log:
        process = subprocess.Popen([sys.executable, "-B", "-m", "tests.linux_files_app", str(output), scenario],
                                   cwd=ROOT, env=env, stdout=log, stderr=log, start_new_session=True)
        try:
            code = process.wait(timeout=20)
        finally:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
    logs = (tmp_path / "child.log").read_text()
    assert code == 0, logs
    result = json.loads(output.read_text())
    assert result["closed"] and not result["workers"] and not result["webview_loaded"]
    assert result["filtered_calls"] == 1 and result["filtered_tokens"] == 70
    assert result["cost_hidden"] and result["rows"] == 7
    assert result["codex_limits_visible"] and result["unknown_calls_hidden"]
    assert result["chart_ready"] and result["notifier_closed"]
    if "settings" in scenario:
        assert result["settings_verified"]
    assert not list((tmp_path / "data").glob("tray-*"))
    assert "Traceback" not in logs and "CRITICAL" not in logs
    if scenario == "roundtrip":
        assert result["restored_calls"] == 2 and result["previous_calls"] == 3
        assert result["integrity"] == "ok"

    if scenario == "restore-failure":
        assert result["shell_exit_code"] == 1
        assert "Restore failed" in result["dialog_titles"]
        assert result["restored_calls"] == 2
    if scenario == "cancel-restore":
        assert result["shell_exit_code"] == 0 and result["restored_calls"] == 2
