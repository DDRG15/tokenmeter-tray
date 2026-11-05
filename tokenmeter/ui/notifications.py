"""Desktop notifications over an existing session bus; never activate a daemon."""
from __future__ import annotations
import os


class DesktopNotifier:
    def __init__(self):
        self._bus = None
        self._ids = set()

    def _call(self, method, parameters):
        if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
            return None
        import gi
        from gi.repository import Gio
        if self._bus is None:
            self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        return self._bus.call_sync("org.freedesktop.Notifications",
            "/org/freedesktop/Notifications", "org.freedesktop.Notifications",
            method, parameters, None, Gio.DBusCallFlags.NO_AUTO_START, 500, None)

    def notify(self, title, message):
        try:
            from gi.repository import GLib
            reply = self._call("Notify", GLib.Variant("(susssasa{sv}i)",
                ("TokenMeter", 0, "", title, message, [], {}, 5000)))
            if reply is not None:
                self._ids.add(reply.unpack()[0])
                return True
        except Exception:
            # Unsupported/missing notification services must not stop metering.
            pass
        return False

    def close(self):
        if not self._ids:
            return
        try:
            from gi.repository import GLib
            for notification in self._ids:
                try:
                    self._call("CloseNotification", GLib.Variant("(u)", (notification,)))
                except Exception:
                    pass
        finally:
            self._ids.clear()
