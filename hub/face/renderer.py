#!/usr/bin/env python3
"""NOVA's face as the hub's live wallpaper (ADR 0028): one borderless window
showing http://127.0.0.1:8765/face. The `nova-face@nova` GNOME Shell extension
puts this window behind every other window, on every workspace, so the nova
is the desktop and the terminal and apps sit on top of it.

Runs with the system Python (GTK 4 + WebKitGTK 6), started at sign-in by
`nova-hub face on`. If the hub service is restarting, it keeps retrying.
"""
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("WebKit", "6.0")
from gi.repository import GLib, Gtk, WebKit  # noqa: E402

URL = "http://127.0.0.1:8765/face"
APP_ID = "io.nova.face"
TITLE = "NOVA face"


def on_activate(app: Gtk.Application) -> None:
    win = Gtk.ApplicationWindow(application=app, title=TITLE, decorated=False)
    view = WebKit.WebView()
    settings = view.get_settings()
    settings.set_enable_developer_extras(False)
    settings.set_hardware_acceleration_policy(WebKit.HardwareAccelerationPolicy.ALWAYS)

    def retry(*_args):  # the hub service may be restarting; come back to the face
        GLib.timeout_add_seconds(5, lambda: (view.load_uri(URL), False)[1])
        return True

    view.connect("load-failed", retry)
    view.connect("web-process-terminated", lambda *_: retry())
    view.load_uri(URL)
    win.set_child(view)
    monitors = win.get_display().get_monitors()   # fill the screen even before the shell fullscreens it
    if monitors.get_n_items():
        geo = monitors.get_item(0).get_geometry()
        win.set_default_size(geo.width, geo.height)
    win.fullscreen()
    win.present()


def main() -> int:
    app = Gtk.Application(application_id=APP_ID)
    app.connect("activate", on_activate)
    return app.run(sys.argv[:1])


if __name__ == "__main__":
    sys.exit(main())
