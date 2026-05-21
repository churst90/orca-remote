# Changelog

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
