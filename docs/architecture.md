# Orca Remote — architecture

This file is a tour of how the extension is laid out and **why** it
is laid out that way. For the on-wire message vocabulary, see
[wire-protocol.md](wire-protocol.md). For symptom-to-fix
debugging, see [troubleshooting.md](troubleshooting.md).

## Module layout

| File | Purpose |
|------|---------|
| `remote.py` | `RemoteExtension` subclass: lifecycle, commands, GLib↔asyncio marshalling, state machines. |
| `transport.py` | Single-channel asyncio TLS client with fingerprint pin, reconnect backoff, write-buffer backpressure guard. |
| `protocol.py` | NVDA Remote v2.x wire (newline-JSON), message constants, encode/decode helpers. |
| `keymap.py` | Windows VK → X11 keysym table. Pure data + lookup. |
| `braille_table.py` | US computer braille ASCII → cell byte table + Unicode braille block passthrough. |
| `settings_dialog.py` | Non-blocking Gtk dialog for relay host / port / channel / fingerprint / role. |
| `remote_menu.py` | Non-blocking Gtk dialog: the state-aware Orca+Ctrl+R menu. |
| `__init__.py` | Thin re-export so Orca's loader can find `RemoteExtension`. |

## Threads

Three threads are in play:

1. **GLib main thread.** Orca lives here. Every `controller.*` call
   (speech, clipboard, modal mode), every `Atspi.*` call, every
   Gtk widget operation MUST run here. The extension's command
   handlers fire here.
2. **asyncio loop thread.** Started on extension enable, daemon.
   Owns the single TLS connection to the relay. Every wire send
   and every wire receive happens here.
3. **Speech path thread.** `speechdispatcherfactory.SpeechServer.speak`
   is invoked from various threads depending on the speech
   engine; assume it's GLib-thread but don't rely on it.
   `_on_speech_emitted` defensively marshals to asyncio.

The thread-crossing rules:

- **GLib → asyncio**: use `asyncio.run_coroutine_threadsafe(coro, loop)`.
  Always thread the resulting Future through
  `RemoteExtension._schedule_send` so a `add_done_callback` logs
  transport errors instead of swallowing them.
- **asyncio → GLib**: use `GLib.idle_add(callback, *args)`. Every
  inbound message handler that touches the controller, clipboard,
  or any Gtk widget does this. The callback signature must return
  `False` to be one-shot.
- **Speech path → asyncio**: same as GLib → asyncio. Don't block
  the speech path; speech is on the critical interactive path.

## Message flow

### Inbound from relay

```
relay ──TLS──► RemoteTransport._connect_and_read (asyncio)
            ── reader.readline ──► protocol.decode
            ── await self._on_message ──► RemoteExtension._on_message
                  ├── MSG_SPEAK / MSG_CANCEL / MSG_PAUSE_SPEECH
                  │     └── GLib.idle_add(InterruptSpeech) / _say_async
                  ├── MSG_KEY (host mode)
                  │     ├── _schedule_send(MSG_CANCEL)  [fire-and-forget]
                  │     ├── GLib.idle_add(InterruptSpeech)
                  │     └── GLib.idle_add(synth keysym, pressed)
                  ├── MSG_SET_CLIPBOARD_TEXT
                  │     └── GLib.idle_add(controller.set_clipboard_text)
                  └── MSG_CHANNEL_JOINED / MSG_CLIENT_LEFT / MSG_MOTD
                        └── _say_async / log
```

Critical property: `_on_message` itself is `async` but should
return quickly. The inline `await` on `_handle_inbound_key`
sending CANCEL was the cause of the "web browsing is sluggish"
bug pre-0.5.0 — every inbound key serialized behind a drain. Now
the CANCEL is `_schedule_send`'d (which is fire-and-forget) and
the read loop never waits on outbound writes.

### Outbound from extension

```
SpeechServer.speak ──► controller.emit_speech_emitted
                    ── (each subscriber) ──► RemoteExtension._on_speech_emitted (GLib)
                            ├── coalesce dup
                            └── _schedule_send(MSG_SPEAK)
                                  └── asyncio.run_coroutine_threadsafe
                                        └── transport.send (asyncio)
                                              ├── buffer-size guard (drop if congested)
                                              └── writer.write + drain

braille.refresh ──► controller.emit_braille_emitted
                 ── RemoteExtension._on_braille_emitted (GLib)
                       ├── frame dedup (text, cursor_cell)
                       ├── braille_table.text_to_cells
                       ├── (first frame) _schedule_send(MSG_SET_BRAILLE_INFO)
                       └── _schedule_send(MSG_DISPLAY)
```

Both paths assume the underlying perf-branch `emit_*` hooks exist
on the controller; `subscribe_*` calls are wrapped in
`try/except AttributeError` so the extension degrades silently on
an older Orca.

## State, and where it lives

`RemoteExtension` is the only stateful actor. Categories of state:

**Settings (persisted):**

- `_settings` — dict mirrored to `~/.local/share/orca/orca-remote-settings.json`
  (mode 0o600). Host, port, channel key, fingerprint, role,
  `auto_connect`.

**Transport (asyncio-thread):**

- `_loop`, `_loop_thread`, `_transport`.
- `RemoteTransport._writer`, `_stop_event`, `_task`,
  `_dropped_outbound`.

**Wire-state (GLib-thread, read by asyncio):**

- `_pressed_keysyms` — keysyms synth'd PRESS for but not yet
  RELEASE. Drained on disconnect/disable so a force-killed VM is
  never the only way out of a stuck modifier.
- `_announced_join` — true after the first `channel_joined` for
  the current session intent; reset by explicit
  connect/disconnect/disable. Prevents reconnect spam.
- `_last_outbound_speech`, `_last_outbound_braille`,
  `_sent_braille_info` — coalesce / dedup sentinels.
- `_focus_on_remote` — master-side: hearing the slave?
- `_mirror_speech`, `_mirror_braille` — host-side: emitting to
  the master?

**Counters (debug visibility):**

- `_dropped_nonstring_items` — count of inbound speak sequence
  items that were not plain strings (LangChange / IndexCommand
  etc.). Logged on disable.

## Why the choices we made

### Fingerprint pin instead of CA trust

The public relay (`nvdaremote.com`) uses a self-signed cert; even
self-hosters often do. CA trust would either be useless (everyone
self-signs) or require manual CA install on every client. SHA-256
pin gives the same guarantee as TOFU after first connect, but
makes the first-connect bootstrap explicit (you have to paste the
fingerprint), which avoids the silent-on-bootstrap surprise of
TOFU.

### Non-blocking settings dialog

A remote master in host mode can synthesize Orca+Ctrl+R, which
opens our settings. If that dialog used `Gtk.Dialog.run()` (a
nested GLib main loop), the GLib thread would block until a local
user clicked something. Real lock-up. The dialog is built around
the `response` signal callback so the main loop keeps running.

### Stuck-key drain on disconnect

`Atspi.generate_keyboard_event` goes through XTEST. A PRESS
without a RELEASE outlives the process — the X server believes
the key is held. We track every synth'd PRESS in `_pressed_keysyms`
and synthesize matching RELEASEs on `_stop_transport` and
`disable`. Before this safety net a dropped connection mid-pair
left the slave's modifier stuck.

### Locking keys pass through (don't drop)

0.4.1 dropped Caps_Lock / Num_Lock / Scroll_Lock unconditionally
to stop the XTest "press = toggle" foot-gun, which also killed
legitimate taps. 0.4.3 reverted to pass-through with strict
autorepeat dedupe: a tap toggles, NVDA-laptop-modifier chord
usage produces extra toggles the user can untoggle with another
tap. Slave's lock state follows the slave's keyboard, not the
master's NVDA layout.

### Own-chord refusal in host mode

If the master sends Orca+Ctrl+R, XTest delivers it on the slave,
Orca's input listener picks it up, and we open the settings
dialog on the slave (not the master). Same for Orca+Ctrl+Page
Up/Down (transport bounce) and Orca+Alt+Tab (focus toggle). The
extension drops these chords on the synth side using
`_pressed_keysyms` to detect when modifiers are held.

### Fire-and-forget outbound CANCEL

Pre-0.5.0 the inbound key handler `await`ed
`transport.send({"type":"cancel"})`. Each web-browsing arrow key
paid a per-key writer-drain round-trip; under VM-network jitter
this was the "very sluggish" symptom. 0.5.0 schedules CANCEL via
`_schedule_send` (which fire-and-forgets via
`run_coroutine_threadsafe`); ordering vs SPEAK reaction to the
same key is still preserved because `writer.write()` buffers in
call order.

### Bounded outbound buffer

`RemoteTransport.send` checks `writer.transport.get_write_buffer_size()`
and drops if over 256 KiB. Stops unbounded backlog when the relay
or its TCP path is congested. Letting drain() block here would
cascade backpressure into every producer (speech_emitted, inbound
key reaction).

### US computer braille for outbound mirroring

We need NVDA's braille viewer to show legible content. NVDA Remote
v2.x's `display` carries raw cell bytes; we have a TEXT string
(from the perf-branch `braille_emitted` hook). The simplest robust
translation is a static ASCII → cell table. English text renders
correctly; non-Latin scripts come through as blank cells. A
liblouis-backed translation can drop in behind `text_to_cells`
without changing the wire layer.

## Deferred work

Items the user has explicitly asked for that aren't in current
releases, with the design constraint:

### Master-side key forwarding (Orca master → slave)

Needs either:

1. A new "consume keyboard event" subscribe API on the perf-branch
   controller. Orca's input event manager calls this BEFORE
   `event.process()`; the subscriber returns True to swallow the
   event locally and forward it on the wire. ~150 lines in
   `input_event_manager.py` + dispatch surface in `dbus_service.py`.
2. Or, on master-mode toggle, register `Atspi.Device.add_key_grab`
   for every (keysym, modifier_mask) combination we want to forward.
   Combinatorial in modifier combos (~600 grabs for full Latin
   coverage), and grab register/unregister is not free.

Option 1 is the right design. Tracked for a future session.

### Inbound braille rendering (master plays remote braille)

The host→master direction works (master's NVDA viewer or, in
principle, another Orca master, can display incoming cells). But
to render an incoming braille frame onto a local BrlAPI display
on a master, we'd need a "push braille text" API on the
controller that bypasses `braille.refresh`'s region-stack walk.
Another perf-branch hook. Tracked.

### File transfer

NVDA Remote v2.x has no file-transfer message. The user's
strategic decision was "skip — keep v2 wire" (file transfer would
either be incompatible with NVDA on Windows or require upgrading
the whole stack to NVDA Remote v3 protocol). Use `scp` / `rsync`.

### Liblouis-backed braille translation

The static ASCII→cell table is English-only. A liblouis
translator would let us send UEB Grade 2, language-tagged
contractions, and non-Latin scripts correctly. Liblouis is a
heavy dep (C library + Python bindings); not in scope for a
zero-dep extension. Future opt-in via a setting.
