# AIROX Connect protocol (version 1)

AIROX Connect links a phone or tablet to an AIRUX PC on the same network. The PC side is
`airox-connectd` (`connect/airox_connect/` in AIRUX); the Android app is its own repository,
[AIROX_Connect](https://github.com/Absurd28/AIROX_Connect). A later
iOS app speaks this same protocol.

## Trust and transport

- **Off by default.** The PC listens only after the user turns on *Allow phone connections* in the
  AIROX Connect window.
- **Pairing link** (shown as a QR code on the PC, valid 120 s, single use):

  ```text
  airox://pair?h=<LAN IPv4 addresses, comma separated>&p=<port>&fp=<SHA-256 of the certificate>&c=<code>&n=<PC name>&id=<PC id>
  ```

- **TLS 1.3** to the PC's self-signed ECDSA P-256 certificate. The client trusts the certificate
  only if the SHA-256 of its DER bytes equals `fp` from the pairing link (no CA, no trust on first
  use). Host names are not checked; the pin is the identity.
- **Device token.** Every request except `/v1/hello` and `/v1/pair` carries
  `Authorization: Bearer <device_id>.<token>`. The PC stores only the token's SHA-256; the phone
  keeps the token encrypted with an Android Keystore key. Removing a device on the PC revokes it.
- **LAN only.** Requests from addresses that are not private (RFC 1918, ULA), link-local or
  loopback are dropped before TLS. 100.64.0.0/10 is refused too.
- **Rate limit.** Ten failures (bad token, bad code) from one address in 60 s block it for 60 s.
- **Broker.** Every action runs through AIRA's permission broker. Actions that need confirmation
  send a `confirm` message to the phone that asked; an answer from any other device is ignored;
  no answer in 120 s, or no connected phone, means *denied*. Actions started at the PC are
  confirmed at the PC.

Port 8766 is HTTPS and WebSocket; UDP 8767 is discovery; port 8768 serves the APK over plain HTTP
only while *Share the app* is on (10 minutes).

## Pairing

`POST /v1/pair` `{"code", "name", "model", "platform"}`. It waits (up to 120 s) until the person at
the PC clicks **Allow** (in the window or in the notification). Answers:

- `200 {"device_id", "token", "pc": {id, name, fingerprint, port, addresses, macs, version, protocol, user}}`
- `403 {"error"}` when the code is wrong, expired or used, or the PC denied the device.

## Discovery

When the saved address stops answering, the phone broadcasts the UDP datagram `AIROX?<pc id>` to
port 8767. Only the PC with that id answers, with `AIROX!<pc id> <https port>`, from its current
address.

## REST (JSON unless noted)

| Method and path | What |
| --- | --- |
| `GET /v1/hello` | `{"app": "airox-connect", "protocol": 1}`, no token |
| `GET /v1/info` | PC info, this device, `features`, AIRA link and input state |
| `GET /v1/status` | `summary` (cpu %, memory %, GPU, uptime, PC battery), AIRA busy |
| `GET /v1/activity` | running apps, AIRA's recent transcript, recent actions, Phagos status |
| `GET /v1/tasks/processes` | Task Manager groups (app, background, system) with their processes |
| `GET /v1/tasks/performance` | CPU, memory, disks, network, GPUs, each with a 60-sample history |
| `GET /v1/tasks/services` | `{"system": [...], "user": [...]}` |
| `GET /v1/tasks/startup` | autostart apps |
| `GET /v1/tasks/users` | sessions and per-user use |
| `POST /v1/run` | `{"scope": "taskmgr" \| "connect", "tool", "args"}` -> `202 {"rid"}`; the result arrives on the WebSocket as `{"t": "result", "rid", "ok", "allowed", "decision", "risks", "data", "error"}` |
| `GET /v1/files/list?path=~/...&hidden=0` | `{"path", "parent", "items": [{name, path, dir, size, mtime, kind, mime}]}`; home folder only |
| `GET /v1/files/download/<tid>` | the file; `Range: bytes=a-b` gives `206` (resume) |
| `GET /v1/files/hash/<tid>` | `{"sha256"}` of an offered file |
| `GET /v1/files/incoming` | files the PC sent and the phone has not fetched |
| `POST /v1/files/done/<tid>` | the phone has the file |
| `GET /v1/files/upload/<uid>` | `{"received": bytes}` so far |
| `PUT /v1/files/upload/<uid>?name&size&sha256&start` | raw bytes from `start`; `409` says where to resume; when complete the SHA-256 is checked and the file is saved in `~/Downloads/AIROX Connect/` under a new name if one exists |
| `GET /v1/clipboard` | `{"data": {"text"}}` |
| `GET /v1/screen/shot` | `image/jpeg` of the whole screen |
| `POST /v1/aira/voice` | body `audio/wav` (16 kHz mono, up to 8 MB): a spoken command |
| `GET /v1/aira/speech?name=aira-say-<n>.wav` | AIRA's spoken reply (Kokoro) for the phone to play |

Tools for `POST /v1/run`:

| Scope | Tool | Arguments | Risk (asks first?) |
| --- | --- | --- | --- |
| taskmgr | `end_task` | `pid`, `start`, `force` | stop (yes); + root for another user's process |
| taskmgr | `end_app` | `key`, `force` | stop (yes) |
| taskmgr | `set_priority` | `pid`, `start`, `nice` | write, or root to raise (yes) |
| taskmgr | `service` | `name`, `action`, `scope` | stop/root (yes); starting a user service: write |
| taskmgr | `startup_app` | `entry`, `enabled` | write; overwrite when it rewrites your own file (yes) |
| connect | `send_files` | `paths` | send_off_machine (yes) |
| connect | `clipboard_set` | `text` | write |
| connect | `open_link` | `url` (http/https) | send_off_machine (yes) |
| connect | `media` | `action` (play_pause, next, previous, stop, volume_up, volume_down, mute, set_volume), `level` | write |
| connect | `power` | `action` (lock, sleep, restart, shutdown) | lock: write; others: stop (yes) |
| connect | `remote_input` | `action` (start, stop) | write; KDE asks the person at the PC once |

`pid` and `start` (the process's start time in clock ticks) together name one process, so a
reused process id is never ended by mistake.

## Events WebSocket: `GET /v1/ws`

Text frames, JSON. From the PC:

- `hello` `{pc, aira, busy, input, time, transcript}`
- `aira` `{event, ...}`: AIRA's shell events, relayed: `you` (`via`), `thinking`, `aira` (`text`,
  `tags`, `calls`), `idle`, `spoken` (`voice.wav`), `confirm` (AIRA's own: `id`, `tool`, `args`,
  `risks`), `confirm_done`, `link` (`up`), `error`. After a reconnect the shell replays its recent
  history, so clients drop duplicates (same `ts` and text).
- `confirm` `{id, source, tool, args, risks, summary, strong, timeout}`: a broker confirmation for
  this phone. `strong` asks the app to require the screen lock or biometrics before yes.
- `confirm_done` `{id, yes, why}`
- `result` (see `/v1/run`), `saved` `{name, path}` (an upload was saved)
- `incoming` `{files: [{tid, name, size, mime, origin}]}`: the PC sends files
- `notification` `{app, title, body, urgency}`: desktop notifications (AIRA, Phagos, Drivers,
  Task Manager; all of them if the user chose so)
- `input` `{state}`: remote input ready, denied, closed or error
- `ring`: AIRA was asked to ring the phone
- `pong` `{ts, server}`

From the phone:

- `say` `{text}`: a typed command for AIRA
- `answer` `{id, yes, src}`: `src: "aira"` answers AIRA's own confirmation, otherwise a broker one
- `input` `{op: "start"}` or `{op: "pointer", dx, dy}`, `{op: "button", button: left|right|middle, down}`,
  `{op: "scroll", dx, dy}`, `{op: "key", keysym, down}`, `{op: "text", text}`
- `status` `{battery, charging}`
- `ping` `{ts}`

## Screen WebSocket: `GET /v1/screen/stream?source=virtual|monitor&fps=60&kbps=12000`

- `virtual`: KDE's "New Virtual Output" (a second screen, 1920x1080, placed right of the others);
  `monitor`: a mirror of a screen. The first time, KDE asks the person at the PC to choose and allow
  it; the choice is remembered.
- Binary frames: 8 bytes big-endian PC time in microseconds when the frame was ready, then one
  H.264 access unit (Annex B, no B-frames, SPS and PPS before every key frame).
- Text frames from the PC: `stream` `{ev: "stream", output, width, height, encoder, virtual}`,
  `stream` `{ev: "error" | "ended", error}`, `pong`.
- Text frames from the phone: `touch` `{phase: down|move|up, x, y (0..1), press}`, `button`, `scroll`,
  `key`, `text` (as above), `ping` `{ts}`.
- If the phone falls behind (more than a quarter second of frames waiting), frames are skipped
  until the next key frame, so delay does not build up.
