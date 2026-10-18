"""Tray icon (pystray) on its own thread.

Menu callbacks run on the pystray thread and **cannot** touch Tk widgets:
they only enqueue actions that the main thread drains.
"""

from __future__ import annotations

import logging
import threading

import pystray
from pystray import MenuItem as Item

from .icon import render_icon, taskbar_is_light, tooltip

log = logging.getLogger(__name__)

ICON_SIZE = 64


class Tray:
    def __init__(self, app) -> None:
        self._app = app
        self._icon: pystray.Icon | None = None
        self._thread: threading.Thread | None = None
        self._last_key: tuple | None = None

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        self._icon = pystray.Icon(
            "tokenmeter",
            icon=render_icon(0, size=ICON_SIZE, has_activity=False),
            title="TokenMeter",
            menu=self._menu(),
        )
        self._thread = threading.Thread(target=self._icon.run, name="tray", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:  # pragma: no cover - depends on the backend
                log.debug("failed to stop the icon", exc_info=True)

    # -- menu --------------------------------------------------------------

    def _menu(self) -> pystray.Menu:
        enqueue = self._app.enqueue
        return pystray.Menu(
            Item("Open TokenMeter", lambda: enqueue("open_dashboard"), default=True),
            Item("History…", lambda: enqueue("open_history")),
            Item("Compact panel", lambda: enqueue("toggle_panel")),
            pystray.Menu.SEPARATOR,
            Item("Reset session now", lambda: enqueue("reset_session")),
            pystray.Menu.SEPARATOR,
            Item("Calibrate with claude.ai…", lambda: enqueue("calibrate")),
            Item("Weekly window…", lambda: enqueue("weekly")),
            Item("Manual budget…", lambda: enqueue("edit_budget")),
            Item(
                "% basis: include cache",
                lambda: enqueue("toggle_basis"),
                checked=lambda _i: self._app.config.percent_basis == "all",
            ),
            pystray.Menu.SEPARATOR,
            Item(
                "Alerts at 60 / 85 / 100%",
                lambda: enqueue("toggle_alerts"),
                checked=lambda _i: self._app.config.alerts_enabled,
            ),
            Item(
                "Pause collectors",
                lambda: enqueue("toggle_pause"),
                checked=lambda _i: self._app.collectors_paused,
            ),
            Item("Open data folder", lambda: enqueue("open_data_dir")),
            Item("Create backup…", lambda: enqueue("backup")),
            Item("Restore backup…", lambda: enqueue("restore")),
            pystray.Menu.SEPARATOR,
            Item("Quit", lambda: enqueue("quit")),
        )

    # -- refresh ----------------------------------------------------------

    def update(self, snapshot) -> None:
        """Redraws only when what is visible changes.

        Regenerating the icon every second makes the Windows tray flicker, so
        the integer percentage is the invalidation key.
        """
        if self._icon is None:
            return
        cfg = self._app.config
        # The theme is part of the key: if the user switches from a dark taskbar
        # to a light one, the number has to change color even if the percentage
        # does not move.
        light = taskbar_is_light()
        key = (int(snapshot.percent), snapshot.has_activity,
               cfg.warn_percent, cfg.danger_percent, light)
        if key != self._last_key:
            self._last_key = key
            try:
                self._icon.icon = render_icon(
                    snapshot.percent,
                    size=ICON_SIZE,
                    has_activity=snapshot.has_activity,
                    warn=cfg.warn_percent,
                    danger=cfg.danger_percent,
                    light_taskbar=light,
                )
            except Exception:
                log.exception("could not redraw the icon")

        # The tooltip does carry the countdown, so it is always refreshed.
        try:
            self._icon.title = tooltip(snapshot)
        except Exception:
            log.debug("could not update the tooltip", exc_info=True)

    def notify(self, title: str, message: str) -> None:
        """Windows notification balloon.

        Not every pystray backend supports it, and the user may have
        notifications turned off: a failure here must not take down the main
        loop, so it is swallowed and logged.
        """
        if self._icon is None:
            return
        try:
            self._icon.notify(message, title)
        except Exception:
            log.debug("could not notify", exc_info=True)
