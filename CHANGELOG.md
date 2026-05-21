# Changelog

## 0.4.2 -- 2026-05-21

Second round of host-mode safety fixes after the 0.4.1 VM test.

- **Autorepeat suppression for Orca-modifier chords.** NVDA Remote
  forwards OS-level key autorepeat as a stream of PRESS frames,
  one per repeat. Without dedupe, holding Insert+R rapid-fired
  Orca's OCR "Recognizing." command and the slave looped until
  release. The synth callback now drops a PRESS whose keysym is
  already in `_pressed_keysyms` while any Orca modifier is held.
  Plain-key autorepeat (typing, terminal scroll) is unaffected
  because no Orca modifier is in the held set in that context.
- **Tap-vs-modifier detection for Caps Lock / Num Lock /
  Scroll Lock.** 0.4.1 dropped these unconditionally to stop the
  XTest "press = toggle" foot-gun, which also killed legitimate
  taps. Replaced with the behaviour NVDA itself uses internally:
  a standalone tap (PRESS then RELEASE with no other key in
  between) synthesizes a real PRESS+RELEASE toggle; a press-with-
  chord (NVDA-laptop-modifier usage) is dropped on the RELEASE.
  Pending-lock state lives in `_lock_press_pending` on the
  asyncio thread and is cleared on transport teardown so a
  dropped connection mid-press leaves nothing stale.

## 0.4.1 -- 2026-05-21

Host-mode safety fixes from a VM session that locked Orca into
a stuck state requiring a forced power-off.

- **Refuse locking keysyms.** `Caps_Lock`, `Num_Lock`, and
  `Scroll_Lock` are no longer synthesized through XTest. XTest
  treats a press of any locking keysym as a TOGGLE of the X
  server's lock state, and the toggle outlives Orca itself
  because it lives in the X server, not in Orca. NVDA Remote
  forwards Caps Lock as the laptop-layout NVDA modifier; one
  press locked the slave's caps lock on, after which every
  alphabetic Orca chord stopped matching and an Orca restart
  could not undo it (the lock was in X). Slave-side users still
  use their own modifier locally.
- **Refuse own-command chords in host mode.** When the master
  sends an inbound key whose press would complete one of our own
  command bindings (Orca+Ctrl+R, Orca+Ctrl+Page Up/Down,
  Orca+Alt+Tab), the synth is dropped so the chord doesn't fire
  the local settings dialog / connect / disconnect / switch-side.
  The check uses `_pressed_keysyms` (what we've synthesized
  PRESS for and not yet RELEASED), which works because NVDA
  Remote forwards modifiers before the letter key.
- **Non-blocking settings dialog.** Replaced the blocking
  `Gtk.Dialog.run()` with a `response`-signal callback. A
  remote-master-triggered open of the settings dialog can no
  longer suspend the GLib main loop until a local user clicks
  something. `build_settings_dialog` now takes an `on_result`
  callback and returns the dialog immediately.
- **Suppress reconnect re-announces.** The "Orca Remote
  connected" / "...connected in host mode" announcement now
  fires only on the first `channel_joined` per session intent.
  Subsequent auto-reconnects (network blip, relay restart) are
  silent; explicit Connect / Disconnect chords and settings
  saves reset the gate so the next user-driven join is
  announced. Previously a flaky link to nvdaremote.com produced
  an announcement every ~30s (the backoff cap) which the master
  heard as "repeating things over and over."

## 0.4.0 -- 2026-05-21

Robustness fixes from first host-mode VM test.

- **Stuck-key safety net.** Host mode now tracks every keysym it
  synthesizes a `PRESS` for, and on transport teardown or extension
  disable it synthesizes the matching `RELEASE` for anything still
  held. `Atspi.generate_keyboard_event` goes through XTEST, so a
  press without a release outlives Orca itself; previously, a
  dropped connection mid-pair could leave a key held until the
  user force-killed the session. Releases are best-effort and
  swallowed if the AT-SPI device is already gone.
- **Auto-connect persistence.** Settings now carry an
  `auto_connect` flag (default True). Orca+Ctrl+Page Up flips it
  on; Orca+Ctrl+Page Down flips it off. On extension startup we
  only dial the relay if the flag is True, so an explicit
  disconnect followed by Orca restart stays offline until the user
  asks to reconnect. Settings file at
  `$XDG_DATA_HOME/orca/orca-remote-settings.json` will gain the
  new key on first save.

## 0.3.0 -- 2026-05-20

Stage 2 (Phase 1): host mode lands.

- New **Role** setting: "Receive speech (control a remote machine)"
  or "Broadcast speech (let a remote machine control us)".
- In host mode, the extension subscribes to the
  `speech_emitted` signal on the controller (perf-branch addition)
  and forwards every utterance to the relay as an NVDA-Remote
  `speak` message. No monkey-patching of the speech server.
- Inbound `speak` messages are now ignored when we're in host mode
  (prevents feedback if both peers somehow broadcast).
- **Orca + Alt + Tab** is no longer a placeholder: in client mode
  it toggles master focus between the remote session and the local
  machine. Focused-on-local mutes the inbound speech stream without
  dropping the connection; useful when a helper wants to use their
  own machine briefly. No-op in host mode (the slave has no remote
  session to focus away from); role changes happen in the settings
  dialog.
- Channel-joined announcement is role-aware
  ("connected" vs "connected in host mode").
- README rewritten for bidirectional scope.

## 0.2.0 -- 2026-05-20

- Rebind settings dialog from Orca+Shift+M to **Orca+Ctrl+R**.
- Split connect/disconnect into explicit chords:
  **Orca+Ctrl+Page Up** connects; **Orca+Ctrl+Page Down** disconnects.
- Reserve **Orca+Alt+Tab** for switching between host and remote
  machine (placeholder; lands with Stage 2 host mode).
- Auto-copy the server fingerprint to the clipboard on a pin mismatch
  so a screen-reader user can paste it directly into the settings
  field instead of memorising 64 hex characters.
- Workaround for the extension loader not registering the synthetic
  top-level parent (`orca_user_extension`) in sys.modules, which
  blocked relative imports.

## 0.1.0 -- 2026-05-20

Initial release. Stage 1 MVP.

- Client-only receive-speech mirror.
- NVDA Remote v2.x wire-protocol compatibility (newline-JSON over
  TLS, `protocol_version` + `join` handshake, `speak` / `cancel`
  / `motd` / `channel_joined` / `client_joined` / `client_left`
  inbound handling).
- Custom Gtk settings dialog bound to **Orca + Shift + M** (host,
  port, channel key, server fingerprint).
- Server cert pinned by SHA-256 fingerprint; first-connect
  bootstrap surfaces the actual fingerprint to the user.
- Settings persist to `$XDG_DATA_HOME/orca/orca-remote-settings.json`.
- Inbound speech routed through `controller.present_message_internal`,
  so it speaks through whatever TTS Orca is configured to use
  (espeak-ng / Voxin / sd-piper / etc.).
