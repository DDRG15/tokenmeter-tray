"""Portable SQLite and configuration backups, with validation and rollback."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import zipfile
from contextlib import closing

from dataclasses import asdict
from .config import Config
from .store import Store
from .files import output_destination

MAX_BACKUP_SIZE = 256 * 1024 * 1024
MEMBERS = {"usage.db", "config.json", "manifest.json"}


def _unpack(path: Path, folder: Path) -> tuple[Config, dict]:
    with zipfile.ZipFile(path) as archive:
        items = archive.infolist()
        if len(items) != 3 or {i.filename for i in items} != MEMBERS:
            raise ValueError("The backup does not have the expected TokenMeter format")
        if sum(i.file_size for i in items) > MAX_BACKUP_SIZE:
            raise ValueError("The backup exceeds the 256 MiB limit")
        # Never extract paths supplied by the archive.
        content = {name: archive.read(name) for name in MEMBERS}
    manifest = json.loads(content["manifest.json"])
    if not isinstance(manifest, dict) or manifest.get("format") != "tokenmeter-backup-v1":
        raise ValueError("Unrecognized backup version")
    for name in ("usage.db", "config.json"):
        if hashlib.sha256(content[name]).hexdigest() != manifest.get("sha256", {}).get(name):
            raise ValueError("The backup is damaged: " + name)
        (folder / name).write_bytes(content[name])
    cfg = Config.from_dict(json.loads(content["config.json"].decode("utf-8")), strict=True)
    # `with sqlite3.connect(...)` commits the transaction but does NOT close: on
    # Windows the temp file stayed locked and the backup failed while cleaning up.
    with closing(sqlite3.connect((folder / "usage.db").resolve().as_uri() + "?mode=ro",
                                 uri=True)) as conn:
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("The backup database fails the integrity check")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"usage_events", "state", "file_offsets", "calibrations", "coverage"} <= tables:
            raise ValueError("The database does not belong to TokenMeter")
        calls, first, last = conn.execute("SELECT COUNT(*),MIN(ts),MAX(ts) FROM usage_events").fetchone()
    from datetime import datetime
    return cfg, {"calls": calls, "created_at": manifest.get("created_at"),
                 "first_day": datetime.fromtimestamp(first).strftime("%Y-%m-%d") if first else "no events",
                 "last_day": datetime.fromtimestamp(last).strftime("%Y-%m-%d") if last else "no events"}


def inspect_backup(path: str | Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="tokenmeter-inspect-") as folder:
        _, info = _unpack(Path(path), Path(folder))
        return info


def create_backup(store: Store, config: Config, path: str | Path) -> Path:
    destination = output_destination(path, store.path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tokenmeter-backup-", dir=destination.parent) as folder:
        stage = Path(folder)
        store.backup_to(stage / "usage.db")
        cfg = json.dumps(asdict(config), ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
        Config.from_dict(json.loads(cfg), strict=True)
        (stage / "config.json").write_bytes(cfg)
        manifest = {"format": "tokenmeter-backup-v1", "created_at": time.time(),
                    "sha256": {name: hashlib.sha256((stage / name).read_bytes()).hexdigest()
                               for name in ("usage.db", "config.json")}}
        (stage / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        bundle = stage / "backup.zip"
        with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name in sorted(MEMBERS):
                archive.write(stage / name, name)
        _unpack(bundle, stage)
        os.replace(bundle, destination)
    return destination


def restore_backup(path: str | Path, home: Path) -> Path:
    """Call with instance exclusion and with every Store closed.

    Validates before writing; saves the previous state and reverts if the swap
    fails. Returns the previous backup so the operation can be undone.
    """
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="restore-", dir=home) as folder:
        stage = Path(folder)
        _unpack(Path(path), stage)
        previous = home / f"backup-before-restore-{time.time_ns()}.zip"
        # Config.load() uses TOKENMETER_HOME; read the given home explicitly.
        cfg_path = home / "config.json"
        config = Config.from_dict(json.loads(cfg_path.read_text(encoding="utf-8")), strict=True) if cfg_path.exists() else Config()
        old = Store(home / "usage.db")
        try:
            create_backup(old, config, previous)
        finally:
            old.close()
        rollback = stage / "rollback"
        rollback.mkdir()
        _unpack(previous, rollback)
        try:
            for name in ("usage.db-wal", "usage.db-shm"):
                (home / name).unlink(missing_ok=True)
            for name in ("usage.db", "config.json"):
                os.replace(stage / name, home / name)
        except BaseException:
            for name in ("usage.db", "config.json"):
                shutil.copyfile(rollback / name, home / name)
            raise
        return previous
