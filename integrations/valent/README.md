# Valent file provider

An optional [external file provider](../../docs/file-providers.md) for sending
files to paired phones and browsing their files from Strata.

## Use

Install **Valent** from **Settings → Integrations → Devices**, then open Valent
and pair your phone. Both devices must be connected, and the relevant file
sharing plugins must be enabled in Valent and on the phone.

- Select local files and choose **Send to… → Valent → your phone**. The entire
  selection is submitted once; Valent shows transfer progress and results.
  Submission does not prove delivery to the phone.
- Your connected phone appears under **Devices** in the sidebar. Click its name
  to mount and open its files in that window. If it exports several storage locations, the row
  opens the first; the browsing menu below lists each location individually.
  The row follows connection changes and the integration's enabled state. Phone
  browsing is unavailable in file chooser windows and local-only sessions.
- Right-click a folder background or an item and choose
  **Browse phone files → your phone** in the Valent section. Valent mounts its
  SFTP storage; the provider opens the first exported folder in Strata. Once
  mounted, the submenu lists the phone's individual storage locations. Mounted
  locations are also available through Strata's existing GVfs support.

The capabilities are independent: a phone without SFTP can still receive files.
Disconnected and unpaired devices do not appear. Menus update when devices,
capabilities or exported folders change. Duplicate names get identity suffixes.

Browsing needs a GVfs SFTP backend on the desktop and remote file access enabled
on the phone. On NixOS, enable `services.gvfs.enable = true`; this machine already
has it enabled. Valent owns authentication and mounting. The provider does not
read private keys, store credentials, change pairing, or replace the default
file manager. Some phones do not support remote browsing.

If mounting takes longer than 30 seconds, a desktop notification reports the
failure. Check Valent, connectivity and the phone's file-sharing permissions,
then try again. Mount requests and file submissions are never retried
automatically. Discovery requires a running Valent service.

## Dependencies and installation

Python 3.10 or later with PyGObject/Gio, Valent, and Strata's external provider
support are required. Catalog installation writes the provider under
`${XDG_CONFIG_HOME:-$HOME/.config}/strata/providers/valent/`. Disable or remove it
on the Integrations page. A manual registration has this shape, with actual
absolute paths for the interpreter and installed script:

```json
{
  "version": 1,
  "id": "valent",
  "name": "Valent",
  "category": "devices",
  "command": ["/absolute/path/to/python3", "/absolute/path/to/providers/valent/provider.py"]
}
```

Keep `messages.json` beside `provider.py`. Select **Refresh** on the Integrations
page after manual installation. As with all external providers, this is trusted
code running with the user's permissions. Messages follow the system locale,
with all thirteen Strata languages supplied.

The provider talks to `ca.andyholmes.Valent` on the session bus and can also work
with a Flatpak instance exporting that interface. A Flatpak-only setup needs
manual registration if the catalog's native `valent` command check fails.

## Selection and failure semantics

Sending supports 1–200 native UTF-8 regular files. Folders, symbolic links,
unreadable files and mixed unsupported selections are not partially sent. Files
are checked again at activation; edited, replaced or removed files are rejected.
Valent opens paths after accepting an action, so the final path lookup and
opening are not an atomic snapshot. This provider does not lock files.

Single-use context tokens bind the exact selection, foreground/background
context, device object path, operation, exported URI and Valent's unique D-Bus
owner. A restart, disconnect, expired token or changed selection rejects an old
action. A missing reply is reported as uncertain: a send may already have
reached the phone, so check it before retrying.

The provider discovers devices through `org.freedesktop.DBus.ObjectManager`,
uses typed, enabled `org.gtk.Actions` capabilities (`share.uris` or compatible
`share.files`, and `sftp.browse`), and reads only that device's exported
`device.sftp.open-uri` targets. It opens validated SFTP locations through
Strata's own application interface. Menus exceeding Strata's 64-node limit are
withheld rather than truncated.

Interface references:

- [Valent device actions](https://valent.andyholmes.ca/documentation/class.Device.html)
- [Valent D-Bus interface](https://github.com/andyholmes/valent/discussions/613)
- [File-sharing actions](https://github.com/andyholmes/valent/blob/main/src/plugins/share/valent-share-plugin.c)
- [SFTP actions and folders](https://github.com/andyholmes/valent/blob/main/src/plugins/sftp/valent-sftp-plugin.c)

## Tests

With PyGObject and `dbus-daemon` available, run from this directory:

```sh
python3 -m unittest discover -s tests -v
```

Tests cover discovery, typed batch submission, file identity, disconnects,
restarts, selection binding, uncertain replies, asynchronous mounting, storage
locations, URI filtering, locale catalogs and framed transport. The fixture
exports real Gio action groups, menus and application interfaces on a private
session bus. It records requests without reading selected content, transferring
files, mounting a phone or using the desktop session bus.

The `valent-sidebar-opens-phone-files` QA scenario covers pointer activation,
mount and storage tokens, disconnection and disabling. Targeted Rust tests under
`ui::window::valent::` also cover lifecycle changes and URI validation. Existing
file-provider scenarios cover the unchanged shared menu renderer. Mocked
acceptance does not establish physical delivery: send disposable files and
verify them on the phone before using it for documents.

Sidebar providers run with an internal `--open-destination` argument naming a
unique session-bus connection and a per-window object. After validating the
phone's exported URI, the provider returns it there, so a click navigates the
originating window. Only SFTP locations during a pending click are accepted;
closing the window unregisters the endpoint. Context-menu browsing retains the
normal Strata application open interface.
