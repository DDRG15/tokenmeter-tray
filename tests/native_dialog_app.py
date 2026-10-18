"""Integration process: real App, real Tk, no tray or collectors."""

import json
import os
from pathlib import Path
import sys
import time

from tokenmeter.app import App
from tokenmeter.config import Config
from tokenmeter.models import UsageEvent
from tokenmeter.ui.webserver import DashboardServer


def main():
    import logging

    # stderr goes to app.log in the test: without this the log stayed empty and
    # an intermittent failure left no trace on the server side.
    logging.basicConfig(level=logging.DEBUG, stream=sys.stderr,
                        format="%(asctime)s.%(msecs)03d %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    home = Path(sys.argv[1])
    os.environ["TOKENMETER_HOME"] = str(home)
    cfg = Config(proxy_enabled=False, transcripts_enabled=False, codex_enabled=False,
                 session_budget_tokens=100_000)
    cfg.save()
    app = App(cfg)
    app.store.add_event(UsageEvent(ts=time.time() - 60, provider="anthropic",
        model="claude-opus-5", input_tokens=12345, output_tokens=0, source="transcript",
        dedupe_key="req:native-dialog"))
    app.dashboard = DashboardServer(app)
    app.dashboard.start()
    assert app.dashboard.wait_ready(10)
    (home / "ready.json").write_text(json.dumps({"url": app.dashboard.url,
                                                "pid": os.getpid()}), encoding="utf-8")

    def tick():
        if (home / "stop").exists():
            app._quit()
            return
        if (home / "add-event").exists():
            (home / "add-event").unlink()
            app.store.add_event(UsageEvent(ts=time.time(), provider="anthropic",
                model="claude-opus-5", input_tokens=99, output_tokens=0,
                source="transcript", dedupe_key="req:after-backup"))
            (home / "added-event").touch()
        app._drain_actions()
        if app._running:
            app.root.after(100, tick)

    app.root.after(100, tick)
    try:
        app.root.mainloop()
    finally:
        app.shutdown()
        app.root.destroy()


if __name__ == "__main__":
    main()
