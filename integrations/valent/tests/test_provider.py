# SPDX-License-Identifier: MIT
import json
import os
from pathlib import Path
import selectors
from string import Formatter
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from gi.repository import Gio, GLib

from provider import (
    Device, MESSAGES, Provider, Snapshot, Unavailable, Uncertain, Valent,
    menu_locations, remote_uri, tr,
)

HERE = Path(__file__).resolve().parent
PROVIDER = HERE.parent
PHONE = "/ca/andyholmes/Valent/Device/phone_2d1"
TABLET = "/ca/andyholmes/Valent/Device/tablet_2d2"
URI = "sftp://phone.test:1739/storage/emulated/0"


class FakeValent:
    def __init__(self):
        self.owner = ":1.20"
        self.devices = {PHONE: Device("Phone")}
        self.folders = {}
        self.submissions = []
        self.browsing = []
        self.failure = None

    def discover(self, deadline):
        if self.failure == "offline":
            raise Unavailable("Offline")
        return Snapshot(self.owner, dict(self.devices))

    def locations(self, owner, device):
        return self.folders.get(device, [])

    def submit(self, snapshot, device, uris, deadline):
        if isinstance(self.failure, Exception):
            raise self.failure
        self.submissions.append((snapshot.owner, device, uris))

    def browse(self, snapshot, device, uri, deadline):
        self.browsing.append((snapshot.owner, device, uri))


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.file = self.root / "résumé %23?\n.txt"
        self.file.write_text("fixture")
        self.backend = FakeValent()
        self.now = 100.0
        self.provider = Provider(self.backend, lambda: self.now)
        self.serial = 0

    def request(self, method="menu", paths=None, **extra):
        self.serial += 1
        return {"version": 1, "id": self.serial, "method": method,
                "paths": paths if paths is not None else [str(self.file)], "background": False, **extra}

    def menu(self, paths=None, **extra):
        return self.provider.handle(self.request(paths=paths, **extra))["actions"]

    def activate(self, leaf, paths=None, **extra):
        return self.provider.handle(self.request(
            "activate", paths, action=leaf["id"], context=leaf["context"], **extra))

    def test_send_entire_selection_once_to_exact_device_with_encoded_uris(self):
        second = self.root / "冬.txt"
        second.write_text("second")
        self.backend.devices[TABLET] = Device("Tablet")
        paths = [str(self.file), str(second)]
        leaves = [a for a in self.menu(paths) if a.get("placement") == "send-to"]
        reply = self.activate(leaves[1], paths)
        self.assertEqual(self.backend.submissions, [(":1.20", TABLET, [self.file.as_uri(), second.as_uri()])])
        self.assertEqual(reply["outcome"], {"status": "accepted", "accepted": 2, "total": 2})
        self.assertNotIn("delivered", reply["message"].lower())
        self.assertEqual(self.activate(leaves[1], paths)["outcome"]["status"], "rejected")
        self.assertEqual(len(self.backend.submissions), 1)

    def test_background_and_folder_selections_offer_browsing_without_uploads(self):
        for paths, background in (([str(self.root)], True), ([str(self.root)], False),
                                  ([str(self.file), str(self.root)], False)):
            with self.subTest(paths=paths, background=background):
                actions = self.menu(paths, background=background)
                self.assertFalse(any(a.get("placement") == "send-to" for a in actions))
                browse = actions[0]["children"][0]
                self.assertEqual(self.activate(browse, paths, background=background)["outcome"]["status"], "accepted")
        self.assertEqual(len(self.backend.browsing), 3)
        self.assertEqual(self.backend.submissions, [])

    def test_mounted_phone_exposes_storage_locations_and_opens_selected_one(self):
        self.backend.folders[PHONE] = [("Internal storage", URI), ("SD card", URI + "/sd")]
        browse = self.menu()[-1]["children"][0]
        self.assertEqual([a["label"] for a in browse["children"]], ["Internal storage", "SD card"])
        self.activate(browse["children"][1])
        self.assertEqual(self.backend.browsing, [(":1.20", PHONE, URI + "/sd")])
        self.assertEqual(self.backend.submissions, [])

    def test_each_capability_is_independent(self):
        for info, expected in ((Device("Phone", None, True), ["browse"]),
                               (Device("Phone", "share.uris", False), ["send-to"])):
            with self.subTest(info=info):
                self.backend.devices = {PHONE: info}
                self.assertEqual([a.get("placement", a["id"]) for a in self.menu()], expected)

    def test_missing_service_and_empty_device_list_hide_all_actions(self):
        self.backend.devices.clear()
        self.assertEqual(self.menu(), [])
        self.backend.failure = "offline"
        self.assertEqual(self.menu(), [])

    def test_disconnect_restart_and_disabled_capability_reject_old_send(self):
        for change in ("disconnect", "restart", "disabled", "invalidate"):
            with self.subTest(change=change):
                self.backend.owner = ":1.20"
                self.backend.devices = {PHONE: Device("Phone"), TABLET: Device("Tablet")}
                leaf = self.menu()[0]
                if change == "disconnect":
                    del self.backend.devices[PHONE]
                elif change == "restart":
                    self.backend.owner = ":1.21"
                elif change == "disabled":
                    self.backend.devices[PHONE] = Device("Phone", None, True)
                else:
                    self.provider.invalidate()
                self.assertEqual(self.activate(leaf)["outcome"]["status"], "rejected")
        self.assertEqual(self.backend.submissions, [])

    def test_changed_replaced_removed_or_symlinked_files_never_send(self):
        for operation in ("edit", "replace", "remove", "symlink"):
            with self.subTest(operation=operation):
                self.file.unlink(missing_ok=True)
                self.file.write_text("original")
                leaf = self.menu()[0]
                if operation == "edit":
                    self.file.write_text("edited")
                else:
                    self.file.unlink()
                    if operation == "replace":
                        self.file.write_text("replacement")
                    elif operation == "symlink":
                        self.file.symlink_to("/etc/hosts")
                self.assertEqual(self.activate(leaf)["outcome"]["status"], "rejected")
        self.assertEqual(self.backend.submissions, [])

    def test_selection_background_action_and_expiry_are_bound_to_ticket(self):
        other = self.root / "other"
        other.write_text("other")
        for change in ("selection", "background", "action", "expired"):
            with self.subTest(change=change):
                leaf = self.menu()[0]
                request = self.request("activate", action=leaf["id"], context=leaf["context"])
                if change == "selection":
                    request["paths"] = [str(other)]
                elif change == "background":
                    request["background"] = True
                elif change == "action":
                    request["action"] = "browse"
                else:
                    self.now += 121
                self.assertEqual(self.provider.handle(request)["outcome"]["status"], "rejected")
        self.assertEqual(self.backend.submissions, [])

    def test_lost_reply_is_unknown_and_not_automatically_replayed(self):
        leaf = self.menu()[0]
        self.backend.failure = Uncertain("Lost reply")
        self.assertEqual(self.activate(leaf)["outcome"]["status"], "unknown")
        self.backend.failure = None
        self.assertEqual(self.activate(leaf)["outcome"]["status"], "rejected")
        self.assertEqual(self.backend.submissions, [])

    def test_invalid_and_excessive_selections_are_rejected(self):
        for paths in ([], ["relative"], ["/nul\0"], [str(self.file)] * 201, ["/" + "x" * 16385]):
            with self.subTest(paths=repr(paths)[:80]):
                self.assertEqual(self.provider.handle(self.request(paths=paths))["error"], "invalid-request")
        for extra in ({"background": "false"}, {"id": True}, {"version": 2}):
            self.assertEqual(self.provider.handle(self.request(**extra))["error"], "invalid-request")

    def test_duplicate_names_controls_unicode_and_menu_budget(self):
        self.backend.devices[TABLET] = Device("Phone")
        actions = self.menu()
        self.assertNotEqual(actions[0]["label"], actions[1]["label"])
        self.backend.devices = {PHONE: Device("\n\x1b冬" * 100)}
        for node in (self.menu()[0], self.menu()[-1]["children"][0]):
            self.assertLessEqual(len(node["label"].encode()), 128)
            self.assertNotIn("\x1b", node["label"])
        self.backend.devices = {PHONE + str(i): Device("Phone") for i in range(33)}
        self.assertEqual(self.menu(), [])

    def test_catalog_translations_have_matching_keys_and_placeholders(self):
        for locale, catalog in MESSAGES.items():
            self.assertEqual(set(catalog), set(MESSAGES["en"]), locale)
            for key, value in catalog.items():
                fields = lambda text: {field for _, field, _, _ in Formatter().parse(text) if field}
                self.assertEqual(fields(key), fields(value), (locale, key))
        with patch.dict(os.environ, {"LC_ALL": "fr_FR.UTF-8"}):
            self.assertEqual(tr("Browse phone files"), "Parcourir les fichiers du téléphone")


class MenuTests(unittest.TestCase):
    def test_only_advertised_sftp_folders_are_openable(self):
        root, submenu = Gio.Menu(), Gio.Menu()
        for action, uri in (("device.sftp.open-uri", URI), ("device.sftp.open-uri", "file:///etc/passwd"),
                            ("device.share.uri", URI), ("device.sftp.open-uri", "https://example.com")):
            item = Gio.MenuItem.new("Storage", None)
            item.set_action_and_target_value(action, GLib.Variant("s", uri))
            submenu.append_item(item)
        root.append_submenu("Phone", submenu)
        self.assertEqual(menu_locations(root, GLib.VariantType.new), [("Storage", URI)])
        for uri in ("file:///tmp", "sftp://", "sftp://user:password@host/path", "sftp://host:99999/",
                    "sftp://host/path\n", "sftp://host/path?query", "sftp://host/#fragment"):
            self.assertFalse(remote_uri(uri), uri)


class Connection:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.actions = {"share.uris": (True, "as", []), "sftp.browse": (True, "", [])}

    def is_closed(self):
        return False

    def call_sync(self, owner, path, interface, method, args, result_type, flags, timeout, cancellable):
        self.calls.append((owner, path, interface, method, args.unpack() if args else None, flags, timeout))
        if self.failure:
            raise self.failure
        if method == "GetNameOwner":
            return GLib.Variant("(s)", (":1.2",))
        if method == "GetManagedObjects":
            props = lambda state: {"Name": GLib.Variant("s", "Phone"), "State": GLib.Variant("u", state)}
            return GLib.Variant("(a{oa{sa{sv}}})", ({
                PHONE: {"ca.andyholmes.Valent.Device": props(3)},
                TABLET: {"ca.andyholmes.Valent.Device": props(2)},
            },))
        if method == "DescribeAll":
            return GLib.Variant("(a{s(bgav)})", (self.actions,))
        return GLib.Variant("()", ())


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.backend = Valent()
        self.connection = Connection()
        self.backend.connection = self.connection

    def test_discovery_requires_connected_paired_and_enabled_typed_actions(self):
        self.assertEqual(self.backend.discover(time.monotonic() + 5), Snapshot(":1.2", {PHONE: Device("Phone")}))
        self.assertEqual(self.connection.calls[1][0], ":1.2")
        self.assertTrue(all(call[-2] == Gio.DBusCallFlags.NO_AUTO_START for call in self.connection.calls))
        self.connection.actions = {"share.uris": (False, "as", []), "sftp.browse": (True, "", [])}
        self.assertIsNone(self.backend.discover(time.monotonic() + 5).devices[PHONE].share)
        self.connection.actions = {"share.files": (True, "as", [])}
        self.assertEqual(self.backend.discover(time.monotonic() + 5).devices[PHONE].share, "share.files")
        self.connection.actions = {"share.uris": (True, "s", [])}
        self.assertEqual(self.backend.discover(time.monotonic() + 5).devices, {})

    def test_batch_uses_one_typed_action_call_to_unique_owner(self):
        snapshot = Snapshot(":1.2", {PHONE: Device("Phone")})
        self.backend.submit(snapshot, PHONE, ["file:///tmp/a%20b", "file:///tmp/%25"], time.monotonic() + 5)
        call = self.connection.calls[0]
        self.assertEqual(call[:4], (":1.2", PHONE, "org.gtk.Actions", "Activate"))
        self.assertEqual(call[4], ("share.uris", [["file:///tmp/a%20b", "file:///tmp/%25"]], {}))

    def test_known_and_uncertain_dbus_failures_are_distinct(self):
        self.connection.failure = Gio.DBusError.new_for_dbus_error("org.freedesktop.DBus.Error.UnknownObject", "Gone")
        with self.assertRaises(Unavailable):
            self.backend.activate(":1.2", PHONE, "sftp.browse", None, time.monotonic() + 5)
        self.connection.failure = GLib.Error.new_literal(Gio.io_error_quark(), "timeout", Gio.IOErrorEnum.TIMED_OUT)
        with self.assertRaises(Uncertain):
            self.backend.activate(":1.2", PHONE, "sftp.browse", None, time.monotonic() + 5)

    def test_expired_deadline_never_dispatches(self):
        with self.assertRaises(Unavailable):
            self.backend.activate(":1.2", PHONE, "sftp.browse", None, time.monotonic() - 1)
        self.assertEqual(self.connection.calls, [])

    def test_removed_mount_target_is_rejected_and_never_opens(self):
        with patch.object(self.backend, "locations", return_value=[]):
            with self.assertRaises(Unavailable):
                self.backend.browse(Snapshot(":1.2", {PHONE: Device("Phone")}), PHONE, URI, time.monotonic() + 5)
        self.assertEqual(self.connection.calls, [])


class WireTests(unittest.TestCase):
    def test_fragmented_frames_flush_and_eof_exits(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "file.txt"
            path.write_text("fixture")
            process = subprocess.Popen(
                [sys.executable, "-c", "import sys; sys.path.insert(0,'tests'); from provider import main; from test_provider import FakeValent; main(FakeValent())"],
                cwd=PROVIDER, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env={**os.environ, "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent-strata-test-bus"},
            )
            self.addCleanup(lambda: process.poll() is None and process.kill())
            request = {"version": 1, "id": 1, "method": "menu", "paths": [str(path)], "background": False}
            wire = (json.dumps(request) + "\n").encode()
            process.stdin.write(wire[:20]); process.stdin.flush()
            process.stdin.write(wire[20:]); process.stdin.flush()
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(4), "Provider did not flush a reply")
            leaf = json.loads(process.stdout.readline())["actions"][0]
            activation = {**request, "id": 2, "method": "activate", "action": leaf["id"], "context": leaf["context"]}
            process.stdin.write((json.dumps(activation) + "\n").encode()); process.stdin.flush()
            process.stdin.close(); process.stdin = None
            output, errors = process.communicate(timeout=5)
            self.assertEqual(json.loads(output)["outcome"]["status"], "accepted")
            self.assertEqual(process.returncode, 0, errors.decode())

    def test_missing_session_bus_returns_empty_menu(self):
        request = {"version": 1, "id": 1, "method": "menu", "paths": [__file__], "background": False}
        process = subprocess.run([sys.executable, str(PROVIDER / "provider.py")],
                                 input=json.dumps(request) + "\n", text=True, capture_output=True, timeout=5,
                                 env={**os.environ, "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent-strata-test-bus"})
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)["actions"], [])


if __name__ == "__main__":
    unittest.main()
