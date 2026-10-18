"""Entry point: `python -m tokenmeter`."""

from __future__ import annotations

import argparse
import logging
import sys

from .app import App
from .config import Config, data_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tokenmeter", description=__doc__)
    parser.add_argument("--debug", action="store_true", help="verbose console logging")
    parser.add_argument("--no-proxy", action="store_true", help="do not open the local proxy")
    parser.add_argument("--no-transcripts", action="store_true",
                        help="do not read the Claude Code transcripts")
    parser.add_argument("--no-codex", action="store_true",
                        help="do not read the Codex sessions")
    parser.add_argument("--print-config", action="store_true",
                        help="show the effective configuration and exit")
    parser.add_argument("--rescan", action="store_true",
                        help="reprocess the transcripts from scratch and exit "
                             "(needed after changing what is extracted from each line)")
    parser.add_argument("--start-fresh", action="store_true",
                        help="back up, then delete events, calibrations and the "
                             "weekly cycle; the history starts today (close the app first)")
    parser.add_argument("--backup", metavar="FILE.zip", help="create a backup while the app is closed")
    parser.add_argument("--restore", metavar="FILE.zip", help="restore a backup while the app is closed")
    parser.add_argument("--inspect-backup", metavar="FILE.zip", help="validate and show the contents of a backup")
    parser.add_argument("--shutdown", action="store_true", help="request an orderly shutdown of TokenMeter")
    # Internal use: the child process that draws the web window (see ui/window.py).
    parser.add_argument("--window", metavar="URL", help=argparse.SUPPRESS)
    parser.add_argument("--parent-pid", type=int, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.window:
        from .ui.window import run_window

        return run_window(args.window, args.parent_pid)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if args.shutdown:
        home = data_dir()
        home.mkdir(parents=True, exist_ok=True)
        (home / "quit.request").write_text("quit", encoding="ascii")
        return 0
    if args.inspect_backup:
        from .backups import inspect_backup
        print(inspect_backup(args.inspect_backup))
        return 0
    from .instance import InstanceBusy, InstanceLock
    try:
        with InstanceLock(data_dir()):
            return _run(args)
    except InstanceBusy as exc:
        logging.getLogger("tokenmeter").warning("%s", exc)
        if getattr(sys, "frozen", False):
            from tkinter import messagebox
            messagebox.showinfo("TokenMeter", str(exc))
        return 1


def _run(args) -> int:
    config = Config.load()
    if args.no_proxy:
        config.proxy_enabled = False
    if args.no_transcripts:
        config.transcripts_enabled = False
    if args.no_codex:
        config.codex_enabled = False

    if args.print_config:
        print(f"config      : {Config.path()}")
        print(f"data        : {data_dir()}")
        print(f"budget      : {config.session_budget_tokens:,} tokens (basis '{config.percent_basis}')")
        print(f"window      : {config.session_window_hours:g} h")
        print(f"proxy       : {'yes' if config.proxy_enabled else 'no'} "
              f"http://{config.proxy_host}:{config.proxy_port}")
        print(f"transcripts : {'yes' if config.transcripts_enabled else 'no'} "
              f"{config.transcripts_path()}")
        return 0

    if args.rescan:
        return rescan(config)

    if args.start_fresh:
        return start_fresh(config)

    if args.restore:
        from .backups import restore_backup
        print("Previous backup:", restore_backup(args.restore, data_dir()))
        return 0
    if args.backup:
        from .backups import create_backup
        from .store import Store
        store = Store(data_dir() / "usage.db")
        try:
            print(create_backup(store, config, args.backup))
        finally:
            store.close()
        return 0

    (data_dir() / "quit.request").unlink(missing_ok=True)
    app = App(config)
    if config.proxy_enabled:
        for provider in sorted(config.upstreams):
            logging.getLogger("tokenmeter").debug(
                "%s → %s", app_base_url(config, provider), config.upstreams[provider]
            )
    app.run()
    return 0


def rescan(config: Config) -> int:
    """Reprocesses the transcripts from scratch. Close the app before using it."""
    import time

    from .collectors.transcripts import TranscriptCollector
    from .store import Store

    store = Store(data_dir() / "usage.db", retain_days=config.retain_days)
    before = store.totals_since(0)
    deleted = store.reset_transcript_data()
    print(f"transcript events deleted : {deleted:,}")

    started = time.perf_counter()
    collector = TranscriptCollector(store, config.transcripts_path(),
                                    overrides=config.billing_overrides)
    new = collector.scan_once()
    print(f"reprocessed               : {new:,} in {time.perf_counter() - started:.2f}s")

    after = store.totals_since(0)
    print()
    print(f"calls             : {before.calls:,} → {after.calls:,}")
    print(f"cache writes      : {before.cache_write_tokens:,} → {after.cache_write_tokens:,}")
    print(f"  with 1 h TTL    : {before.cache_write_1h_tokens:,} → "
          f"{after.cache_write_1h_tokens:,}")
    store.close()
    return 0


# What a calibration or an anchor sets. These go back to their defaults; the
# rest of the configuration (proxy, upstreams, thresholds) belongs to the user.
CALIBRATION_FIELDS = (
    "session_budget_tokens", "percent_basis", "calibrated_at",
    "calibrated_from_percent", "calibrated_tokens", "weekly_budget_tokens",
    "weekly_calibrated_at", "weekly_calibrated_from_percent",
    "calibrated_session_start", "calibrated_unseen_tokens", "calibrated_scope",
)


def start_fresh(config: Config, now: float | None = None) -> int:
    """Deletes everything measured and calibrated; the history starts today at 00:00.

    Before touching anything it copies the database and the configuration to
    `backup-<date>`. The transcripts already on disk are marked as read and only
    their events from today are kept: otherwise, on startup the app would load
    the ~30 days Claude Code keeps again and the wipe would be pointless.
    """
    import shutil
    import time
    from dataclasses import fields
    from datetime import date, datetime

    from .collectors.transcripts import TranscriptCollector
    from .history import local_midnight
    from .store import Store

    now = time.time() if now is None else now
    home = data_dir()
    backup = home / f"backup-{datetime.fromtimestamp(now):%Y-%m-%d-%H%M%S}"
    backup.mkdir(parents=True, exist_ok=False)
    if (home / "usage.db").exists():
        original = Store(home / "usage.db")
        try:
            original.backup_to(backup / "usage.db")
        finally:
            original.close()
    for filename in ("config.json",):
        source = home / filename
        if source.exists():
            shutil.copy2(source, backup / filename)
    print(f"backup      : {backup}")

    for filename in ("usage.db", "usage.db-wal", "usage.db-shm"):
        try:
            (home / filename).unlink(missing_ok=True)
        except PermissionError:
            print("the database is in use: close TokenMeter (tray icon → Quit) and try again")
            return 1

    defaults = {f.name: f.default for f in fields(Config) if f.name in CALIBRATION_FIELDS}
    for name, value in defaults.items():
        setattr(config, name, value)
    config.save()

    today = local_midnight(date.fromtimestamp(now))
    store = Store(home / "usage.db", retain_days=config.retain_days)
    store.set_coverage_floor(today)
    read = 0
    if config.transcripts_enabled:
        read = TranscriptCollector(store, config.transcripts_path(),
                                     overrides=config.billing_overrides).scan_once()
    if config.codex_enabled:
        # Codex sessions are also marked as read.
        from .collectors.codex import CodexCollector

        read += CodexCollector(store, config.codex_path()).scan_once()
    with store._lock:  # noqa: SLF001 - maintenance operation, no public API
        deleted = store._conn.execute(  # noqa: SLF001
            "DELETE FROM usage_events WHERE ts < ?", (today,)).rowcount
        store._conn.commit()  # noqa: SLF001
    kept = store.totals_since(today).calls
    store.close()

    print(f"transcripts read : {read:,} events, {deleted:,} from before today discarded")
    print(f"kept from today  : {kept:,} calls")
    print("calibration      : cleared; calibrate again from the menu")
    return 0


def app_base_url(config: Config, provider: str) -> str:
    return f"http://{config.proxy_host}:{config.proxy_port}/{provider}"


if __name__ == "__main__":
    sys.exit(main())
