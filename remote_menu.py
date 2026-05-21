"""Remote menu dialog (Orca+Ctrl+R landing page).

A non-blocking Gtk.Dialog with state-dependent action buttons. The
extension calls `build_remote_menu(state, actions)`; the dialog
returns immediately and invokes the chosen action callback (if any)
when the user picks an item or cancels.

Why a Gtk.Dialog of buttons, not a Gtk.Menu? Pop-up menus are
finicky with screen readers (Orca's menu handling is more reliable
inside a dialog frame), and we want mnemonic keys, arrow-key
navigation, and a clear "Cancel/Close" out. The chosen item runs
its callback on the GLib main loop; the dialog destroys itself
before the callback fires so the action can open follow-up dialogs
(e.g. Settings) without z-order surprises.

State shape (dict, all optional with safe defaults):
    is_connected:    bool   -- transport up?
    role:            str    -- ROLE_CLIENT | ROLE_HOST
    speech_muted:    bool   -- host-side speech mirror muted?
    braille_muted:   bool   -- host-side braille mirror muted?
    focus_on_remote: bool   -- master-side: hearing the slave?

Actions dict keys (each value: 0-arg callable). Missing actions
just hide their button.
    "settings"
    "push_clipboard"
    "toggle_speech"
    "toggle_braille"
    "toggle_focus"
    "connect"
    "disconnect"
"""

from __future__ import annotations

from typing import Any, Callable

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402


def build_remote_menu(
    state: dict[str, Any],
    actions: dict[str, Callable[[], Any]],
) -> Gtk.Dialog:
    """Show the remote menu without blocking. Returns the dialog."""

    is_connected = bool(state.get("is_connected", False))
    role = str(state.get("role", "client"))
    speech_muted = bool(state.get("speech_muted", False))
    braille_muted = bool(state.get("braille_muted", False))
    focus_on_remote = bool(state.get("focus_on_remote", True))

    dialog = Gtk.Dialog(
        title="Orca Remote",
        modal=True,
    )
    dialog.add_button("_Close", Gtk.ResponseType.CANCEL)
    dialog.set_default_response(Gtk.ResponseType.CANCEL)

    content = dialog.get_content_area()
    content.set_spacing(6)
    content.set_border_width(12)

    # State line up top so a screen reader user can read off the
    # current status without poking each button. Spoken when the
    # dialog opens (Orca reads the heading at dialog activation).
    if is_connected:
        if role == "host":
            heading = "Status: connected (host — broadcasting our speech)"
        else:
            heading = "Status: connected (client — listening to remote)"
    else:
        heading = "Status: not connected"
    label = Gtk.Label(label=heading, xalign=0.0)
    label.set_selectable(True)
    content.pack_start(label, False, False, 0)

    selected_action: dict[str, Callable[[], Any] | None] = {"fn": None}

    def add_button(label_text: str, action_key: str) -> None:
        callback = actions.get(action_key)
        if callback is None:
            return
        button = Gtk.Button.new_with_mnemonic(label_text)
        button.set_hexpand(True)

        def _on_clicked(_btn: Gtk.Button) -> None:
            selected_action["fn"] = callback
            dialog.response(Gtk.ResponseType.OK)

        button.connect("clicked", _on_clicked)
        content.pack_start(button, False, False, 0)

    # Always present: Settings.
    add_button("_Settings…", "settings")

    if is_connected:
        # Connection-state action: Disconnect when up.
        add_button("_Disconnect", "disconnect")
        # Clipboard push needs an open transport.
        add_button("_Push clipboard to remote", "push_clipboard")
        # Host-side mirror toggles are only meaningful when we ARE
        # the host. Hide them in client mode.
        if role == "host":
            if speech_muted:
                add_button("_Unmute outbound speech mirror", "toggle_speech")
            else:
                add_button("M_ute outbound speech mirror", "toggle_speech")
            if braille_muted:
                add_button("Resume outbound _braille mirror", "toggle_braille")
            else:
                add_button("Stop outbound _braille mirror", "toggle_braille")
        # Master-side: "mute remote" = stop speaking inbound. Hide
        # in host mode (a slave has nothing to mute).
        if role == "client":
            if focus_on_remote:
                add_button("_Mute inbound remote speech", "toggle_focus")
            else:
                add_button("Un_mute inbound remote speech", "toggle_focus")
    else:
        # Not connected: the connect path opens settings (the user's
        # stated convention -- "connect" is the settings window
        # because that's how you configure the relay).
        add_button("_Connect (opens settings)", "connect")

    dialog.show_all()

    def _on_response(_d: Gtk.Dialog, _resp: int) -> None:
        fn = selected_action["fn"]
        _d.destroy()
        if fn is not None:
            try:
                fn()
            except Exception:  # pylint: disable=broad-except
                # Swallow: the calling extension logs via debug. We
                # can't surface here because the dialog is gone.
                pass

    dialog.connect("response", _on_response)
    return dialog
