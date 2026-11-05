"""Bounded child fixture: native GTK with synthetic sources, never user data."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sys
import threading
import time

from tokenmeter.billing import CachedBilling
from tokenmeter.config import Config, data_dir
from tokenmeter.instance import InstanceLock
from tokenmeter.models import UsageEvent
from tokenmeter.runtime import Runtime
from tokenmeter.ui.linux import LinuxShell
from tests.linux_indicator import IndicatorHost


def main():
    output, scenario = Path(sys.argv[1]), sys.argv[2]
    home = data_dir()
    result = {"visible": False, "pause_verified": False, "reopen_verified": False}
    config = Config(proxy_port=0, upstreams={},
                    transcripts_root=str(home / "synthetic-claude"),
                    codex_root=str(home / "synthetic-codex"))
    with InstanceLock(home):
        runtime = Runtime(config, transcript_billing=CachedBilling(
            lambda: ("subscription", "synthetic")))
        runtime.store.add_event(UsageEvent(
            ts=time.time(), provider="anthropic", model="synthetic-model",
            input_tokens=1200, output_tokens=300, source="transcript",
            dedupe_key="req:gtk-test", billing="subscription"))
        indicator_host = IndicatorHost()
        shell = LinuxShell(runtime)
        if scenario == "startup-error":
            start = runtime.start
            def broken_start():
                start()
                raise RuntimeError("synthetic startup failure")
            runtime.start = broken_start

        def ready(ui):
            result["visible"] = ui.window.get_visible()
            result["menu_active"] = ui.indicator.get_status() == ui.AppIndicator.IndicatorStatus.ACTIVE
            result["summary"] = ui.summary.get_text()
            def pause():
                ui.pause_button.clicked()
                return False
            def resume():
                result["pause_verified"] = runtime.collectors_paused
                ui.pause_button.clicked()
                return False
            def reopen():
                ui.window.hide()
                ui.open_item.activate()
                result["reopen_verified"] = ui.window.get_visible()
                return False
            def finish():
                if scenario == "quit-menu":
                    ui.quit_item.activate()
                elif scenario == "shutdown-request":
                    (home / "quit.request").write_text("quit")
                elif scenario == "close-window":
                    ui.window.close()
                else:
                    os.kill(os.getpid(), signal.SIGTERM)
                return False
            for delay, callback in ((150, pause), (600, resume), (900, reopen), (1200, finish)):
                ui.GLib.timeout_add(delay, callback)
        try:
            code = shell.run(ready)
            assert scenario != "startup-error"
            assert code == 0
        except RuntimeError as exc:
            if scenario != "startup-error" or str(exc) != "synthetic startup failure":
                raise
        finally:
            runtime.shutdown()
        if scenario != "startup-error" and indicator_host.registration is not None:
            assert indicator_host.items, "Native indicator did not register on the test bus"
        result.update(closed=runtime.store.closed, port=runtime.proxy.port,
                      workers=[t.name for t in threading.enumerate()
                               if t.name in {"proxy", "transcripts", "codex"}],
                      webview_loaded=any(n == "webview" or n.startswith("webview.")
                                         for n in sys.modules))
    output.write_text(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
