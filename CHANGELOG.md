# Changelog

## 0.5.0 -- 2026-05-21

Flow-control and silent-drop fixes. First wave of a larger Stage-3
push (bidirectional menu UI, clipboard, braille, master-side key
forwarding). 0.5.0 is the foundation; user-visible features land in
0.5.x and 0.6.x.

- **Fire-and-forget outbound CANCEL.** The 0.4.4 inline
  `await transport.send({"type":"cancel"})` inside the inbound key
  handler was serializing every subsequent inbound key behind the
  CANCEL's `writer.drain()`, which under VM-network jitter caused
  the "web browsing is very sluggish" symptom: each arrow press
  paid a per-key round-trip. CANCEL now schedules via
  `run_coroutine_threadsafe`; ordering vs any SPEAK reaction to the
  same key is still preserved because `writer.write()` buffers in
  scheduling order.
- **Bounded outbound buffer with drop counter.** `RemoteTransport.send`
  now checks `transport.get_write_buffer_size()` and drops the frame
  (incrementing a counter) when over 256 KiB. Stops unbounded
  backlog on congested links, which previously cascaded into
  drain-backpressure on every producer. The first drop and every
  50th drop are surfaced via the status callback.
- **Done-callback on every scheduled send.** New `_schedule_send`
  helper in the extension wraps `run_coroutine_threadsafe` and adds
  a done-callback so transport-side exceptions are logged with the
  kind of message that failed (`speech` / `cancel` / `key`), not
  silently swallowed.
- **Coalesce identical back-to-back outbound speech.** Orca emits
  the same string twice in a few legitimate flows (caret-moved +
  name-changed, focus-of-focus); the master's NVDA queues and speaks
  both. Host mode now compares last-sent text and skips a repeat.
  Reset on disable/disconnect so a reconnect doesn't accidentally
  swallow a fresh first utterance.
- **`pause_speech` inbound is now handled.** Treated the same as
  `cancel` (screen-reader use wants "stop now," not pause/resume).
  Previously we recognized the constant but never acted on it.
- **Bigger reader limit.** `asyncio.open_connection(limit=1MiB)` so
  a legitimate huge speak/braille frame doesn't trip
  `LimitOverrunError`. Default was 64 KiB.
- **Settings file written with 0o600.** Channel key (a shared
  passphrase) used to land at default umask -- usually 0o644.
  Created via `os.open(..., 0o600)`; an existing 0o644 file is
  tightened on next save.
- **Non-string sequence-item counter.** `extract_speech_text` now
  returns `(text, dropped)`. The extension accumulates the dropped
  count for the session and logs the total on disable, so a user
  with NVDA-side speech commands (LangChange, IndexCommand) can see
  what's being lost.

## 0.4.4 -- 2026-05-21

Master-queue cancel: send MSG_CANCEL to the master on every inbound
PRESS.

Root cause this addresses: 0.4.3 added a proactive local interrupt
on the slave, but the user is hearing speech through NVDA on the
master, and NVDA holds its own speech queue of every `speak`
message it has received. Cancelling the slave's speechd has no
effect on that queue, so pressing Ctrl on the master and arrowing
fast both left the queue draining at its own pace.

NVDA Remote v2.x's `cancel` wire message is the signal to flush
that queue. The slave now sends `{"type":"cancel"}` outbound
*before* synthesizing the key, on every inbound PRESS. The
existing local SpeechManager.InterruptSpeech idle callback stays
in place so the slave's own speechd is also cancelled
deterministically.

Send is inline-`await`ed inside `_handle_inbound_key` (now async)
so the CANCEL is strictly ordered ahead of any SPEAK we generate
by reacting to the same key.

## 0.4.3 -- 2026-05-21

Third round of host-mode fixes. Replaces the 0.4.2 tap-detection
with straight pass-through (per user request: slave behavior
should follow the slave's own layout, not the master's NVDA
layout) and adds two new defenses.

- **Locking keys pass through.** Removed the tap-vs-modifier
  detection added in 0.4.2. Caps Lock / Num Lock / Scroll Lock
  are now synthesized straight to the X server; a tap toggles
  the lock state as on any normal keyboard. The slave's caps
  lock is whatever the user has toggled it to, regardless of
  which layout NVDA is using on the master. If an NVDA-laptop
  modifier chord accidentally leaves the slave's caps lock on,
  one more tap clears it.
- **Strict autorepeat dedupe.** 0.4.2 dropped duplicate PRESS
  events only while an Orca modifier was held. User reports
  that even a single physical Insert+R press still loops the
  OCR "Recognizing." command, which means NVDA Remote can send
  duplicate PRESS frames for a single keystroke even without
  autorepeat. The dedupe now drops ANY PRESS for a keysym
  already in `_pressed_keysyms`. Cost: held-key autorepeat for
  typing (e.g. holding 'a' to fill a text field) no longer
  works over the link; tap each key instead.
- **Proactive speech interrupt on every inbound PRESS.** Orca's
  natural interrupt-on-key path (`KeyboardEvent._present`)
  should also fire on XTest-synthesized events, but under VM
  AT-SPI load with a backed-up speech-dispatcher queue the
  interrupt lags noticeably -- Control no longer silences
  speech, quick-arrow no longer cuts off the previous
  utterance. We now call `SpeechManager.InterruptSpeech` from
  the asyncio thread the moment a PRESS arrives, which mirrors
  the local feel.

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
