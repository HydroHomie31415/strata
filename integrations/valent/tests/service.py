# SPDX-License-Identifier: MIT
"""Private Valent/Strata D-Bus fixture. Records requests; never transfers files."""
import json
from pathlib import Path
import sys

from gi.repository import Gio, GLib

SERVICE = "ca.andyholmes.Valent"
ROOT = "/ca/andyholmes/Valent"
state_file, event_file, ready_file = map(Path, sys.argv[1:])
connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
connection.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                     "RequestName", GLib.Variant("(su)", (SERVICE, 0)), GLib.VariantType.new("(u)"),
                     Gio.DBusCallFlags.NONE, 1000, None)
devices, groups, menus, exports = {}, {}, {}, {}
previous = None


def record(event):
    with event_file.open("a") as stream:
        stream.write(json.dumps(event) + "\n")


def properties(device):
    return {"Name": GLib.Variant("s", device.get("name", "Phone")),
            "Id": GLib.Variant("s", "phone-id"),
            "State": GLib.Variant("u", device.get("state", 3))}


def call(connection, sender, path, interface, method, parameters, invocation):
    objects = {key: {SERVICE + ".Device": properties(device)} for key, device in devices.items()}
    invocation.return_value(GLib.Variant("(a{oa{sa{sv}}})", (objects,)))


xml = """<node><interface name="org.freedesktop.DBus.ObjectManager">
<method name="GetManagedObjects"><arg type="a{oa{sa{sv}}}" direction="out"/></method>
<signal name="InterfacesAdded"><arg type="o"/><arg type="a{sa{sv}}"/></signal>
<signal name="InterfacesRemoved"><arg type="o"/><arg type="as"/></signal>
</interface></node>"""
connection.register_object(ROOT, Gio.DBusNodeInfo.new_for_xml(xml).interfaces[0], call, None, None)


def update_menu(path):
    menu = menus[path]
    menu.remove_all()
    if devices[path].get("mounted"):
        folders = Gio.Menu()
        for name, uri in devices[path].get("folders", [["Internal storage", "sftp://phone.test:1739/storage/emulated/0"]]):
            item = Gio.MenuItem.new(name, None)
            item.set_action_and_target_value("device.sftp.open-uri", GLib.Variant("s", uri))
            folders.append_item(item)
        menu.append_submenu("Browse Folders", folders)


def activate(action, parameter, path):
    record({"device": path, "action": action.get_name(),
            "parameter": parameter.unpack() if parameter is not None else None})
    if action.get_name() == "sftp.browse":
        def mounted():
            if path in devices:
                devices[path]["mounted"] = True
                update_menu(path)
            return False
        GLib.timeout_add(devices[path].get("mount_delay_ms", 50), mounted)


def refresh():
    global previous
    wire = state_file.read_text()
    if wire == previous:
        return True
    previous = wire
    updated = json.loads(wire)
    for path in set(devices) - set(updated):
        for kind, token in exports.pop(path):
            (connection.unexport_action_group if kind == "actions" else connection.unexport_menu_model)(token)
        groups.pop(path)
        menus.pop(path)
        connection.emit_signal(None, ROOT, "org.freedesktop.DBus.ObjectManager", "InterfacesRemoved",
                               GLib.Variant("(oas)", (path, [SERVICE + ".Device"])))
    devices.clear()
    devices.update(updated)
    for path, device in devices.items():
        if path not in groups:
            group = Gio.SimpleActionGroup()
            for name, signature in (("share.uris", "as"), ("sftp.browse", None)):
                action = Gio.SimpleAction.new(name, GLib.VariantType.new(signature) if signature else None)
                action.connect("activate", activate, path)
                group.add_action(action)
            groups[path] = group
            menus[path] = Gio.Menu()
            exports[path] = [("actions", connection.export_action_group(path, group)),
                             ("menu", connection.export_menu_model(path, menus[path]))]
            connection.emit_signal(None, ROOT, "org.freedesktop.DBus.ObjectManager", "InterfacesAdded",
                                   GLib.Variant("(oa{sa{sv}})", (path, {SERVICE + ".Device": properties(device)})))
        groups[path].lookup_action("share.uris").set_enabled(device.get("share", True))
        groups[path].lookup_action("sftp.browse").set_enabled(device.get("browse", True))
        update_menu(path)
        connection.emit_signal(None, path, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                               GLib.Variant("(sa{sv}as)", (SERVICE + ".Device", properties(device), [])))
    return True


app = Gio.Application(application_id="io.github.lgse.Strata", flags=Gio.ApplicationFlags.HANDLES_OPEN)
app.connect("open", lambda app, files, n, hint: record({"open": [file.get_uri() for file in files]}))
app.register(None)
refresh()
GLib.timeout_add(50, refresh)
ready_file.write_text("ready")
GLib.MainLoop().run()
