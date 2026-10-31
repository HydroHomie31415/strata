# SPDX-License-Identifier: MIT
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from gi.repository import Gio, GLib

from provider import Device, Provider, Snapshot, Unavailable, Valent

HERE = Path(__file__).resolve().parent
PHONE = "/ca/andyholmes/Valent/Device/phone_2d1"
TABLET = "/ca/andyholmes/Valent/Device/tablet_2d2"
URI = "sftp://phone.test:1739/storage/emulated/0"


def wait_until(predicate, seconds=5):
    expires = time.monotonic() + seconds
    while time.monotonic() < expires:
        while GLib.MainContext.default().pending():
            GLib.MainContext.default().iteration(False)
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Private D-Bus fixture did not reach expected state")


class DBusTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.state = self.root / "state.json"
        self.events = self.root / "events.jsonl"
        self.ready = self.root / "ready"
        self.state.write_text(json.dumps({PHONE: {"name": "Phone"}, TABLET: {"name": "Tablet", "state": 2}}))
        self.bus = subprocess.Popen(["dbus-daemon", "--session", "--nofork", "--print-address=1"],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.addCleanup(self.stop, self.bus)
        address = self.bus.stdout.readline().strip()
        self.assertTrue(address)
        self.environment = {key: value for key, value in os.environ.items()
                            if key not in ("DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS")}
        self.environment.update(DBUS_SESSION_BUS_ADDRESS=address, GIO_USE_VFS="local")
        self.connection = Gio.DBusConnection.new_for_address_sync(
            address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None, None)
        self.addCleanup(self.connection.close_sync, None)
        self.start_service()
        self.changes = []
        self.backend = Valent(lambda: self.changes.append(True))
        with patch.object(Gio, "bus_get_sync", return_value=self.connection):
            self.backend.connect()

    @staticmethod
    def stop(process):
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()

    def start_service(self):
        self.ready.unlink(missing_ok=True)
        self.service = subprocess.Popen(
            [sys.executable, str(HERE / "service.py"), str(self.state), str(self.events), str(self.ready)],
            env=self.environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop, self.service)
        wait_until(self.ready.exists)

    def recorded(self):
        if not self.events.exists():
            return []
        return [json.loads(line) for line in self.events.read_text().splitlines()]

    def test_real_actions_introspection_and_single_uri_batch(self):
        snapshot = self.backend.discover(time.monotonic() + 5)
        self.assertEqual(snapshot.devices, {PHONE: Device("Phone")})
        uris = ["file:///tmp/a%20b", "file:///tmp/%25%0A"]
        self.backend.submit(snapshot, PHONE, uris, time.monotonic() + 5)
        wait_until(lambda: bool(self.recorded()))
        self.assertEqual(self.recorded(), [{"device": PHONE, "action": "share.uris", "parameter": uris}])

    def test_mount_then_exported_folder_opens_in_strata_once(self):
        snapshot = self.backend.discover(time.monotonic() + 5)
        self.backend.browse(snapshot, PHONE, "", time.monotonic() + 5)
        wait_until(lambda: any("open" in event for event in self.recorded()))
        self.assertEqual(self.recorded(), [{"device": PHONE, "action": "sftp.browse", "parameter": None},
                                           {"open": [URI]}])
        self.assertEqual(self.backend.pending, {})

    def test_sidebar_open_returns_to_its_unique_window_endpoint(self):
        path = "/io/github/lgse/Strata/ValentSidebar/test_window"
        name = "io.github.lgse.Strata.ValentSidebar"
        interface = Gio.DBusNodeInfo.new_for_xml(
            "<node><interface name='" + name + "'><method name='Open'>"
            "<arg type='s' direction='in'/></method></interface></node>").interfaces[0]
        opened = []

        def call(connection, sender, object_path, interface_name, method, parameters, invocation):
            opened.append(parameters.unpack()[0])
            invocation.return_value(None)

        registration = self.connection.register_object(path, interface, call, None, None)
        self.addCleanup(self.connection.unregister_object, registration)
        self.backend.open_destination = (self.connection.get_unique_name(), path)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.backend.open_location, URI, time.monotonic() + 5)
            wait_until(future.done)
            future.result()
        self.assertEqual(opened, [URI])
        self.assertEqual(self.recorded(), [])

    def test_disconnect_invalidates_menus_and_old_tickets_reject(self):
        provider = Provider(self.backend)
        request = {"version": 1, "id": 1, "method": "menu", "paths": [str(self.state)], "background": False}
        leaf = provider.handle(request)["actions"][0]
        self.state.write_text("{}")
        wait_until(lambda: not self.backend.discover(time.monotonic() + 2).devices)
        wait_until(lambda: bool(self.changes))
        reply = provider.handle({**request, "id": 2, "method": "activate", "action": leaf["id"], "context": leaf["context"]})
        self.assertEqual(reply["outcome"]["status"], "rejected")
        self.assertEqual(self.recorded(), [])

    def test_restart_does_not_redirect_a_stale_selection(self):
        provider = Provider(self.backend)
        request = {"version": 1, "id": 1, "method": "menu", "paths": [str(self.state)], "background": False}
        leaf = provider.handle(request)["actions"][0]
        self.stop(self.service)
        self.start_service()
        reply = provider.handle({**request, "id": 2, "method": "activate", "action": leaf["id"], "context": leaf["context"]})
        self.assertEqual(reply["outcome"]["status"], "rejected")
        self.assertEqual(self.recorded(), [])


class BrowseJobTests(unittest.TestCase):
    def setUp(self):
        self.backend = Valent()
        self.snapshot = Snapshot(":1.2", {PHONE: Device("Phone")})
        self.callbacks = []
        for name in ("activate", "open_location", "notify"):
            patcher = patch.object(self.backend, name)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(GLib, "timeout_add", side_effect=lambda interval, callback: self.callbacks.append(callback) or len(self.callbacks))
        patcher.start()
        self.addCleanup(patcher.stop)

    def queue(self):
        with patch.object(self.backend, "locations", return_value=[]):
            self.backend.browse(self.snapshot, PHONE, "", time.monotonic() + 5)

    def test_repeated_clicks_do_not_repeat_mount_and_ready_opens_once(self):
        self.queue()
        self.queue()
        self.assertEqual(len(self.callbacks), 1)
        with patch.object(self.backend, "discover", return_value=self.snapshot), patch.object(self.backend, "locations", return_value=[("Phone", URI)]):
            self.assertFalse(self.callbacks[0]())
        self.backend.activate.assert_called_once()
        self.backend.open_location.assert_called_once()
        self.assertEqual(self.backend.pending, {})

    def test_disconnected_or_replaced_service_never_opens_previous_phone(self):
        for current in (Snapshot(":1.2", {}), Snapshot(":1.3", {PHONE: Device("Phone")})):
            with self.subTest(current=current):
                self.queue()
                with patch.object(self.backend, "discover", return_value=current):
                    self.assertFalse(self.callbacks[-1]())
        self.backend.open_location.assert_not_called()
        self.assertEqual(self.backend.notify.call_count, 2)

    def test_mount_timeout_reports_failure_without_retry(self):
        now = time.monotonic()
        self.queue()
        with patch.object(self.backend, "discover", return_value=self.snapshot), patch.object(self.backend, "locations", return_value=[]), patch("provider.time.monotonic", return_value=now + 60):
            self.assertFalse(self.callbacks[0]())
        self.backend.activate.assert_called_once()
        self.backend.open_location.assert_not_called()
        self.backend.notify.assert_called_once()


if __name__ == "__main__":
    unittest.main()
