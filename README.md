# Orca Remote

NVDA-Remote-compatible remote access for
[Orca](https://gitlab.gnome.org/GNOME/orca), packaged as a user
extension (`.orca-ext`). Bidirectional speech mirroring, host-mode
key injection (NVDA master → Orca slave), bidirectional clipboard
sync, and host-mode braille mirroring.

This README covers install, configure, and the shortcuts. Deeper docs:

- [docs/architecture.md](docs/architecture.md) — threads, message
  flow, why the design is the way it is.
- [docs/wire-protocol.md](docs/wire-protocol.md) — every message
  type we send/receive, NVDA Remote v2.x compatibility matrix.
- [docs/troubleshooting.md](docs/troubleshooting.md) — known
  symptoms (web sluggishness, stuck keys, VM freezes) and their
  fixes.

## Feature matrix

| Direction              | Speech | Braille          | Keys    | Clipboard |
|------------------------|--------|------------------|---------|-----------|
| Orca host → NVDA master| YES    | YES (Latin-only) | n/a     | OUT-OK    |
| Orca host → Orca master| YES    | YES (Latin-only) | n/a     | OUT-OK    |
| NVDA master → Orca host| n/a    | n/a              | YES     | IN-OK     |
| Orca master → Orca host| n/a    | YES (inbound)    | PARTIAL | IN-OK     |
| Orca master → NVDA host| n/a    | YES (inbound)    | PARTIAL | IN-OK     |

**YES** = implemented and exercised. **NO** = not yet implemented;
gap is documented in [docs/architecture.md](docs/architecture.md)
under "Deferred work."

Notes:

- **Braille mirroring** out from a host uses a US computer braille
  ASCII→cell table, so English text renders correctly on a master's
  braille viewer. Non-Latin scripts come through as blank cells.
- **Master-side key forwarding** (Orca user typing on the Linux
  master to control a Windows / Linux slave) ships full system-
  level consume as of 0.7.0 via the vendored
  [orca-ext-utils](https://github.com/churst90/orca-ext-utils)
  KeysetGrab: forwarded keys reach the slave only, not also the
  focused local app. Partial coverage on Wayland compositors that
  refuse some AT-SPI grabs (the refused count goes to the debug
  log). While forwarding is active, the five orca-remote command
  chords (see "Shortcuts" below) bypass forwarding and dispatch
  locally so the user can always quit / mute / switch sides; all
  other Orca / NVDA chords (Insert+Down, Insert+T, etc.) still
  forward to the remote so its screen reader can act on them.
- **File transfer** is not in scope — the v2 wire has no
  file-transfer message, and a custom one would lose NVDA interop.

## Requirements

- Orca built from `orca-perf` (this is the user's working branch;
  upstream lacks the `speech_emitted` / `braille_emitted` /
  `synthesize_key_event` hooks the extension depends on).
- Python 3.12 or newer. Standard library only (`asyncio` + `ssl`);
  no third-party dependencies for the extension itself.
- A reachable NVDA Remote v2.x relay. Public default is
  `nvdaremote.com:6837`. Self-hosting is straightforward — see
  [docs/architecture.md](docs/architecture.md) for the wire
  contract.

## Install

```sh
./build-orca-ext.sh .
orca --install-extension remote.orca-ext
```

`./build-orca-ext.sh .` reads `manifest.toml` for the output name
and produces `remote.orca-ext` in the current directory. The
`orca --install-extension` step registers it; Orca prompts to enable
it on the next launch.

## Shortcuts

The 0.8.0 release dropped the popup menu in favor of direct
keyboard shortcuts. All five chords work while master-mode key
forwarding is active (they're on the bypass list so they dispatch
locally instead of being forwarded to the remote).

- **Orca + Ctrl + R** — open the settings dialog.
- **Orca + Ctrl + M** — client only; toggle inbound mute. Drops
  speech AND braille from the slave without dropping the
  connection ("stop hearing them move around while I work").
  Independent of the focus toggle: a muted state persists across
  Orca+Alt+Tab toggles.
- **Orca + Ctrl + Page Up** — connect (or open settings if no
  relay configured yet).
- **Orca + Ctrl + Page Down** — disconnect.
- **Orca + Alt + Tab** — client only; toggle "focus on remote."
  Activates / releases the KeysetGrab and the inbound speech
  pipeline together. Announcement includes the mute state on the
  way back to remote: "focused on remote machine, muted" if
  mute is still set.

## Configure

Settings dialog fields (Orca+Ctrl+R opens it):

- **Relay host** — default `nvdaremote.com`.
- **Relay port** — default `6837`.
- **Channel key** — the shared passphrase agreed on by both peers.
  This is the same value you type into NVDA Remote's "Connect"
  dialog on the Windows side.
- **Server fingerprint (SHA-256)** — the SHA-256 of the relay's
  TLS certificate, lowercase hex. **Required.** See "First
  connect" below.
- **Role** — Client (receive speech / control remote) or Host
  (broadcast speech, accept inbound keys).

Settings persist to `$XDG_DATA_HOME/orca/orca-remote-settings.json`
(typically `~/.local/share/orca/orca-remote-settings.json`). The
file is created with `0o600` perms — the channel key is a shared
secret.

## First connect (fingerprint bootstrap)

Orca Remote pins the relay's certificate by SHA-256 fingerprint —
no CA trust, no first-connect TOFU. On the first attempt with an
empty fingerprint field, the connection is refused and Orca speaks
the fingerprint it actually saw AND copies it to your clipboard.
Press Orca+Ctrl+R, focus "Server fingerprint", and paste with
Control + V.

If the relay rotates its certificate later, you'll get the same
"server fingerprint did not match" announcement — verify the new
value out of band (`openssl s_client` works), then update the
setting.

Pre-fetch the fingerprint from the shell:

```sh
openssl s_client -servername nvdaremote.com -connect nvdaremote.com:6837 \
    < /dev/null 2>/dev/null \
  | openssl x509 -fingerprint -sha256 -noout \
  | sed 's/SHA256 Fingerprint=//; s/://g' \
  | tr '[:upper:]' '[:lower:]'
```

## Pairing scenarios

### Linux host, Windows master (NVDA helper)

You want a sighted (or screen-reader) helper on Windows to hear
your Linux Orca and control your machine.

1. Both peers: agree on a channel key.
2. Linux: Orca + Ctrl + R → Settings…, set Role = Host, paste
   relay host/port + channel key + fingerprint, Save.
3. Windows: open NVDA Remote → "Allow this machine to control
   another", enter same relay + channel key.
4. Both peers should hear "connected." The Windows side now hears
   your Orca speech and sees your braille buffer. Their keystrokes
   land on your machine.

To temporarily silence the speech mirror without disconnecting:
Orca + Ctrl + R → "Mute outbound speech mirror".

### Linux master, NVDA host (you control NVDA)

Listen to a remote Windows machine running NVDA. Note that
master-side key forwarding from Orca isn't implemented yet, so
this is listen-only.

1. Both peers: agree on a channel key.
2. Linux: Orca + Ctrl + R → Settings…, set Role = Client, fill
   relay + channel + fingerprint.
3. Windows: NVDA Remote → "Control another machine".
4. You'll hear NVDA's speech locally. To briefly mute the inbound
   stream while you work on your own machine: Orca + Alt + Tab.

### Two Orca machines

Same as above. The extension wire format is NVDA Remote v2.x for
compatibility, so two Orca peers work with the public relay or
any self-hosted v2 relay.

## Testing

Pure-function unit tests covering protocol parsing, VK translation,
and braille cell mapping:

```sh
python3 -m pytest tests/
```

Live wire driver for slave-side key injection smoke tests:

```sh
python3 tests/fake_master.py <channel> <fingerprint> --type "hello"
```

See `tests/fake_master.py --help` for options. Requires an actual
slave-mode Orca on the same channel.

## License

LGPL-2.1-or-later. See `LICENSE`.

The wire-protocol layout (newline-JSON frames, message vocabulary,
channel-key join semantics) is compatible with NVDA Remote v2.x
but the implementation here is a clean-room rewrite; no code is
lifted from the NVDA Remote project (GPL-2.0).
