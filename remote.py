"""RemoteExtension -- the user-extension entry point.

Stage 1 scope: client-only receive-speech mirror. On enable, spin up
an asyncio loop in a daemon thread that holds an outbound TLS
connection to an NVDA Remote v2.x relay. Inbound `speak` messages
are routed back to Orca's main thread and spoken via the controller.

No keyboard commands and no host (be-controlled) mode in Stage 1.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from typing import Any

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from orca import debug, keybindings  # noqa: E402
from orca.command import Command, KeyboardCommand  # noqa: E402
from orca.extension import Extension  # noqa: E402

from . import protocol
from .settings_dialog import (
    DEFAULT_SETTINGS,
    SETTING_CHANNEL,
    SETTING_FINGERPRINT,
    SETTING_HOST,
    SETTING_PORT,
    build_settings_dialog,
)
from .transport import RemoteTransport


_SETTINGS_FILENAME = "orca-remote-settings.json"


def _settings_path() -> str:
    """Absolute path to the JSON settings file."""

    return os.path.join(
        GLib.get_user_data_dir(),  # pylint: disable=no-value-for-parameter
        "orca",
        _SETTINGS_FILENAME,
    )


class RemoteExtension(Extension):
    """NVDA Remote v2.x receive-speech mirror."""

    GROUP_LABEL = "Orca Remote"

    def __init__(self) -> None:
        self._settings: dict[str, Any] = dict(DEFAULT_SETTINGS)
        self._load_settings()

        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._transport: RemoteTransport | None = None
        self._enabled: bool = False
        super().__init__()

    # ---- lifecycle -------------------------------------------------

    def enable(self) -> None:
        """Called by the loader when the extension is turned on."""

        self._enabled = True
        self._log("enabling")
        self._start_loop_thread()
        self._restart_transport()

    def disable(self) -> None:
        """Called by the loader when the extension is turned off."""

        self._enabled = False
        self._log("disabling")
        self._stop_transport()
        self._stop_loop_thread()
        super().disable()

    # ---- command registration -------------------------------------

    def _get_commands(self) -> list[Command]:
        return [
            KeyboardCommand(
                "orcaRemoteOpenSettingsHandler",
                self.open_settings,
                self.GROUP_LABEL,
                "Open Orca Remote settings",
                desktop_keybinding=keybindings.KeyBinding(
                    "r", keybindings.ORCA_SHIFT_MODIFIER_MASK,
                ),
                laptop_keybinding=keybindings.KeyBinding(
                    "r", keybindings.ORCA_SHIFT_MODIFIER_MASK,
                ),
            ),
        ]

    def open_settings(self) -> bool:
        """Open the modal settings dialog and apply any changes on OK."""

        result = build_settings_dialog(dict(self._settings))
        if result is None:
            return True
        changed = False
        for key, value in result.items():
            if self._settings.get(key) != value:
                self._settings[key] = value
                changed = True
        if changed:
            self._save_settings()
            self._say("Orca Remote settings saved.")
            if self._enabled:
                self._restart_transport()
        return True

    def _get_setting(self, key: str) -> Any:
        return self._settings.get(key, DEFAULT_SETTINGS.get(key))

    def _set_setting(self, key: str, value: Any) -> None:
        if self._settings.get(key) == value:
            return
        self._settings[key] = value
        self._save_settings()
        # Reconnect if the change affects the transport. Stage 1
        # treats all four primary settings as connect-affecting.
        if key in (SETTING_HOST, SETTING_PORT, SETTING_CHANNEL, SETTING_FINGERPRINT):
            if self._enabled:
                self._restart_transport()

    # ---- settings persistence -------------------------------------

    def _load_settings(self) -> None:
        path = _settings_path()
        try:
            with open(path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as error:
            self._log(f"settings load failed ({error}); using defaults")
            return
        if isinstance(loaded, dict):
            for key, default in DEFAULT_SETTINGS.items():
                value = loaded.get(key, default)
                # Coerce port back to int in case the JSON stored a string.
                if key == SETTING_PORT:
                    try:
                        value = int(value)
                    except (TypeError, ValueError):
                        value = default
                self._settings[key] = value

    def _save_settings(self) -> None:
        path = _settings_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(self._settings, handle, indent=2)
        except OSError as error:
            self._log(f"settings save failed: {error}")

    # ---- asyncio thread plumbing ----------------------------------

    def _start_loop_thread(self) -> None:
        if self._loop_thread is not None:
            return
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop_thread_main,
            name="orca-remote-asyncio",
            daemon=True,
        )
        self._loop_thread.start()

    def _loop_thread_main(self) -> None:
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            try:
                self._loop.close()
            except Exception:  # pylint: disable=broad-except
                pass

    def _stop_loop_thread(self) -> None:
        if self._loop is None or self._loop_thread is None:
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop_thread.join(timeout=2.0)
        self._loop = None
        self._loop_thread = None

    # ---- transport orchestration ----------------------------------

    def _restart_transport(self) -> None:
        """Stop any current transport and start a fresh one."""

        if self._loop is None:
            return
        # Validate enough is configured to even try.
        channel = str(self._settings.get(SETTING_CHANNEL, "") or "")
        host = str(self._settings.get(SETTING_HOST, "") or "")
        port = int(self._settings.get(SETTING_PORT, 0) or 0)
        fingerprint = str(self._settings.get(SETTING_FINGERPRINT, "") or "")

        # Stop existing transport (if any) before starting a new one.
        # Done in the loop thread so we don't race with its read loop.
        self._stop_transport()

        if not host or port <= 0 or not channel:
            self._say("Orca Remote not configured. Open Settings to set host, port, and channel key.")
            return

        async def _setup() -> None:
            transport = RemoteTransport(
                host=host,
                port=port,
                channel=channel,
                fingerprint=fingerprint,
                on_message=self._on_message,
                on_status=self._on_status,
                on_fingerprint_mismatch=self._on_fingerprint_mismatch,
            )
            transport.start()
            self._transport = transport

        asyncio.run_coroutine_threadsafe(_setup(), self._loop)

    def _stop_transport(self) -> None:
        if self._loop is None or self._transport is None:
            return
        transport = self._transport
        self._transport = None

        async def _teardown() -> None:
            await transport.stop()

        future = asyncio.run_coroutine_threadsafe(_teardown(), self._loop)
        try:
            future.result(timeout=2.0)
        except Exception:  # pylint: disable=broad-except
            pass

    # ---- callbacks (run on asyncio thread) ------------------------

    async def _on_message(self, message: dict) -> None:
        msg_type = message.get("type")
        if msg_type == protocol.MSG_SPEAK:
            text = protocol.extract_speech_text(message)
            if text:
                self._say_async(text)
        elif msg_type == protocol.MSG_CHANNEL_JOINED:
            self._say_async("Orca Remote connected.")
        elif msg_type == protocol.MSG_CLIENT_LEFT:
            self._say_async("Orca Remote: peer left.")
        elif msg_type == protocol.MSG_MOTD:
            motd = str(message.get("motd", "")).strip()
            if motd:
                self._log(f"motd: {motd}")
        else:
            self._log(f"unhandled message type: {msg_type}")

    def _on_status(self, status: str) -> None:
        self._log(f"transport: {status}")

    def _on_fingerprint_mismatch(self, actual: str) -> None:
        # Surface the fingerprint we actually saw so the user can
        # paste it into the setting if they trust it.
        self._say_async(
            "Orca Remote: server fingerprint did not match. "
            "Open Settings and set Server fingerprint to: "
            + actual
        )

    # ---- helpers ---------------------------------------------------

    def _say(self, text: str) -> None:
        """Speak from the main (GLib) thread."""

        try:
            self.controller.present_message_internal(text)
        except Exception as error:  # pylint: disable=broad-except
            self._log(f"present_message_internal failed: {error}")

    def _say_async(self, text: str) -> None:
        """Speak from the asyncio thread by marshalling onto the GLib loop."""

        GLib.idle_add(self._say_idle_cb, text)

    def _say_idle_cb(self, text: str) -> bool:
        self._say(text)
        return False  # one-shot

    def _log(self, message: str) -> None:
        debug.print_message(debug.LEVEL_INFO, f"REMOTE: {message}", True)
