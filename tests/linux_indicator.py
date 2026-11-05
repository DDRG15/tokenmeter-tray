"""A test-owned StatusNotifier host for private D-Bus graphical sessions.

Without a host Ayatana falls back to XEmbed, which can reach the real X11
panel even when the session bus is private. Keep native indicator protocol
traffic inside the test bus instead. This helper never replaces an owner.
"""
from __future__ import annotations


class IndicatorHost:
    def __init__(self):
        from gi.repository import Gio, GLib

        self.GLib = GLib
        self.items = []
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        interface = Gio.DBusNodeInfo.new_for_xml("""
            <node><interface name="org.kde.StatusNotifierWatcher">
              <method name="RegisterStatusNotifierItem">
                <arg name="service" type="s" direction="in"/>
              </method>
              <property name="ProtocolVersion" type="i" access="read"/>
              <property name="IsStatusNotifierHostRegistered" type="b" access="read"/>
              <property name="RegisteredStatusNotifierItems" type="as" access="read"/>
            </interface></node>
        """).interfaces[0]
        self.registration = self.bus.register_object(
            "/StatusNotifierWatcher", interface, self._method, self._property, None)
        response = self.bus.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            "RequestName", GLib.Variant("(su)", ("org.kde.StatusNotifierWatcher", 4)),
            GLib.VariantType.new("(u)"), Gio.DBusCallFlags.NONE, 1000, None)
        if response.unpack()[0] != 1:
            # A real desktop host already owns this name. Leave it alone.
            self.bus.unregister_object(self.registration)
            self.registration = None

    def _method(self, bus, sender, path, interface, method, parameters, invocation):
        if method == "RegisterStatusNotifierItem":
            self.items.append(parameters.unpack()[0])
            invocation.return_value(self.GLib.Variant("()", ()))

    def _property(self, bus, sender, path, interface, name):
        values = {
            "ProtocolVersion": self.GLib.Variant("i", 0),
            "IsStatusNotifierHostRegistered": self.GLib.Variant("b", True),
            "RegisteredStatusNotifierItems": self.GLib.Variant("as", self.items),
        }
        return values[name]
