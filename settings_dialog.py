"""Custom Gtk settings dialog for Orca Remote.

The Orca preferences framework only ships boolean / range / enum /
color / selection PreferenceControls -- there is no string control.
Rather than pad the framework for one extension, Stage 1 owns its
settings UI directly: a modal Gtk.Dialog with labeled entries for
host, port, channel key, and server fingerprint. The extension
binds Orca+Shift+R to open it.

`build_settings_dialog(initial)` blocks (Gtk.Dialog.run) and returns
either a dict of new settings (OK) or None (cancel / close).
"""

from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402


# Setting keys -- also the JSON keys persisted by the extension.
SETTING_HOST = "host"
SETTING_PORT = "port"
SETTING_CHANNEL = "channel"
SETTING_FINGERPRINT = "fingerprint"
SETTING_ROLE = "role"

ROLE_CLIENT = "client"  # Receive speech from a remote (we are master).
ROLE_HOST = "host"      # Broadcast our speech (we are slave/host).

_ROLE_LABELS: list[tuple[str, str]] = [
    (ROLE_CLIENT, "Receive speech (control a remote machine)"),
    (ROLE_HOST,   "Broadcast speech (let a remote machine control us)"),
]


DEFAULT_SETTINGS: dict[str, Any] = {
    SETTING_HOST: "nvdaremote.com",
    SETTING_PORT: 6837,
    SETTING_CHANNEL: "",
    SETTING_FINGERPRINT: "",
    SETTING_ROLE: ROLE_CLIENT,
}


# Kept as a thin shim so the extension's get_preference_controls can
# stay empty for Stage 1 (manifest does not declare style="dialog").
# When Stage 2 lands a real StringPreferenceControl in the framework,
# this returns the proper list and the manifest gets the [preferences]
# block back.
def build_preference_controls(getter, setter) -> list[Any]:  # noqa: ARG001
    return []


def build_settings_dialog(initial: dict[str, Any]) -> dict[str, Any] | None:
    """Open the modal settings dialog and return new settings or None.

    Returns None if the user cancels or closes the window. On OK,
    returns a dict with the same keys as DEFAULT_SETTINGS; port is
    coerced back to int (falls back to the existing value if the
    user typed garbage).
    """

    dialog = Gtk.Dialog(
        title="Orca Remote Settings",
        modal=True,
    )
    dialog.add_button("_Cancel", Gtk.ResponseType.CANCEL)
    dialog.add_button("_Save", Gtk.ResponseType.OK)
    dialog.set_default_response(Gtk.ResponseType.OK)

    content = dialog.get_content_area()
    content.set_spacing(8)
    content.set_border_width(12)

    grid = Gtk.Grid()
    grid.set_row_spacing(8)
    grid.set_column_spacing(12)
    content.pack_start(grid, True, True, 0)

    host_entry = _add_text_row(
        grid, 0, "Relay host:", str(initial.get(SETTING_HOST, "")),
    )
    port_entry = _add_text_row(
        grid, 1, "Relay port:", str(initial.get(SETTING_PORT, "")),
    )
    channel_entry = _add_text_row(
        grid, 2, "Channel key:", str(initial.get(SETTING_CHANNEL, "")),
        masked=True,
    )
    fingerprint_entry = _add_text_row(
        grid, 3, "Server fingerprint (SHA-256):",
        str(initial.get(SETTING_FINGERPRINT, "")),
    )
    role_combo = _add_role_row(
        grid, 4, "Role:", str(initial.get(SETTING_ROLE, ROLE_CLIENT)),
    )

    dialog.show_all()
    response = dialog.run()
    if response != Gtk.ResponseType.OK:
        dialog.destroy()
        return None

    # Pull values out before destroying the dialog.
    try:
        port_value = int(port_entry.get_text().strip())
    except ValueError:
        port_value = int(initial.get(SETTING_PORT, DEFAULT_SETTINGS[SETTING_PORT]))

    role_id = role_combo.get_active_id() or ROLE_CLIENT
    if role_id not in (ROLE_CLIENT, ROLE_HOST):
        role_id = ROLE_CLIENT

    result: dict[str, Any] = {
        SETTING_HOST: host_entry.get_text().strip() or DEFAULT_SETTINGS[SETTING_HOST],
        SETTING_PORT: port_value,
        SETTING_CHANNEL: channel_entry.get_text(),
        SETTING_FINGERPRINT: fingerprint_entry.get_text().strip(),
        SETTING_ROLE: role_id,
    }
    dialog.destroy()
    return result


def _add_text_row(
    grid: Gtk.Grid,
    row: int,
    label_text: str,
    initial_value: str,
    masked: bool = False,
) -> Gtk.Entry:
    """Add a label + Gtk.Entry row to the grid and return the entry."""

    label = Gtk.Label(label=label_text, xalign=0.0)
    label.set_hexpand(False)
    grid.attach(label, 0, row, 1, 1)

    entry = Gtk.Entry()
    entry.set_text(initial_value)
    entry.set_hexpand(True)
    entry.set_activates_default(True)
    if masked:
        entry.set_visibility(False)
        entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)
    grid.attach(entry, 1, row, 1, 1)

    label.set_mnemonic_widget(entry)
    return entry


def _add_role_row(
    grid: Gtk.Grid,
    row: int,
    label_text: str,
    initial_value: str,
) -> Gtk.ComboBoxText:
    """Add a label + role ComboBoxText row, return the combo."""

    label = Gtk.Label(label=label_text, xalign=0.0)
    label.set_hexpand(False)
    grid.attach(label, 0, row, 1, 1)

    combo = Gtk.ComboBoxText()
    for role_id, role_label in _ROLE_LABELS:
        combo.append(role_id, role_label)
    if initial_value not in (ROLE_CLIENT, ROLE_HOST):
        initial_value = ROLE_CLIENT
    combo.set_active_id(initial_value)
    combo.set_hexpand(True)
    grid.attach(combo, 1, row, 1, 1)

    label.set_mnemonic_widget(combo)
    return combo
