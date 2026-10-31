#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Valent file sharing and SFTP browsing for Strata's provider protocol."""

from collections import Counter, OrderedDict
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import time
import unicodedata
from urllib.parse import urlsplit

SERVICE = "ca.andyholmes.Valent"
ROOT = "/ca/andyholmes/Valent"
STRATA = "io.github.lgse.Strata"
MAX_FRAME = 1024 * 1024
REQUEST_SECONDS = 5.0
MESSAGES = json.loads(Path(__file__).with_name("messages.json").read_text(encoding="utf-8"))


def tr(message, **values):
    language = next((os.environ[k] for k in ("LC_ALL", "LC_MESSAGES", "LANG") if os.environ.get(k)), "en")
    language = language.split(".")[0].split("@")[0].replace("_", "-")
    language = {"pt": "pt-BR", "zh": "zh-CN", "zh-HK": "zh-TW", "zh-MO": "zh-TW"}.get(language, language)
    messages = MESSAGES.get(language, MESSAGES.get(language.split("-")[0], MESSAGES["en"]))
    return messages.get(message, message).format(**values)


class Unavailable(Exception):
    """An action is known not to have been submitted."""


class Uncertain(Exception):
    """A submission may have reached Valent. Never retry automatically."""


@dataclass(frozen=True)
class Device:
    name: str
    share: str | None = "share.uris"
    browse: bool = True


@dataclass(frozen=True)
class Snapshot:
    owner: str
    devices: dict


@dataclass
class Ticket:
    token: str
    owner: str
    device: str
    kind: str
    uri: str
    paths: tuple
    background: bool
    identities: tuple
    expires: float


def label(text, limit=90):
    clean = "".join(c for c in text if unicodedata.category(c) != "Cc").strip()
    return (clean or tr("Device")).encode("utf-8")[:limit].decode("utf-8", "ignore")


def action_id(kind, device, uri=""):
    return kind + "-" + hashlib.sha256((device + "\0" + uri).encode()).hexdigest()[:48]


def file_identities(paths):
    result = []
    for path in paths:
        s = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(s.st_mode):
            raise ValueError(tr("Select regular files, without folders or symbolic links."))
        result.append((s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns))
    return tuple(result)


def remote_uri(uri):
    if not isinstance(uri, str) or len(uri.encode("utf-8")) > 16384 or any(ord(c) < 32 or ord(c) == 127 for c in uri):
        return False
    try:
        parsed = urlsplit(uri)
        return (parsed.scheme == "sftp" and bool(parsed.hostname)
                and parsed.password is None and not parsed.query and not parsed.fragment
                and (parsed.port is None or 1 <= parsed.port <= 65535))
    except ValueError:
        return False


def menu_locations(model, variant_type, depth=0, budget=None, retained=None):
    """Only use SFTP targets advertised by this exact device's exported menu."""
    if depth > 4:
        return []
    budget = [256] if budget is None else budget
    result = []
    for index in range(min(model.get_n_items(), budget[0])):
        if budget[0] <= 0:
            break
        budget[0] -= 1
        action = model.get_item_attribute_value(index, "action", variant_type("s"))
        target = model.get_item_attribute_value(index, "target", variant_type("s"))
        name = model.get_item_attribute_value(index, "label", variant_type("s"))
        if (action is not None and action.unpack() == "device.sftp.open-uri"
                and target is not None and remote_uri(target.unpack())):
            result.append((label(name.unpack()) if name is not None else tr("Device"), target.unpack()))
        for link in ("section", "submenu"):
            child = model.get_item_link(index, link)
            if child is not None:
                if retained is not None:
                    retained.append(child)
                result.extend(menu_locations(child, variant_type, depth + 1, budget, retained))
    return list(dict.fromkeys(result))


class Valent:
    def __init__(self, changed=lambda: None, open_destination=None):
        from gi.repository import Gio, GLib

        self.Gio, self.GLib = Gio, GLib
        self.changed = changed
        self.connection = None
        self.models = {}
        self.links = {}
        self.pending = {}
        self.open_destination = open_destination

    def connect(self):
        if self.connection is not None and not self.connection.is_closed():
            return self.connection
        self.connection = self.Gio.bus_get_sync(self.Gio.BusType.SESSION, None)
        self.models.clear()
        self.links.clear()
        self.connection.signal_subscribe(
            SERVICE, None, None, None, None, self.Gio.DBusSignalFlags.NONE, self.service_signal,
        )
        self.connection.signal_subscribe(
            "org.freedesktop.DBus", "org.freedesktop.DBus", "NameOwnerChanged",
            "/org/freedesktop/DBus", SERVICE, self.Gio.DBusSignalFlags.NONE,
            self.owner_changed,
        )
        return self.connection

    def owner_changed(self, *args):
        self.models.clear()
        self.links.clear()
        self.changed()

    def service_signal(self, connection, sender, path, interface, member, parameters):
        if member in {"InterfacesAdded", "InterfacesRemoved", "PropertiesChanged", "Changed"}:
            self.changed()

    def call(self, owner, path, interface, method, args, result_type, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Unavailable(tr("Valent did not respond in time."))
        return self.connect().call_sync(
            owner, path, interface, method, args, self.GLib.VariantType.new(result_type),
            self.Gio.DBusCallFlags.NO_AUTO_START, max(1, min(1500, int(remaining * 1000))), None,
        ).unpack()

    def owner(self, service, deadline):
        return self.call(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "GetNameOwner",
            self.GLib.Variant("(s)", (service,)), "(s)", deadline,
        )[0]

    def discover(self, deadline):
        try:
            owner = self.owner(SERVICE, deadline)
            objects = self.call(
                owner, ROOT, "org.freedesktop.DBus.ObjectManager", "GetManagedObjects", None,
                "(a{oa{sa{sv}}})", deadline,
            )[0]
            if len(objects) > 63:
                raise Unavailable(tr("Too many devices to fit in Strata's device menu."))
            devices = {}
            for path, interfaces in objects.items():
                props = interfaces.get(SERVICE + ".Device", {})
                state = props.get("State", 0)
                if (not path.startswith(ROOT + "/Device/") or not isinstance(state, int)
                        or state & 3 != 3 or not isinstance(props.get("Name"), str)):
                    continue
                actions = self.call(
                    owner, path, "org.gtk.Actions", "DescribeAll", None, "(a{s(bgav)})", deadline,
                )[0]
                share = next((name for name in ("share.uris", "share.files")
                              if name in actions and actions[name][0] and actions[name][1] == "as"), None)
                browse = "sftp.browse" in actions and actions["sftp.browse"][:2] == (True, "")
                if share or browse:
                    devices[path] = Device(props["Name"], share, browse)
            self.models = {key: model for key, model in self.models.items()
                           if key[0] == owner and key[1] in devices}
            self.links = {key: links for key, links in self.links.items() if key in self.models}
            return Snapshot(owner, devices)
        except self.GLib.Error as error:
            raise Unavailable(tr("Valent is unavailable or the device list changed.")) from error

    def activate(self, owner, path, action, parameter, deadline):
        try:
            self.call(
                owner, path, "org.gtk.Actions", "Activate",
                self.GLib.Variant("(sava{sv})", (action, [] if parameter is None else [parameter], {})),
                "()", deadline,
            )
        except self.GLib.Error as error:
            remote = self.Gio.DBusError.get_remote_error(error)
            if remote in {
                "org.freedesktop.DBus.Error.UnknownMethod", "org.freedesktop.DBus.Error.UnknownObject",
                "org.freedesktop.DBus.Error.UnknownInterface", "org.freedesktop.DBus.Error.ServiceUnknown",
                "org.freedesktop.DBus.Error.NameHasNoOwner", "org.freedesktop.DBus.Error.InvalidArgs",
                "org.freedesktop.DBus.Error.AccessDenied",
            }:
                raise Unavailable(tr("The device is unavailable or file sharing was disabled.")) from error
            raise Uncertain(tr("Valent did not confirm the submission. Check your device before retrying.")) from error

    def submit(self, snapshot, device, uris, deadline):
        self.activate(snapshot.owner, device, snapshot.devices[device].share,
                      self.GLib.Variant("as", uris), deadline)

    def locations(self, owner, device):
        key = (owner, device)
        if key not in self.models:
            self.models[key] = self.Gio.DBusMenuModel.get(self.connect(), owner, device)
        # Child D-Bus menu models load asynchronously and unsubscribe when their
        # last reference goes away. Keep them alive between main-loop turns.
        links = []
        result = menu_locations(self.models[key], self.GLib.VariantType.new, retained=links)
        self.links[key] = links
        return result

    def open_location(self, uri, deadline):
        if self.open_destination is not None:
            owner, path = self.open_destination
            self.call(owner, path, STRATA + ".ValentSidebar", "Open",
                      self.GLib.Variant("(s)", (uri,)), "()", deadline)
            return
        self.call(
            self.owner(STRATA, deadline), "/io/github/lgse/Strata", "org.freedesktop.Application", "Open",
            self.GLib.Variant("(asa{sv})", ([uri], {})), "()", deadline,
        )

    def browse(self, snapshot, device, uri, deadline):
        if uri:
            if uri not in [target for _, target in self.locations(snapshot.owner, device)]:
                raise Unavailable(tr("The phone folder is no longer available. Reopen the menu."))
            try:
                self.open_location(uri, deadline)
            except self.GLib.Error as error:
                raise Unavailable(tr("Could not open phone files in Strata.")) from error
            return
        key = (snapshot.owner, device)
        if key in self.pending:
            return
        self.locations(*key)
        self.activate(*key, "sftp.browse", None, deadline)
        # Mounting is asynchronous in Valent. Keep the provider request short,
        # then wait for this device's exported SFTP targets without retrying mount.
        expires = time.monotonic() + 30

        def ready():
            try:
                current = self.discover(time.monotonic() + 2)
                if (current.owner != snapshot.owner or device not in current.devices
                        or not current.devices[device].browse):
                    raise Unavailable(tr("The selected device disconnected or file sharing was disabled."))
                locations = self.locations(*key)
                if locations:
                    self.open_location(locations[0][1], time.monotonic() + 2)
                    self.pending.pop(key, None)
                    return False
                if time.monotonic() < expires:
                    return True
                raise Unavailable(tr("Phone files did not become available. Check Valent and the phone's file-sharing permissions."))
            except (Unavailable, self.GLib.Error) as error:
                self.pending.pop(key, None)
                self.notify(str(error) if isinstance(error, Unavailable) else tr("Could not open phone files in Strata."))
                return False

        self.pending[key] = self.GLib.timeout_add(250, ready)

    def notify(self, message):
        self.connect().call(
            "org.freedesktop.Notifications", "/org/freedesktop/Notifications", "org.freedesktop.Notifications",
            "Notify", self.GLib.Variant("(susssasa{sv}i)", (
                "Valent", 0, "folder-remote-symbolic", tr("Could not open phone files in Strata."),
                message, [], {}, -1,
            )), None, self.Gio.DBusCallFlags.NO_AUTO_START, 1500, None, None, None,
        )


class Provider:
    def __init__(self, backend, clock=time.monotonic):
        self.backend = backend
        self.clock = clock
        self.revision = 0
        self.tickets = OrderedDict()

    def invalidate(self):
        self.revision += 1
        self.tickets.clear()
        return {"version": 1, "event": "invalidate", "revision": self.revision}

    def ticket(self, snapshot, device, kind, uri, paths, background, identities):
        now = self.clock()
        key = (snapshot.owner, device, kind, uri, tuple(paths), background)
        ticket = self.tickets.get(key)
        if ticket is None or ticket.identities != identities or ticket.expires < now:
            ticket = Ticket(secrets.token_hex(24), snapshot.owner, device, kind, uri,
                            tuple(paths), background, identities, now + 120)
            self.tickets[key] = ticket
        self.tickets.move_to_end(key)
        while len(self.tickets) > 256:
            self.tickets.popitem(last=False)
        return ticket

    def menu(self, paths, background, deadline):
        try:
            snapshot = self.backend.discover(deadline)
        except Unavailable:
            return []
        names = {device: label(info.name) for device, info in snapshot.devices.items()}
        counts = Counter(names.values())
        try:
            identities = () if background else file_identities(paths)
        except (OSError, ValueError):
            identities = ()
        actions, browsing = [], []

        def leaf(device, kind, name, uri=""):
            ticket = self.ticket(snapshot, device, kind, uri, paths, background,
                                 identities if kind == "send" else ())
            return {"id": action_id(kind, device, uri), "label": name, "context": ticket.token}

        for device in sorted(names, key=lambda d: (names[d].casefold(), d)):
            name = names[device]
            if counts[name] > 1:
                name += " (" + hashlib.sha256(device.encode()).hexdigest()[:6] + ")"
            info = snapshot.devices[device]
            if identities and info.share:
                actions.append({**leaf(device, "send", name), "placement": "send-to"})
            if info.browse:
                locations = self.backend.locations(snapshot.owner, device)
                if locations:
                    browsing.append({"id": action_id("folders", device), "label": name,
                                     "children": [leaf(device, "browse", folder, uri)
                                                  for folder, uri in locations]})
                else:
                    browsing.append(leaf(device, "browse", name))
        if browsing:
            actions.append({"id": "browse", "label": tr("Browse phone files"), "children": browsing})

        def nodes(items):
            return sum(1 + nodes(item.get("children", [])) for item in items)

        return actions if nodes(actions) <= 64 else []

    def activate(self, request, deadline):
        paths, total = request["paths"], len(request["paths"])

        def result(status, message):
            return {"message": message, "outcome": {
                "status": status, "accepted": total if status == "accepted" else 0, "total": total,
            }, **({"feedback": "notice"} if status == "accepted" else {})}

        ticket_key = next((key for key, value in self.tickets.items()
                           if value.token == request.get("context")), None)
        ticket = self.tickets.pop(ticket_key, None)
        if (ticket is None or tuple(paths) != ticket.paths or request["background"] != ticket.background
                or request.get("action") != action_id(ticket.kind, ticket.device, ticket.uri)
                or ticket.expires < self.clock()):
            return result("rejected", tr("This device menu expired. Reopen the right-click menu."))
        try:
            snapshot = self.backend.discover(deadline)
            if snapshot.owner != ticket.owner or ticket.device not in snapshot.devices:
                raise Unavailable(tr("The selected device disconnected or file sharing was disabled."))
            info = snapshot.devices[ticket.device]
            if ticket.kind == "send":
                if (not info.share or file_identities(paths) != ticket.identities
                        or not all(os.access(path, os.R_OK) for path in paths)):
                    raise Unavailable(tr("The selected files changed. Reopen the right-click menu."))
                self.backend.submit(snapshot, ticket.device, [Path(path).as_uri() for path in paths], deadline)
                message = tr("Files submitted to {device}: {count}. Valent handles transfer progress and results.",
                             device=label(info.name), count=total)
            else:
                if not info.browse:
                    raise Unavailable(tr("The selected device disconnected or file sharing was disabled."))
                self.backend.browse(snapshot, ticket.device, ticket.uri, deadline)
                message = tr("Opening phone files in Strata. Valent handles mounting and authentication.")
        except Uncertain as error:
            return result("unknown", str(error))
        except (OSError, ValueError, Unavailable) as error:
            return result("rejected", str(error) if isinstance(error, Unavailable)
                          else tr("The selected files are no longer available."))
        return result("accepted", message)

    def handle(self, request):
        reply = {"version": 1, "id": request.get("id", 0)}
        paths = request.get("paths")
        try:
            valid_paths = (isinstance(paths, list) and 1 <= len(paths) <= 200
                           and all(isinstance(path, str) and path.startswith("/") and "\0" not in path
                                   and len(path.encode("utf-8")) <= 16384 for path in paths))
        except UnicodeError:
            valid_paths = False
        if (request.get("version") != 1 or not valid_paths
                or type(request.get("id")) is not int or request["id"] < 0
                or type(request.get("background")) is not bool):
            return {**reply, "error": "invalid-request", "message": tr("Invalid provider request.")}
        deadline = time.monotonic() + REQUEST_SECONDS
        if request.get("method") == "query":
            return {**reply, "decorations": [], "revision": self.revision}
        if request.get("method") == "menu":
            return {**reply, "actions": self.menu(paths, request["background"], deadline), "revision": self.revision}
        if request.get("method") == "activate":
            return {**reply, **self.activate(request, deadline)}
        return {**reply, "error": "unsupported-method", "message": tr("Unsupported provider method.")}


def main(backend=None):
    from gi.repository import Gio, GLib

    loop = GLib.MainLoop()

    def emit(value):
        print(json.dumps(value, ensure_ascii=True), flush=True)

    def changed():
        emit(provider.invalidate())

    destination = None
    if backend is None and len(sys.argv) > 1:
        if (len(sys.argv) != 4 or sys.argv[1] != "--open-destination"
                or not Gio.dbus_is_unique_name(sys.argv[2])
                or not GLib.Variant.is_object_path(sys.argv[3])):
            raise SystemExit(2)
        destination = (sys.argv[2], sys.argv[3])
    provider = Provider(backend if backend is not None else Valent(changed, destination))
    buffer = bytearray()
    os.set_blocking(sys.stdin.fileno(), False)

    def incoming(fd, condition):
        try:
            chunk = os.read(fd, 65536)
        except BlockingIOError:
            return True
        if not chunk:
            loop.quit()
            return False
        buffer.extend(chunk)
        while b"\n" in buffer:
            end = buffer.index(b"\n")
            if end > MAX_FRAME:
                loop.quit()
                return False
            frame = bytes(buffer[:end])
            del buffer[:end + 1]
            try:
                request = json.loads(frame)
                if not isinstance(request, dict):
                    raise ValueError("Expected an object")
            except (ValueError, UnicodeError):
                loop.quit()
                return False
            emit(provider.handle(request))
        if len(buffer) > MAX_FRAME:
            loop.quit()
            return False
        return True

    GLib.io_add_watch(sys.stdin.fileno(), GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, incoming)
    try:
        loop.run()
    except (BrokenPipeError, KeyboardInterrupt):
        pass


if __name__ == "__main__":
    main()
