import csv
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import zipfile

import pytest

from tokenmeter.backups import create_backup, inspect_backup, restore_backup
from tokenmeter.config import Config
from tokenmeter.exports import export_usage
from tokenmeter.instance import InstanceLock
from tokenmeter.models import UsageEvent
from tokenmeter.store import Store
from tests.test_dashboard import app_without_tk


def add(store, key, **kwargs):
    values = dict(ts=time.time()-60, provider="anthropic", model="claude-opus-5",
                  input_tokens=100, output_tokens=20, dedupe_key=key, source="transcript")
    values.update(kwargs)
    store.add_event(UsageEvent(**values))


def test_backup_roundtrip_with_wal_and_previous_backup(tmp_path, monkeypatch):
    home = tmp_path/"home"
    home.mkdir()
    monkeypatch.setenv("TOKENMETER_HOME",str(home))
    cfg = Config(session_budget_tokens=12345)
    cfg.save()
    s = Store(home/"usage.db")
    add(s,"a")
    s.set_state("test","saved")
    bundle = create_backup(s,cfg,tmp_path/"saved.zip")
    assert inspect_backup(bundle)["calls"] == 1
    add(s,"b")
    s.close()
    previous = restore_backup(bundle,home)
    assert inspect_backup(previous)["calls"] == 2
    assert Config.load().session_budget_tokens == 12345
    s = Store(home/"usage.db")
    assert s.totals_since(0).calls == 1 and s.get_state("test") == "saved"
    s.close()


def test_corrupt_backup_never_changes_live_files(tmp_path, monkeypatch):
    home = tmp_path/"home"
    home.mkdir()
    monkeypatch.setenv("TOKENMETER_HOME",str(home))
    cfg = Config()
    cfg.save()
    s = Store(home/"usage.db")
    add(s,"a")
    bundle = create_backup(s,cfg,tmp_path/"saved.zip")
    s.close()
    with zipfile.ZipFile(bundle) as archive:
        items = {n: archive.read(n) for n in archive.namelist()}
    items["usage.db"] = b"not SQLite"
    with zipfile.ZipFile(bundle,"w") as archive:
        for name, data in items.items():
            archive.writestr(name,data)
    before = (home/"usage.db").read_bytes()
    with pytest.raises(ValueError):
        restore_backup(bundle,home)
    assert (home/"usage.db").read_bytes() == before


def test_backup_rejects_paths_and_unsupported_members(tmp_path):
    bundle = tmp_path/"bad.zip"
    with zipfile.ZipFile(bundle,"w") as archive:
        archive.writestr("../escape",b"bad")
    with pytest.raises(ValueError):
        inspect_backup(bundle)
    assert not (tmp_path/"escape").exists()


@pytest.mark.parametrize("filename", ["config.json", "usage.db", "usage.db-wal", "usage.db-shm",
                                     "instance.lock", "tokenmeter.log", "unused/../CONFIG.JSON"])
def test_backup_cannot_replace_internal_files(tmp_path, filename):
    cfg = Config()
    config_path = tmp_path / "config.json"
    config_path.write_text('{"session_budget_tokens": 12345}', encoding="utf-8")
    store = Store(tmp_path / "usage.db")
    add(store, "kept")
    before = config_path.read_bytes()
    try:
        with pytest.raises(ValueError, match="internal data"):
            create_backup(store, cfg, tmp_path / filename)
        assert config_path.read_bytes() == before
        assert store.totals_since(0).calls == 1
    finally:
        store.close()


@pytest.mark.parametrize("format", ["json", "csv"])
def test_export_cannot_replace_configuration(tmp_path, monkeypatch, format):
    import tokenmeter.app as app_module

    app = app_without_tk(tmp_path)
    app.root = object()
    add(app.store, "kept")
    target = tmp_path / "config.json"
    target.write_text('{"session_budget_tokens": 12345}', encoding="utf-8")
    before = target.read_bytes()
    errors = []
    monkeypatch.setattr(app_module.filedialog, "asksaveasfilename", lambda **_: str(target))
    monkeypatch.setattr(app_module.messagebox, "showerror", lambda *args, **_: errors.append(args))
    try:
        app._export({"action": f"export_{format}", "days": 7})
        assert len(errors) == 1 and "internal data" in errors[0][1]
        assert target.read_bytes() == before
        assert app.store.totals_since(0).calls == 1
    finally:
        app.store.close()


def test_restore_rollback_if_second_file_replacement_fails(tmp_path,monkeypatch):
    home = tmp_path/"home"
    home.mkdir()
    monkeypatch.setenv("TOKENMETER_HOME",str(home))
    cfg = Config()
    cfg.save()
    s = Store(home/"usage.db")
    add(s,"a")
    bundle = create_backup(s,cfg,tmp_path/"saved.zip")
    add(s,"b")
    s.close()
    original_replace = os.replace
    def fail_config(src,dst):
        if Path(dst) == home/"config.json":
            raise PermissionError("simulated")
        return original_replace(src,dst)
    monkeypatch.setattr("tokenmeter.backups.os.replace",fail_config)
    with pytest.raises(PermissionError):
        restore_backup(bundle,home)
    s = Store(home/"usage.db")
    assert s.totals_since(0).calls == 2
    s.close()


def test_export_filters_csv_safety_and_json_original_values(tmp_path):
    app = app_without_tk(tmp_path)
    add(app.store,"a",model="=HYPERLINK(1)")
    add(app.store,"b",provider="groq",source="proxy")
    data,content_type,filename = export_usage(app,7,provider="anthropic",format="csv")
    rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
    assert len(rows) == 1 and rows[0]["model"].startswith("'=")
    assert rows[0]["coverage"] == "partial" and filename.endswith(".csv")
    data,_,_ = export_usage(app,7,source="transcript",format="json")
    payload = json.loads(data)
    assert payload["events"][0]["model"] == "=HYPERLINK(1)"
    assert payload["summary"]["calls"] == len(payload["events"]) == 1
    assert "dedupe_key" not in payload["events"][0]
    app.store.close()


def test_instance_lock_excludes_other_process_and_releases(tmp_path):
    code = ("from pathlib import Path; from tokenmeter.instance import InstanceLock,InstanceBusy;"
            "import sys;\ntry:\n with InstanceLock(Path(sys.argv[1])): pass\n"
            "except InstanceBusy: sys.exit(7)")
    def child():
        return subprocess.run([sys.executable,"-c",code,str(tmp_path)],timeout=15,capture_output=True)
    with InstanceLock(tmp_path):
        assert child().returncode == 7
    result = child()
    assert result.returncode == 0, result.stderr.decode(errors="replace")


def test_cli_maintenance_is_rejected_when_app_has_lock(tmp_path,monkeypatch):
    from tokenmeter.__main__ import main
    monkeypatch.setenv("TOKENMETER_HOME",str(tmp_path))
    with InstanceLock(tmp_path):
        assert main(["--rescan"]) == 1
        assert main(["--start-fresh"]) == 1
    assert not (tmp_path/"usage.db").exists()


def test_shutdown_requests_the_close_and_the_loop_handles_it(tmp_path, monkeypatch):
    """The installer closes with `--shutdown` instead of killing the app mid-write."""
    import tokenmeter.__main__ as cli
    from tokenmeter.app import App

    monkeypatch.setenv("TOKENMETER_HOME", str(tmp_path))
    assert cli.main(["--shutdown"]) == 0
    assert (tmp_path / "quit.request").exists()

    app = App.__new__(App)
    app._running = True
    closed = []
    app._quit = lambda: (closed.append(True), setattr(app, "_running", False))

    class Root:
        def after(self, *_a):
            return "id"

    app.root = Root()
    app._tick()
    assert closed == [True]
    assert not (tmp_path / "quit.request").exists()
