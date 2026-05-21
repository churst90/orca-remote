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
    SETTING_AUTO_CONNECT,
    SETTING_CHANNEL,
    SETTING_FINGERPRINT,
    SETTING_HOST,
    SETTING_PORT,
    SETTING_ROLE,
    build_settings_dialog,
)
from .keymap import vk_to_keysym
from .protocol import CONNECTION_TYPE_MASTER, CONNECTION_TYPE_SLAVE
from .transport import RemoteTransport


_SETTINGS_FILENAME = "orca-remote-settings.json"


# Modifier keysyms whose press/release we use to recognize when an
# inbound key sequence is about to complete one of our own command
# chords. Tracked against `_pressed_keysyms`, which records what we've
# synthesized PRESS for and not yet RELEASED.
_ORCA_MOD_KEYSYMS: frozenset[int] = frozenset({
    0xff63,  # XK_Insert       (desktop-layout Orca modifier)
    0xff9e,  # XK_KP_Insert    (numpad variant)
})
_CTRL_KEYSYMS: frozenset[int] = frozenset({0xffe3, 0xffe4})
_ALT_KEYSYMS: frozenset[int] = frozenset({0xffe9, 0xffea})

# Keysyms that, when pressed while Orca-mod + Ctrl are held, would fire
# one of our own commands (open_settings, connect, disconnect). Refused
# in host mode so a remote master can't pop our settings dialog or
# bounce the transport.
_OWN_CTRL_CHORD_KEYSYMS: frozenset[int] = frozenset({
    0x72,    # XK_r          -> open_settings
    0xff55,  # XK_Page_Up    -> connect
    0xff56,  # XK_Page_Down  -> disconnect_session
    0xff9a,  # XK_KP_Page_Up
    0xff9b,  # XK_KP_Page_Down
})
_OWN_ALT_CHORD_KEYSYMS: frozenset[int] = frozenset({
    0xff09,  # XK_Tab        -> switch_side
})


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
        # Host-side: keysyms we've synthesized PRESS for but not yet
        # RELEASE. If the transport drops between a press/release pair
        # the X server would otherwise believe the key is still held,
        # which survives Orca restart (Atspi.generate_keyboard_event
        # uses XTEST). On disconnect / disable we drain this set with
        # synthetic releases so a force-killed VM is never the only
        # way out of a stuck modifier.
        self._pressed_keysyms: set[int] = set()
        # Set True the first time we see channel_joined for the current
        # session intent; reset by explicit connect / disconnect /
        # disable so user-initiated transitions re-announce but silent
        # auto-reconnects do not. Without this, a flaky link to the
        # relay produced a "connected in host mode" announcement every
        # 30s (the backoff cap), which is what the VM crash session
        # heard as "repeating things over and over."
        self._announced_join: bool = False
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
        """Register commands, then start the transport if the user
        was connected at last shutdown.

        We always spin up the asyncio thread so a later Orca+Ctrl+
        Page Up can dial without re-entering setup, but we only
        auto-dial when the persisted auto-connect flag says the user
        had an active session before quitting. Explicit disconnect
        (Orca+Ctrl+Page Down) flips that flag off and it stays off
        across Orca restarts.
        """

        super().set_up_commands()
        if self._disabled:
            return
        self._start_loop_thread()
        if bool(self._settings.get(SETTING_AUTO_CONNECT, True)):
            self._log("auto-connect enabled; starting transport")
            self._restart_transport()
        else:
            self._log("auto-connect disabled; waiting for explicit connect")
        # Subscribe to outbound Orca speech so host mode can mirror
        # local utterances onto the relay. No-op on older Orca builds
        # that predate the speech_emitted signal.
        try:
            self.controller.subscribe_speech_emitted(self._on_speech_emitted)
        except AttributeError:
            self._log("controller has no subscribe_speech_emitted (older Orca?)")

    def disable(self) -> None:
        """Stop the transport, then deregister commands."""

        self._log("disabling")
        self._announced_join = False
        try:
            self.controller.unsubscribe_speech_emitted(self._on_speech_emitted)
        except AttributeError:
            pass
        self._stop_transport()
        # Belt-and-braces: _stop_transport already drains held keys,
        # but if there was no transport to stop (e.g. extension toggled
        # off without ever connecting) we still want this to fire.
        self._release_held_keys()
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
        # Explicit connect: remember the intent so the next Orca
        # restart re-dials automatically.
        self._set_auto_connect(True)
        if self._transport is not None:
            self._say("Orca Remote already connected.")
            return True
        # User asked to connect -- they should hear the next join.
        self._announced_join = False
        self._say("Orca Remote: connecting.")
        self._restart_transport()
        return True

    def disconnect_session(self) -> bool:
        # Explicit disconnect: clear the intent so the next Orca
        # restart stays offline until the user reconnects.
        self._set_auto_connect(False)
        # Whatever happens next, the next join should announce again.
        self._announced_join = False
        if self._transport is None:
            self._say("Orca Remote already disconnected.")
            return True
        self._say("Orca Remote: disconnecting.")
        self._stop_transport()
        return True

    def _set_auto_connect(self, value: bool) -> None:
        if bool(self._settings.get(SETTING_AUTO_CONNECT, True)) == value:
            return
        self._settings[SETTING_AUTO_CONNECT] = value
        self._save_settings()

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
        """Show the non-blocking settings dialog; apply on OK."""

        build_settings_dialog(dict(self._settings), self._on_settings_result)
        return True

    def _on_settings_result(self, result: dict[str, Any] | None) -> None:
        """Apply the dict the settings dialog returned (or do nothing).

        Invoked from the GLib main loop when the user closes the
        dialog. `result` is None on cancel / window-close.
        """

        if result is None:
            return
        changed = False
        for key, value in result.items():
            if self._settings.get(key) != value:
                self._settings[key] = value
                changed = True
        if changed:
            self._save_settings()
            self._say("Orca Remote settings saved.")
            if not self._disabled:
                # Settings change -> connect-affecting restart; the
                # user deserves to hear the new join announce.
                self._announced_join = False
                self._restart_transport()

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
                self._announced_join = False
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
            # No live transport, but we may still have a stale held-key
            # set from a previous session. Drain it anyway.
            self._release_held_keys()
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
        # Release anything the remote master pressed before the link
        # went down. Idle-scheduled so it runs on the GLib thread that
        # owns AT-SPI synthesis.
        self._release_held_keys()

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
            if self._announced_join:
                return
            self._announced_join = True
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
        elif msg_type == protocol.MSG_KEY:
            await self._handle_inbound_key(message)
        else:
            self._log(f"unhandled message type: {msg_type}")

    async def _handle_inbound_key(self, message: dict) -> None:
        """Synthesize a remote keystroke on the slave side.

        NVDA Remote key frame:
            {"type":"key","vk_code":<int>,"extended":<bool>,
             "pressed":<bool>,"scan_code":<int>}

        Only honoured when we're the host (a master should never
        receive key frames; if it does, that's a misbehaving peer
        and we drop the frame).
        """

        if self._current_role() != ROLE_HOST:
            return
        try:
            vk_code = int(message.get("vk_code", 0))
            extended = bool(message.get("extended", False))
            pressed = bool(message.get("pressed", False))
        except (TypeError, ValueError):
            self._log(f"malformed key frame: {message!r}")
            return

        keysym = vk_to_keysym(vk_code, extended=extended)
        if keysym == 0:
            self._log(
                f"unmapped VK code 0x{vk_code:x} "
                f"(extended={extended}, pressed={pressed})"
            )
            return

        # Two-part interrupt on every PRESS:
        #
        # 1. MSG_CANCEL outbound to the master, BEFORE we synth. The
        #    master's NVDA holds a speech queue of every speak message
        #    we've forwarded; pressing Ctrl on the master cancels
        #    NVDA's *local* speech but does nothing to that queue
        #    because the wire never told the master to drain. NVDA
        #    Remote v2.x's `cancel` message is exactly the signal to
        #    drain it. Sending CANCEL inline (await rather than schedule)
        #    keeps it strictly ordered before any speak we generate by
        #    reacting to this same key.
        # 2. Local SpeechManager.InterruptSpeech via GLib idle. Orca's
        #    natural interrupt-on-key path (_present) should also fire
        #    when XTest delivers the event we're about to synth, but
        #    under VM AT-SPI load it can lag noticeably. This makes
        #    the slave's own speech-dispatcher cancel deterministic.
        if pressed:
            if self._transport is not None:
                try:
                    await self._transport.send(
                        {"type": protocol.MSG_CANCEL},
                    )
                except Exception as error:  # pylint: disable=broad-except
                    self._log(f"outbound CANCEL failed: {error}")
            GLib.idle_add(self._interrupt_speech_idle_cb)

        GLib.idle_add(self._synthesize_key_idle_cb, keysym, pressed)

    def _synthesize_key_idle_cb(self, keysym: int, pressed: bool) -> bool:
        # Suppress duplicate PRESS events. NVDA Remote forwards
        # OS-level autorepeat as a stream of PRESS frames -- and the
        # user reports that even a SINGLE physical press can arrive
        # as multiple frames on the wire. Without this, every duplicate
        # frame fires the bound Orca command again ("Recognizing."
        # looping on a single Insert+R tap). Dropping any PRESS for a
        # keysym we already have in _pressed_keysyms (i.e. we have
        # synthesized a PRESS we haven't yet RELEASED) collapses all
        # of those down to one event. The cost: key autorepeat for
        # typing-style use (holding 'a' to fill a text field) is gone
        # over the link -- the user taps for each character. Worth it.
        if pressed and keysym in self._pressed_keysyms:
            return False

        # Refuse chords that would fire our own commands. Without this,
        # a remote master pressing e.g. Orca+Ctrl+R synthesizes through
        # XTest, which Orca's own input listener picks up, and our
        # open_settings command runs on the slave -- previously a
        # blocking modal Gtk dialog. The check uses _pressed_keysyms
        # (what we've synthesized PRESS for and not yet RELEASED), so
        # by the time the alphabetic key of the chord arrives, the
        # modifiers are already in the set.
        if pressed and self._chord_matches_own_command(keysym):
            self._log(
                f"refusing own-command chord (keysym 0x{keysym:x}, "
                f"held={sorted(self._pressed_keysyms)})"
            )
            return False

        ok = False
        try:
            ok = bool(self.controller.synthesize_key_event(keysym, pressed))
        except AttributeError:
            self._log("controller has no synthesize_key_event (older Orca?)")
        except Exception as error:  # pylint: disable=broad-except
            self._log(f"synthesize_key_event raised: {error}")
        if ok:
            # Track press/release pairing so a dropped connection mid-pair
            # can't leak a held key into the X server, and so the dedupe
            # check above can spot duplicate frames. Locking keysyms
            # (Caps_Lock / Num_Lock / Scroll_Lock) are also tracked
            # because a stuck PRESS without a RELEASE would otherwise
            # let a future duplicate PRESS through, double-toggling X.
            if pressed:
                self._pressed_keysyms.add(keysym)
            else:
                self._pressed_keysyms.discard(keysym)
        return False  # one-shot

    def _interrupt_speech_idle_cb(self) -> bool:
        """Cancel any in-progress Orca speech on the GLib main thread."""

        try:
            self.controller.execute_command_internal(
                "SpeechManager", "InterruptSpeech", notify_user=False,
            )
        except Exception as error:  # pylint: disable=broad-except
            self._log(f"InterruptSpeech failed: {error}")
        return False  # one-shot

    def _chord_matches_own_command(self, keysym: int) -> bool:
        """True if synthesizing `keysym` PRESS would complete one of our chords.

        Relies on `_pressed_keysyms` already containing the modifier
        keysyms the master sent before the alphabetic key. NVDA Remote
        forwards modifiers first, so by the time the letter/Page key
        arrives the modifier set is populated; reversed orderings will
        not match here and fall through to a normal synth, which is
        acceptable: we'd rather miss a refusal than refuse a real key.
        """

        held = self._pressed_keysyms
        if not (held & _ORCA_MOD_KEYSYMS):
            return False
        if keysym in _OWN_CTRL_CHORD_KEYSYMS and (held & _CTRL_KEYSYMS):
            return True
        if keysym in _OWN_ALT_CHORD_KEYSYMS and (held & _ALT_KEYSYMS):
            return True
        return False

    def _release_held_keys(self) -> None:
        """Synthesize RELEASE for any keysym we previously pressed.

        Called from the asyncio thread (via _stop_transport) and from
        disable(). Snapshots the set before iterating because each
        idle callback mutates it; runs on the GLib main thread for the
        same reason _synthesize_key_idle_cb does. Best-effort: if the
        AT-SPI device is gone we swallow the error.
        """

        if not self._pressed_keysyms:
            return
        held = sorted(self._pressed_keysyms)
        self._log(f"releasing {len(held)} held keysym(s) on shutdown")
        for keysym in held:
            GLib.idle_add(self._synthesize_key_idle_cb, keysym, False)

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
