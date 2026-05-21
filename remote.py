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
import sys
import threading
import types
from typing import Any

# Workaround: orca's extension loader synthesizes the child package
# (`orca_user_extension.remote`) and registers it in sys.modules, but
# not the top-level parent `orca_user_extension`. Without that
# top-level entry, our relative imports below trigger
# `ModuleNotFoundError: No module named 'orca_user_extension'` because
# Python's import machinery walks parent names. Create a stub parent
# package if one isn't already present.
if "orca_user_extension" not in sys.modules:
    _parent = types.ModuleType("orca_user_extension")
    _parent.__path__ = []  # type: ignore[attr-defined]
    sys.modules["orca_user_extension"] = _parent

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from orca import debug, keybindings  # noqa: E402
from orca.command import Command, KeyboardCommand  # noqa: E402
from orca.extension import Extension  # noqa: E402

from . import protocol
from .settings_dialog import (
    DEFAULT_SETTINGS,
    ROLE_CLIENT,
    ROLE_HOST,
    SETTING_CHANNEL,
    SETTING_FINGERPRINT,
    SETTING_HOST,
    SETTING_PORT,
    SETTING_ROLE,
    build_settings_dialog,
)
from .protocol import CONNECTION_TYPE_MASTER, CONNECTION_TYPE_SLAVE
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
        # Master-only "focus" flag: when True, inbound speak messages
        # from the slave are spoken locally; when False, they are
        # dropped so the master can use their own machine without the
        # remote stream chattering over the top. Toggled by
        # Orca+Alt+Tab. Has no effect when we're the host (a slave
        # never receives speak messages it needs to mute).
        self._focus_on_remote: bool = True
        super().__init__()

    # ---- lifecycle -------------------------------------------------
    #
    # The framework calls set_up_commands() at load time (and again
    # when the extension is re-enabled via the prefs panel, after
    # clearing _disabled / _commands_initialized). disable() is
    # called when the user toggles us off. There is no enable() hook
    # in the base class, so all startup work hangs off
    # set_up_commands.

    def set_up_commands(self) -> None:
        """Register commands, then start the transport."""

        super().set_up_commands()
        if self._disabled:
            return
        self._log("starting transport")
        self._start_loop_thread()
        self._restart_transport()
        # Temporary diagnostic: subscribe to the new speech_emitted
        # signal so we can verify it fires with sensible payloads.
        # Logs every outbound utterance at INFO. Remove once Stage 2
        # host mode actually forwards these on the wire.
        try:
            self.controller.subscribe_speech_emitted(self._on_speech_emitted)
            self._log("subscribed to speech_emitted")
        except AttributeError:
            self._log("controller has no subscribe_speech_emitted (older Orca?)")

    def disable(self) -> None:
        """Stop the transport, then deregister commands."""

        self._log("disabling")
        try:
            self.controller.unsubscribe_speech_emitted(self._on_speech_emitted)
        except AttributeError:
            pass
        self._stop_transport()
        self._stop_loop_thread()
        super().disable()

    def _on_speech_emitted(self, text: str, voice_type: str, language: str) -> None:
        """Forward Orca's outbound speech to the remote master when in host mode.

        Called from whatever thread SpeechServer.speak() ran on
        (typically the GLib main thread). Marshals the actual send
        onto the asyncio loop where the transport lives.
        """

        # No-op unless we're a host and the transport is live.
        if self._current_role() != ROLE_HOST:
            return
        if self._transport is None or self._loop is None:
            return
        if not text:
            return

        # NVDA Remote's speak message: {"type":"speak","sequence":[...]}
        # where sequence entries are either strings (text) or dicts
        # (speech commands). For Stage 2 we forward a single text
        # fragment; richer ACSS / index marks can come later.
        message = {"type": protocol.MSG_SPEAK, "sequence": [text]}
        transport = self._transport

        async def _send() -> None:
            await transport.send(message)

        try:
            asyncio.run_coroutine_threadsafe(_send(), self._loop)
        except RuntimeError:
            # Loop was closed between the check and the schedule.
            # Drop the utterance silently; it's only worth logging at
            # debug level.
            pass

    # ---- command registration -------------------------------------
    #
    # Orca+Ctrl+R       open settings dialog
    # Orca+Ctrl+PageUp  connect (no-op if already connected)
    # Orca+Ctrl+PageDn  disconnect (no-op if already disconnected)
    # Orca+Alt+Tab      switch focus between host and remote machine
    #                    (placeholder until host mode lands in Stage 2)

    def _get_commands(self) -> list[Command]:
        ctrl = keybindings.ORCA_CTRL_MODIFIER_MASK
        alt = keybindings.ORCA_ALT_MODIFIER_MASK
        return [
            KeyboardCommand(
                "orcaRemoteOpenSettingsHandler",
                self.open_settings,
                self.GROUP_LABEL,
                "Open Orca Remote settings",
                desktop_keybinding=keybindings.KeyBinding("r", ctrl),
                laptop_keybinding=keybindings.KeyBinding("r", ctrl),
            ),
            KeyboardCommand(
                "orcaRemoteConnectHandler",
                self.connect,
                self.GROUP_LABEL,
                "Connect Orca Remote",
                desktop_keybinding=keybindings.KeyBinding("Page_Up", ctrl),
                laptop_keybinding=keybindings.KeyBinding("Page_Up", ctrl),
            ),
            KeyboardCommand(
                "orcaRemoteDisconnectHandler",
                self.disconnect_session,
                self.GROUP_LABEL,
                "Disconnect Orca Remote",
                desktop_keybinding=keybindings.KeyBinding("Page_Down", ctrl),
                laptop_keybinding=keybindings.KeyBinding("Page_Down", ctrl),
            ),
            KeyboardCommand(
                "orcaRemoteSwitchSideHandler",
                self.switch_side,
                self.GROUP_LABEL,
                "Switch between host and remote machine",
                desktop_keybinding=keybindings.KeyBinding("Tab", alt),
                laptop_keybinding=keybindings.KeyBinding("Tab", alt),
            ),
        ]

    def connect(self) -> bool:
        if self._transport is not None:
            self._say("Orca Remote already connected.")
            return True
        self._say("Orca Remote: connecting.")
        self._restart_transport()
        return True

    def disconnect_session(self) -> bool:
        if self._transport is None:
            self._say("Orca Remote already disconnected.")
            return True
        self._say("Orca Remote: disconnecting.")
        self._stop_transport()
        return True

    def switch_side(self) -> bool:
        """Toggle the master's focus between remote and local.

        Client (master) only. While focused on remote, inbound
        speech is spoken; while focused on local, it is dropped so
        the master can use their own machine without the remote
        stream chattering over the top. Connection stays up either
        way. On the slave (host), this is a silent no-op -- matching
        how NVDA Remote ignores F11-equivalents on the controlled
        machine.
        """

        if self._current_role() != ROLE_CLIENT:
            return True  # slave: silent no-op

        self._focus_on_remote = not self._focus_on_remote
        if self._focus_on_remote:
            self._say("Orca Remote: focused on remote machine.")
        else:
            self._say("Orca Remote: focused on local machine.")
        return True

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
            if not self._disabled:
                self._restart_transport()
        return True

    def _get_setting(self, key: str) -> Any:
        return self._settings.get(key, DEFAULT_SETTINGS.get(key))

    def _set_setting(self, key: str, value: Any) -> None:
        if self._settings.get(key) == value:
            return
        self._settings[key] = value
        self._save_settings()
        # Reconnect if the change affects the transport. Role is
        # connect-affecting because the join message's connection_type
        # changes (master vs slave).
        if key in (
            SETTING_HOST, SETTING_PORT, SETTING_CHANNEL,
            SETTING_FINGERPRINT, SETTING_ROLE,
        ):
            if not self._disabled:
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
        role = str(self._settings.get(SETTING_ROLE, ROLE_CLIENT) or ROLE_CLIENT)
        connection_type = (
            CONNECTION_TYPE_SLAVE if role == ROLE_HOST else CONNECTION_TYPE_MASTER
        )

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
                connection_type=connection_type,
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
            # Only speak inbound utterances when we're a client AND
            # the master is currently focused on the remote session.
            # Toggling focus to local (Orca+Alt+Tab) silences the
            # remote stream without dropping the connection.
            if (
                self._current_role() == ROLE_CLIENT
                and self._focus_on_remote
            ):
                text = protocol.extract_speech_text(message)
                if text:
                    self._say_async(text)
        elif msg_type == protocol.MSG_CHANNEL_JOINED:
            role = self._current_role()
            if role == ROLE_HOST:
                self._say_async("Orca Remote connected in host mode.")
            else:
                self._say_async("Orca Remote connected.")
        elif msg_type == protocol.MSG_CLIENT_LEFT:
            self._say_async("Orca Remote: peer left.")
        elif msg_type == protocol.MSG_MOTD:
            motd = str(message.get("motd", "")).strip()
            if motd:
                self._log(f"motd: {motd}")
        else:
            self._log(f"unhandled message type: {msg_type}")

    def _current_role(self) -> str:
        role = str(self._settings.get(SETTING_ROLE, ROLE_CLIENT) or ROLE_CLIENT)
        return role if role in (ROLE_CLIENT, ROLE_HOST) else ROLE_CLIENT

    def _on_status(self, status: str) -> None:
        self._log(f"transport: {status}")

    def _on_fingerprint_mismatch(self, actual: str) -> None:
        # Surface the fingerprint we actually saw so the user can
        # paste it into the setting if they trust it. Also copy it
        # to the clipboard from the GLib main thread so a blind user
        # doesn't have to memorise a 64-char hex string.
        GLib.idle_add(self._copy_fingerprint_idle_cb, actual)
        self._say_async(
            "Orca Remote: server fingerprint did not match. "
            "The fingerprint has been copied to the clipboard. "
            "Press Orca+Shift+M, focus the Server fingerprint field, "
            "and paste with Control+V."
        )

    def _copy_fingerprint_idle_cb(self, actual: str) -> bool:
        try:
            self.controller.set_clipboard_text(actual)
        except Exception as error:  # pylint: disable=broad-except
            self._log(f"set_clipboard_text failed: {error}")
        return False  # one-shot

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
