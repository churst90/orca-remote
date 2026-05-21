"""Remote popup menu (Orca+Ctrl+R landing page).

A Gtk.Menu popup, NVDA-NVDA+N style: appears at the pointer,
Orca speaks the highlighted item, arrow keys navigate, Enter
activates, Escape (or click-outside) dismisses. No window frame,
no Close button -- it's a context menu, not a dialog.

The extension calls `build_remote_menu(state, actions)`; the
function builds, pops up, and returns the Gtk.Menu. The chosen
item's callback runs when the menu emits "selection-done" so
the menu has finished tearing down before any follow-up dialog
opens.

State shape (dict, all optional with safe defaults):
    is_connected:    bool   -- transport up?
    role:            str    -- ROLE_CLIENT | ROLE_HOST
    speech_muted:    bool   -- host-side speech mirror muted?
    braille_muted:   bool   -- host-side braille mirror muted?
    focus_on_remote: bool   -- master-side: hearing the slave?

Actions dict keys (each value: 0-arg callable). Missing actions
just hide their menu item.
    "settings"        always-shown "Settings…"
    "push_clipboard"  shown when connected
    "toggle_speech"   shown when connected & host
    "toggle_braille"  shown when connected & host
    "toggle_focus"    shown when connected & client
    "connect"         shown when disconnected
    "disconnect"      shown when connected
"""

from __future__ import annotations

from typing import Any, Callable

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402


def build_remote_menu(
    state: dict[str, Any],
    actions: dict[str, Callable[[], Any]],
) -> Gtk.Menu:
    """Build, pop up, and return a context menu."""

    is_connected = bool(state.get("is_connected", False))
    role = str(state.get("role", "client"))
    speech_muted = bool(state.get("speech_muted", False))
    braille_muted = bool(state.get("braille_muted", False))
    focus_on_remote = bool(state.get("focus_on_remote", True))

    menu = Gtk.Menu()
    # Selection-done fires once when the menu tears down (whether
    # because an item was chosen, Escape was pressed, or focus was
    # lost). We use it to invoke the chosen action AFTER the menu
    # widget is gone, so a follow-up dialog (e.g. Settings) doesn't
    # open underneath a still-popping-down menu.
    selected: dict[str, Callable[[], Any] | None] = {"fn": None}

    def add_item(label_text: str, action_key: str) -> None:
        callback = actions.get(action_key)
        if callback is None:
            return
        item = Gtk.MenuItem.new_with_mnemonic(label_text)

        def _on_activate(_w: Gtk.MenuItem) -> None:
            selected["fn"] = callback

        item.connect("activate", _on_activate)
        menu.append(item)

    # State header item -- non-selectable, just for screen-reader
    # context when the menu opens. A Gtk.MenuItem with a sensitive=
    # False makes Orca announce it but skip it during arrow nav.
    if is_connected:
        if role == "host":
            heading = "Orca Remote: connected as host"
        else:
            heading = "Orca Remote: connected as client"
    else:
        heading = "Orca Remote: not connected"
    header = Gtk.MenuItem(label=heading)
    header.set_sensitive(False)
    menu.append(header)
    menu.append(Gtk.SeparatorMenuItem())

    # Always available.
    add_item("_Settings…", "settings")

    if is_connected:
        # Connection-state action: Disconnect when up.
        add_item("_Disconnect", "disconnect")
        # Clipboard push needs an open transport.
        add_item("_Push clipboard to remote", "push_clipboard")
        # Host-side mirror toggles only meaningful when we ARE host.
        if role == "host":
            if speech_muted:
                add_item("_Unmute outbound speech mirror", "toggle_speech")
            else:
                add_item("M_ute outbound speech mirror", "toggle_speech")
            if braille_muted:
                add_item("Resume outbound _braille mirror", "toggle_braille")
            else:
                add_item("Stop outbound _braille mirror", "toggle_braille")
        # Master-side: "mute remote" = stop speaking inbound. Hide
        # in host mode (a slave has nothing to mute).
        if role == "client":
            if focus_on_remote:
                add_item("_Mute inbound remote speech", "toggle_focus")
            else:
                add_item("Un_mute inbound remote speech", "toggle_focus")
    else:
        # Not connected: the connect path opens settings (per user
        # convention -- configuring the relay IS how you connect).
        add_item("_Connect (opens settings)", "connect")

    def _on_selection_done(_m: Gtk.Menu) -> None:
        fn = selected["fn"]
        if fn is not None:
            # Defer the callback one main-loop tick so the menu widget
            # finishes tearing down before any follow-up dialog opens.
            # Otherwise a Settings dialog opened from "Settings…" can
            # appear behind / fight the menu's grab.
            def _run() -> bool:
                try:
                    fn()
                except Exception:  # pylint: disable=broad-except
                    pass
                return False  # one-shot
            GLib.idle_add(_run)

    menu.connect("selection-done", _on_selection_done)
    menu.show_all()
    _popup(menu)
    return menu


def _popup(menu: Gtk.Menu) -> None:
    """Pop the menu up at a sensible location.

    Keyboard activation: the pointer might be anywhere -- including
    off-screen on a multi-monitor setup -- so popup_at_pointer is
    unreliable. Prefer popup_at_widget against the currently focused
    toplevel, which puts the menu over the active window where the
    user actually is.

    Falls back through pointer -> screen-center if the toplevel
    lookup fails.
    """

    try:
        # Try to anchor on the focused toplevel for predictable
        # placement on a screen-reader user's desktop.
        toplevel = None
        for win in Gtk.Window.list_toplevels():
            try:
                if win.is_active() and win.get_visible():
                    toplevel = win
                    break
            except Exception:  # pylint: disable=broad-except
                continue
        if toplevel is not None:
            menu.popup_at_widget(
                toplevel,
                Gdk.Gravity.CENTER,
                Gdk.Gravity.CENTER,
                None,
            )
            return
    except Exception:  # pylint: disable=broad-except
        pass

    try:
        menu.popup_at_pointer(None)
        return
    except Exception:  # pylint: disable=broad-except
        pass

    # Last-resort: GTK3 legacy popup() with no anchor.
    try:
        menu.popup(None, None, None, None, 0, Gtk.get_current_event_time())
    except Exception:  # pylint: disable=broad-except
        pass
