# Orca Remote

NVDA-Remote-compatible remote access for [Orca](https://gitlab.gnome.org/GNOME/orca),
packaged as a user extension (`.orca-ext`).

**Stage 1 scope (this release):** client-only receive-speech mirror.
Connect outbound to an NVDA Remote v2.x relay (e.g. `nvdaremote.com`),
join a channel by shared key, and speak any inbound speech locally
through Orca. This is enough to pair a Linux/Orca machine with a
Windows/NVDA machine that has the NVDA Remote add-on installed --
the Windows side broadcasts its speech as the "slave", and your
Linux Orca speaks it as the "master".

**Not in Stage 1:**

- Host (be-controlled) mode -- you can't yet *be* the slave.
- Key forwarding -- you can't yet drive the remote machine from
  your local Orca.
- Braille mirroring, clipboard sync, tones.
- Self-hosted relay docs (works fine against any NVDA Remote v2.x
  relay; just not documented here yet).

Stages 2 and 3 will fill these in.

## Requirements

- Orca built from `orca-perf` (or a future stock Orca that ships
  the user-extension framework upstream).
- Python 3.12 or newer (Stage 1 uses `asyncio` + `ssl` from stdlib;
  no third-party dependencies).
- A reachable NVDA Remote v2.x relay. The public default is
  `nvdaremote.com:6837`.

## Install

```sh
./build-orca-ext.sh .
orca --install-extension remote.orca-ext
```

The first command builds `remote.orca-ext` from the current directory.
The second registers it; Orca prompts to enable it on next launch.

## Configure

Once enabled, press **Orca + Shift + R** to open the settings dialog.
Fields:

- **Relay host:** default `nvdaremote.com`.
- **Relay port:** default `6837`.
- **Channel key:** the shared passphrase agreed on by both peers.
  This is the same value you type into NVDA Remote's
  "Connect" dialog on the Windows side.
- **Server fingerprint (SHA-256):** the SHA-256 of the relay's TLS
  certificate, lowercase hex. **Required.** See "First connect" below.

Settings persist to `$XDG_DATA_HOME/orca/orca-remote-settings.json`
(typically `~/.local/share/orca/orca-remote-settings.json`).

## First connect (fingerprint bootstrap)

Orca Remote pins the relay's certificate by SHA-256 fingerprint --
there is no CA trust and no first-connect TOFU. On the first
attempt with an empty fingerprint field, the connection is refused
and Orca speaks the fingerprint it actually saw. Open the settings
dialog again (Orca + Shift + R), paste that value into "Server
fingerprint", and save. Subsequent connects will succeed as long
as the fingerprint still matches.

If the relay rotates its certificate later, you'll get the same
"server fingerprint did not match" announcement -- verify the new
value out of band, then update the setting.

You can also fetch the fingerprint ahead of time:

```sh
openssl s_client -servername nvdaremote.com -connect nvdaremote.com:6837 < /dev/null 2>/dev/null \
  | openssl x509 -fingerprint -sha256 -noout \
  | sed 's/SHA256 Fingerprint=//; s/://g' | tr '[:upper:]' '[:lower:]'
```

## How it works

- On enable, the extension starts a daemon thread running an
  `asyncio` event loop.
- That loop holds one outbound TLS connection to the relay,
  reconnecting with exponential backoff on failure.
- After TLS handshake, the extension sends a `protocol_version`
  frame followed by a `join` frame with `connection_type=master`.
- Inbound `speak` messages have their text extracted and routed
  back to Orca's main thread via `GLib.idle_add` -- the actual
  TTS hand-off uses `controller.present_message_internal`.

## Roadmap

- **Stage 2:** host mode (be controlled), Orca command-key forwarding
  via `controller.enter_modal_mode`. Requires upstream `speech_emitted`
  signal so outbound speech can be tapped without monkey-patching.
- **Stage 3:** braille buffer mirroring, clipboard push, tone/beep
  forwarding, self-hosted relay docs.

## License

LGPL-2.1-or-later. See `LICENSE`.

The wire-protocol layout (newline-JSON frames, message vocabulary,
channel-key join semantics) is compatible with NVDA Remote v2.x but
the implementation here is a clean-room rewrite; no code is lifted
from the NVDA Remote project (GPL-2.0).
