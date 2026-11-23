# HandsRelay v5

Let a personal AI agent operate a Windows PC — screenshots,
mouse, keyboard — with a security posture designed for leaving it reachable
while the owner is away from home.

HandsRelay v5 has three parts:

| Part | Runs where | What it does |
| --- | --- | --- |
| **hands** | Windows host, native (never in Docker) | The desktop driver: screenshot, mouse, keyboard, window control. A container has no interactive Windows desktop session, so the hands must live on the host — the same constraint every desktop agent (UFO², OmniTool) runs into. |
| **gateway** | Docker or native Python | The agent-facing API: token auth, failed-guess lockout, routing of screen/action calls to the hands and parse calls to the parser. This is the only surface the agent talks to. |
| **parser** | Docker or native Python (GPU optional) | Screen parsing (OmniParser-class): screenshot in → labeled, numbered UI elements out, for surfaces with no DOM and no UI Automation tree (games, canvas, custom UI). Optional component; the relay works without it. |

The project roadmap is in [docs/ROADMAP.md](docs/ROADMAP.md); item dispositions and verification limits are in [docs/COMMISSION-CLOSEOUT.md](docs/COMMISSION-CLOSEOUT.md).

## What's new in v5

v5 adds two input changes on top of v4. Default typing behavior is unchanged.

- **Real-key typing (opt-in).** `POST /type` accepts `method: "real_keys"`, for fields that reject paste. Each character is sent through Windows `SendInput` as a real key down and key up, with Shift wrapped around shifted characters and a Unicode down/up pair for characters that have no key on the active layout. It never touches the clipboard and never falls back to paste. Timing is a fixed 0.02 s per character, with no jitter. Off Windows the method answers `501 real_keys_unavailable`.
- **Session-wide switch.** The Settings entry "Type with real key presses instead of pasting" (`type_real_keys`, off by default; environment variable `HANDS_REAL_KEYS`) makes `method: "auto"` use real keys. A method named in the request still wins.
- **Owner input still wins.** Keyboard or mouse input from the person at the PC or a locked desktop stops real-key typing. A changed foreground window stops it only when the request carries `expect_focus`, which queued acts pin automatically. A direct `POST /type` or `POST /v1/act` without `expect_focus` keeps typing into whichever window now has focus and returns 200. When typing is stopped before the first character, the request is refused with a 409. If typing had started, the response reports `type_aborted` and `chars_typed`.
- **Clicks land on scaled displays.** The hands process is now explicitly DPI-aware, and each input request thread runs per-monitor-v2, so request coordinates are physical pixels on every monitor. This corrects clicks that missed their target when a monitor was set to 125% or 150% after logon. Screenshot routes use the same physical pixels.

The executable, the autostart entry and `start-v4.bat` keep their `v4` names in this release, so the build still produces `HandsRelay v4.exe`.

## Prerequisites

Use Windows 10/11 with an interactive desktop session and Python 3.11–3.13 on PATH (`python --version`). Open a terminal in the repository root. Building the native app installs dependencies from PyPI; Docker is optional and is only for the gateway and parser. Never run two copies on the same service ports.

## Packaged app (Windows) — the normal way to run it

One icon, one button. Build once:

```
build_app.bat   :: builds "HandsRelay v4.exe"
```

Then just double-click **HandsRelay v4.exe** and press **Start**. The one
window runs all three parts in-process — hands on 127.0.0.1:5000, gateway on
:5010, parser on :5011 — with a status line for each. **Stop** (or closing the
window) stops everything. No other app is needed: the v1 relay is retired,
and the hands run headless inside this app (no separate window, no tunnel —
the gateway is the only surface an agent talks to).

Secrets in the packaged app:

The historical `.HandsRelayV0.1` directory is retained as a compatibility storage path. Upgrades reuse both existing credential files; the folder name is not the product version.

- The **gateway token** persists per machine at
  `%USERPROFILE%\.HandsRelayV0.1\gateway-token.txt` — generated on first
  run, reused after that (delete the file to rotate it), so an agent keeps
  working across relaunches. The window shows it on demand (Show / Copy);
  it is never logged.
- The **hands secret** is owned by the app and persists per machine at
  `%USERPROFILE%\.HandsRelayV0.1\hands-secret.txt` — generated on first run,
  reused after that (delete the file to rotate it). The app feeds the same
  value to the gateway and the hands, so the two can never disagree about
  it the way the old two-app setup could.

## Tray icon and Settings

The packaged app puts a HandsRelay icon in the Windows notification area
(it may sit in the overflow ^). Its tint and corner badge show the state:
grey = stopped, green = running and free (purple badge: an agent is
watching), blue badge = an agent holds the hands, amber = paused by you,
red = the gateway or hands is down; a white dot at top-left means you are
using the PC and the agent is waiting. Hover for the same in words.
Left-click opens the window; right-click offers Open dashboard, Settings,
Pause relay (releases the claim and refuses every agent act until you
resume) and Quit.

Settings live in `%USERPROFILE%\.HandsRelayV0.1\settings.json`
(versioned). Every default is the behaviour from before settings existed.
A damaged file is left in place, the app uses the defaults and shows a
warning in the window. To add a setting, see the docstring at the top of
`relay_settings.py`.

### Agent-capability toggles (all off by default)

Partner ruling 2026-11-15: risky capabilities ship as Settings switches,
off by default; whoever turns one on owns the risk. Each switch shows its
risk note. Safety guards (paste/focus guards, auth lockout) are not
toggles.

- **Batched actions** (`batched_actions`, live). Adds
  `POST /v1/act/batch {"actions": [{"op": ...}, ...], "stop_on_error"?: true}`:
  up to 50 acts run in order through the same path as `/v1/act`. The batch
  stops at the first act refused because you are using the PC (yield) or
  the relay is paused, and, with `stop_on_error`, at any other failure.
  Answer: `{ok, total, completed, stopped_at, stop_reason, results[]}`.
  Off: the route answers 404 exactly like an unknown route. Standalone
  gateway: `HANDSRELAY_BATCHED_ACTIONS=1`.

- **Trajectory recording** (`trajectory_recording`, live). Every act
  forwarded to the hands (by `/v1/act`, `/v1/act/queued` and its retries,
  `/v1/act/batch`) is appended to
  `%USERPROFILE%\.HandsRelayV0.1\trajectories\<YYYYMMDD>\actions.jsonl`
  and every screenshot served by `/v1/screen` is saved beside it as
  `screen-<ms>.png`. Local folder only; nothing is uploaded. **Risk:
  recordings keep everything on screen.** Typed text (`text` of `type` /
  `clipboard_set`) is stored as its length only unless the second switch
  **Also keep the text the agent types** (`trajectory_record_typed_text`)
  is on; then passwords and anything else typed are saved in plain text.
  Key and hotkey names are always kept. Standalone gateway:
  `HANDSRELAY_TRAJECTORY_RECORDING=1`, `HANDSRELAY_TRAJECTORY_TYPED_TEXT=1`,
  `HANDSRELAY_TRAJECTORY_DIR`.

- **Input-injecting smoke test** (`input_injecting_smoke_script`).
  `python tools/smoke_inject.py [--type-into Notepad]` refuses (exit 2)
  unless this switch is on. It checks health and info, claims the hands
  as `smoke-test`, moves the pointer twice and, with `--type-into`, focuses
  that window and types a marker with `expect_focus` pinned to it, then
  always releases the claim. **Risk: it moves the pointer and types.**

- **Custom tools** (`custom_tools_registry`, live). Agents may list
  (`GET /v1/tools`) and run (`POST /v1/tools/<name>/run {"args"?: [...]}`)
  only the programs **you** declare in
  `%USERPROFILE%\.HandsRelayV0.1\tools.json`:
  `{"version": 1, "tools": [{"name": "build", "description": "...",
  "command": ["C:\\tools\\build.bat"], "cwd": "C:\\src",
  "timeout_seconds": 30, "allow_args": false}]}`. Agents cannot add tools;
  nothing is downloaded; the file is re-read on every request. A run is
  `command` (+ `args` if `allow_args`) with no shell, stdin closed, output
  capped at 64 KB, refused while the relay is paused. **Risk: a custom tool
  runs whatever its author wrote, on this PC.** Off: both routes are 404.
  Standalone gateway: `HANDSRELAY_CUSTOM_TOOLS=1`, `HANDSRELAY_TOOLS_FILE`.

- **Invisible agent browser** (`agent_browser_invisible`, live). **Browser
  tasks only, not a full desktop.** The agent starts its own headless Edge
  (else Chrome) with a separate profile in
  `%USERPROFILE%\.HandsRelayV0.1\agent-browser\profile`; it never shows on
  your screen and is driven over the DevTools protocol on 127.0.0.1 (random
  port), never with your mouse or keyboard, so it needs no claim and you keep
  working. Routes (token required, 423 while paused):
  `GET /v1/agent-browser/status`, `POST /v1/agent-browser/{start,stop}`,
  `open {url}` (http/https/about/data), `read {max_chars?, html?}`,
  `screenshot` (PNG base64, also saved under `agent-browser\screenshots`),
  `click {selector}`, `type {selector, text}`. Turning it off kills the
  browser and all routes are 404. Native Windows apps still run on your
  desktop. **Risk: pages it opens are readable to the agent.** Standalone
  gateway: `HANDSRELAY_AGENT_BROWSER=1`.

## UI Automation tree (opt-in, default off)

Settings → Agent capabilities → "Let agents read the Windows UI
Automation tree". **Risk:** the agent can read the names, fields and
labels of the window in front and click or focus them. Off (the
default): both routes answer `403 {"error_code": "uia_disabled"}` and
nothing else changes. Needs the optional `comtypes` package (MIT;
`build_app.bat` installs it), else `501 uia_unavailable`. Running the
services yourself: `RELAY_UIA_TREE=1` (gateway) and `HANDS_UIA_TREE=1`
(hands); both must be on.

- `GET /v1/uia/tree?max_depth=6&max_nodes=400` — the FOREGROUND window
  only. Returns `snapshot` (a token, valid 120 s), `window`, `truncated`
  and `nodes: [{id, depth, name, control_type, rect{x,y,w,h}, enabled,
  focused, automation_id}]`. `id` is the child path (`"0.2.1"`). Depth
  is capped at 20, nodes at 2000, names at 200 characters. Reading does
  not wait for you to stop using the PC (it injects nothing).
- `POST /v1/uia/act {"snapshot", "id", "action": "click"|"focus",
  "button"?, "human"?, "expect_focus"?}` — click re-reads the element's
  live rectangle and clicks it through the normal `/click` (human motion
  inside the element's box); focus uses UIA SetFocus. Like every act it
  yields while you are active (409), refuses on a locked desktop, honours
  `expect_focus`, and is refused while the relay is paused (423). A
  stale snapshot or vanished element answers `409 uia_stale`: read the
  tree again. The hands claim stays advisory, as for `/v1/act`.

## Browser DOM adapter (opt-in, default off)

Settings -> Agent capabilities -> "Let agents read and drive web pages through the browser's page
structure". Off by default; while off, every `/v1/dom/*` route answers **403
`error_code: dom_adapter_off`** and the rest of the gateway is unchanged.

The relay **never starts your browser**. To allow it, start a Chromium-family browser yourself
with remote debugging, ideally in a separate profile:

    msedge.exe --remote-debugging-port=9222 --user-data-dir=%TEMP%\relay-browser

The adapter connects only to `127.0.0.1` (port from `RELAY_DOM_CDP_PORT`, default 9222) and refuses
any tab whose debugger URL points elsewhere. Stdlib only (no new dependency).

| Route | Body | Notes |
|---|---|---|
| `GET /v1/dom/tabs` | | page tabs: `{id, title, url}` |
| `POST /v1/dom/snapshot` | `tab_id`, `max_chars`? (1000-200000, default 20000), `include_values`? (JSON `true` only) | compact node list `{d, tag, sel, role?, name?, text?, href?, type?, value?, checked?, hidden?}`; form values `[REDACTED]` by default, passwords `[REDACTED:password]` always; `truncated` when capped |
| `POST /v1/dom/act` | `holder`, `tab_id`, `op`: `click`\|`type`, `selector` (CSS), `text` (type) | click = real mouse press/release at the element centre; type = focus + insert text |

Gating: Pause -> 423 `paused` (reads and acts). Acts also need the caller to **hold the hands
claim** (409 `claim_required`) and the owner idle past the give-way threshold (409 `yield`; 503
`owner_state_unknown` when the hands cannot report it). No browser -> 503 `no_browser` with how to
start one.

**Risk:** a browser session is your signed-in accounts. With this on, an agent holding the token can
read those pages and click and type in them. Use a dedicated profile for agent work.

## Quickstart (services)

Docker is an option, never a requirement — both services are plain
Python and run natively just as well.

With Docker:

```
cp .env.example .env
docker compose up --build
```

Native services (PowerShell, from the repository root; run each service in its own terminal):

1. Start the standalone hands app and keep its window open. Copy the current hands secret from that window. For the all-in-one packaged install, use the packaged app instead of following this split-services procedure.
2. In terminal 1, start the parser:

   ```powershell
   Set-Location .\services\parser
   python -m pip install -r requirements.txt
   python app.py
   ```

3. In terminal 2, start the gateway (replace the example values locally; never commit them):

   ```powershell
   Set-Location .\services\gateway
   python -m pip install -r requirements.txt
   $env:RELAY_GATEWAY_TOKEN = "<long random token>"
   $env:HANDS_URL = "http://127.0.0.1:5000"
   $env:HANDS_SECRET = "<secret shown by the standalone hands app>"
   $env:PARSER_URL = "http://127.0.0.1:5011"
   python app.py
   ```

   When using the packaged app, its dashboard starts the gateway and parser for you; do not start duplicate service processes. The secret above is only needed for this split native setup.

- Gateway: http://127.0.0.1:5010/health — agent API under /v1 (screen, screen/hash, monitors, windows, clipboard, act, act/queued, hands/claim, hands/release, parse, find, elevate, info, filedrop, wait)
- Parser: http://127.0.0.1:5011/health — reports whether the model is loaded

**Change detection without the pixels:** `GET /v1/screen/hash` returns a
SHA-256 of the current frame's pixels with the bottom `mask_bottom` px
(default 80 — the taskbar) excluded, so a watcher can poll a tiny JSON
response and only fetch the full PNG when the hash changes. With
`?stable=1` the gateway samples two frames ~0.65 s apart and hashes
their per-pixel minimum, so a blinking caret or spinner phase cannot
flip the result. `/v1/screen` also carries that hash as an `ETag` and
answers a matching `If-None-Match` with `304`. If the gateway lacks
Pillow, the hash route returns 501 and `/v1/screen` serves without an
ETag — never a wrong hash.

The parser answers /parse only once OmniParser v2 weights are in its
models directory (`python services/parser/fetch_weights.py`, heavy deps in
`services/parser/requirements-model.txt`). Until then it returns 501 —
deliberately: a driver must never mistake "no model" for "empty screen".
The parser has no auth of its own, so it listens on loopback by default. Docker binds its published port to `127.0.0.1` on the host and uses `PARSER_HOST=0.0.0.0` only inside the private compose network.

## Coordination: hands claim + queued act

Two gateway features keep multiple agent sessions (and the owner) from
fighting over one desktop. Both are covered by the gateway token, like
every other /v1 route.

**Hands claim (advisory).** A session that is about to drive takes a
claim first so other sessions can see who holds the desktop:

- `POST /v1/hands/claim` `{"holder", "purpose", "ttl_seconds"?, "queue"?}` —
  grants when free (`200 {"granted": true, "claim": {...}}`); the same
  holder re-claiming refreshes the lease. TTL defaults to 900 s, clamped
  30–3600 s; expiry is lazy (an expired claim is simply absent). If
  another holder holds it: `409` with the current claim — or, with
  `"queue": true`, a FIFO queue slot (`200 {"granted": false, "queued":
  true, "position": N}`).
- When a claim is released or expires, the queue head gets a 30 s
  reservation: only that holder's claim can be granted during the grace
  window; everyone else still gets `409` / a queue slot. A head that does
  not claim within its window **forfeits its queue slot** (it can queue
  again at the back).
- The queue holds at most 32 waiters (`409` with `"queue_full": true`
  beyond that). Re-POSTing `{"queue": true}` keeps a waiter's position and
  refreshes its entry; entries not refreshed for 60 minutes are dropped.
  Holder names are capped at 128 characters, purposes at 500.
- `POST /v1/hands/release` `{"holder"}` — only the current holder can
  release (`403` otherwise; releasing an expired claim is an ok no-op).
  `{"holder", "cancel_queue": true}` instead drops that holder's queue
  entry.
- `GET /v1/hands/claim` — `{"claim": {...}|null, "queue": [...],
  "reserved_for": holder|null}`. `GET /v1/info` carries the same claim
  object as `hands_claim`.

The claim is **advisory coordination only — it does not gate
`/v1/act`**. A session that skips claiming can still act; well-behaved
sessions check the claim first and back off politely.

**Queued act (yielded-input accounting).** When the owner is actively
using the desktop, the hands answer an act with `409` (the act yielded).
Plain `POST /v1/act` surfaces that as-is and is unchanged.
`POST /v1/act/queued` takes the same body plus optional `hold_seconds`
(default 30, clamped 1–120):

- Every queued submission has an in-memory receipt identity before dispatch.
  A validated successful injection response settles `applied`; this does not
  prove the target application achieved its intended effect.
- Only structured, verified no-effect owner/lock refusals may become `held`.
  Malformed/lost/contradictory responses terminate as `unknown-outcome`
  (HTTP 502, `error_code: unknown_outcome`), never automatic replay.
- Valid owner aborts with partial counts settle `aborted-owner`; lock or
  foreground aborts settle `aborted`. Counts describe injection calls, not
  document contents. Neither these states nor `failed` authorizes blind replay.
- `GET /v1/act/queued/<id>` returns the receipt; absent/evicted IDs return
  HTTP 404 with `state: unavailable`. Absence is not proof of nonexecution.
  `GET /v1/act/queued` lists summaries, newest first; at most 200 are retained.
  Gateway restart loses this history. Durable recovery/exactly-once desktop
  effects are not provided.
- A receipt records the op and the payload's **key names only — never
  payload values** (typed text may be sensitive). The payload itself is
  held in memory only while the receipt is held and dropped when it
  settles.

### Watcher pause signal (item 32)

A watcher (eyes-only agent: `POST /v1/watch {holder}` or `?watcher=` on
the screen routes) can now say it is pausing:
`POST /v1/watch/pause {"holder", "reason"?, "timeout_seconds"?}`. The
watcher stays listed with `"state": "paused"` under a pause lease
(default 900 s, 30-3600 s) instead of lapsing after the 30-60 s poll
timeout; its next touch resumes it (`"state": "watching"`). A watcher that
is released (`POST /v1/watch/release`) or times out is **stopped** and is
listed in `watchers_stopped` (`how`: `released` / `timed_out`) on
`/v1/watch` and `/v1/info` for 5 minutes. The dashboard shows watching /
paused / stopped; the tray shows a paused-only watcher as free. Clients
that never pause see the old behaviour plus a `state` field.

## File drop

`POST /v1/filedrop` with
`{"folder", "filename", "data_b64", "sha256"?}` drops one file onto the
PC in a single call. The gateway computes the SHA-256 itself when it is
not supplied, verifies it against a supplied one before sending anything
(a mismatch is a 400), then runs the hands' chunked upload protocol
(256 KB raw chunks) and returns `{"ok", "path", "size", "sha256"}`.
Any hands error is surfaced with its status. On the hands side the
protocol is `POST /filedrop/begin` (returns an `upload_id` and
`chunk_max` 524288), `POST /filedrop/chunk` (strictly sequential
offsets; a wrong offset is a 409 carrying the expected offset),
`POST /filedrop/commit` (verifies size and SHA-256, then writes via a
temp file + atomic replace in the target folder), and
`POST /filedrop/abort`. Uploads are capped at 16 MB and expire after
10 minutes (any authenticated hands request purges expired ones). At
most 4 uploads / 64 MB of declared size may be in flight at once; `begin`
beyond that answers 503. Drops only land in allowlisted folders: the hands config
file key `"filedrop_dirs"` (a list of absolute paths), or the
`HANDS_FILEDROP_DIRS` environment variable (`os.pathsep`-separated,
which wins when set). With no allowlist configured, every filedrop
endpoint returns 501. Filenames must be bare names — no path separators
or `..`.

## Wait for a condition

`POST /v1/wait` with
`{"condition": {...}, "timeout_seconds": 30, "poll_seconds": 1}` polls
until a condition holds or the deadline passes, returning
`{"ok": true, "held": true, "waited_seconds": ..., "condition": ...}`
or, on deadline, `{"ok": true, "held": false, "timeout": true, ...}`.
`timeout_seconds` clamps to 1–300 and `poll_seconds` to 0.2–10; an
unknown condition type is a 400 immediately. Condition types:
`{"type": "screen_hash_change", "stable": bool?}` (the masked frame
hash — see `/v1/screen/hash` — differs from the starting sample; 501 if
the gateway lacks Pillow), `{"type": "screen_region_change",
"region": [x, y, w, h]}` (only that region's pixels, clamped to the
frame, change), `{"type": "window_title", "contains": "..."}` (a
window title contains the text, via the hands' `GET /window_titles`;
if the hands answers 404/501 the wait returns
`{"ok": false, "error": "hands does not support window titles"}`
immediately instead of burning the timeout), and
`{"type": "parser_model_loaded"}` (the parser's `/health` reports
`model_loaded` true — the parser loads its model in the background at
startup and retries a failed load every 60 s, `PARSER_RETRY_SECONDS`, so
weights fetched while it runs are picked up without a restart).

## PC lock and sleep state

The hands' `GET /state` returns
`{"ok": true, "platform": "windows"|"other", "locked": true|false|null,
"idle_seconds": number|null}` — on Windows the lock state comes from
the input desktop (`Winlogon` means locked) and idle time from the
last-input timestamp; off Windows, or on any probe failure, the values
are `null` (never a 500). The hands also expose `GET /window_titles`
(`{"ok": true, "titles": [...]}`). The gateway's `GET /v1/info` now
carries a `"pc_state"` field with the hands' `/state` payload, fetched
inside the same 5-second-cached reachability probe as the other info
fields; it is `null` when the hands service is unreachable or `/state` errors,
and all existing `/v1/info` fields are unchanged.
## Actuation


New hands routes, all behind the same X-Secret gate as the existing
routes (and, through the gateway, the same X-Relay-Token gate):

| Hands route | Gateway surface | What it does |
| --- | --- | --- |
| `POST /hotkey {keys: ["ctrl", "l"]}` | act op `hotkey` | Press a true key chord via `pyautogui.hotkey()`. |
| `POST /break {times?: 1..3}` | act op `break` | Ctrl+C chord(s), 0.15 s apart. |
| `POST /drag {x1, y1, x2, y2, button?, duration?}` | act op `drag` | Mouse-down at the start point, move, mouse-up at the end point. |
| `GET /windows` | `GET /v1/windows` | Open windows: title, process, pid, bounds, monitor index, visible, focused. Off Windows it returns an empty list with `"supported": false`. |
| `POST /focus {title, exact?}` | act op `focus` | Focus the first window whose title matches (substring, case-insensitive; full-title when `exact` is true); restores a minimized window first. No match → 404. Works without pywin32 (ctypes); pywin32, when installed, only adds process names to `/windows`. |
| `POST /clipboard/set {text}` | act op `clipboard_set` | Set the clipboard (Windows: ctypes `CF_UNICODETEXT`; else pyperclip when importable; otherwise 501). |
| `GET /clipboard/get` | `GET /v1/clipboard` | Read the clipboard (same backends, same 501 when unavailable). |

**Clipboard read is sensitive.** It stays behind the same authentication
as every other hands/gateway route, and *every read writes a line to the
hands log queue* (`clipboard read via relay`), so the owner sees reads
in the app window as they happen.

**Failsafe.** Throwing the pointer into a screen corner (pyautogui's
FAILSAFE) aborts the act in progress; the hands answer
`409 {"error_code": "failsafe"}` and log it to the app window. An agent
must treat it as the owner saying *stop* — it is not a yield, so a queued
act settles `failed` rather than being retried. Malformed act bodies
(`/move`, `/click`, `/key`, `/scroll` included) are a JSON `400`.

**Why `/break` exists:** the older `/key` op calls `pyautogui.press()`
with ONE key name, so a chord string like `"ctrl+c"` sent to `/key` was
never a real chord — nothing was interrupted. `/break` and `/hotkey`
send true chords via `pyautogui.hotkey()`, so the interrupt lands.

**Fast `/type`:** `POST /type` now also accepts
`{method: "auto"|"paste"|"keys"|"real_keys"}` (absent = the old behavior for short
text). In `auto`, text over 120 chars goes through the clipboard when a
backend exists: the current clipboard is saved, replaced with the text,
pasted with Ctrl+V, then restored. Any clipboard failure falls back to
keystrokes rather than failing, and the response reports what happened:
`{"ok": true, "method_used": "paste"|"keys"|"real_keys", "chars": n,
"elapsed_seconds": x}`. Paste responses also report `owner_guard`. Before injection, owner input or locking refuses the paste, and so does a changed foreground when the request carries `expect_focus`; after injection, a takeover reports `chars_typed` and `type_aborted` without replay. A paste already sent cannot be retracted. Queued owner aborts settle as `aborted-owner`, not as a relay failure. On the gateway, the act timeout for `type` is
sized to the payload — `max(30, min(600, 15 + len(text)/6))` seconds,
since the keystroke path runs at worst ~6 chars/s — while every other
op keeps the flat 30 s.

## Capture variants (hands + gateway)

`GET /v1/monitors` lists the monitors as rectangles in virtual-desktop
coordinates (`{x, y, w, h, primary}` — origins can be negative for a
monitor left of or above the primary). `GET /v1/screen` and
`GET /v1/screen/hash` accept two mutually exclusive selectors,
forwarded to the hands:

- `?monitor=N` — capture monitor N (0-based) only. Out of range is a
  400 from the hands naming how many monitors exist.
- `?region=x,y,w,h` — capture that rectangle in virtual-desktop
  coordinates (a region may span monitors, including negative origins).
  Malformed regions are a 400 from the gateway without the hands being
  called.

With no selector the full primary frame is returned exactly as before,
and the ETag/hash always describe the variant actually requested.
Responses from the hands carry `X-Screen-Width`/`X-Screen-Height` of the
returned image, plus `X-Screen-Monitor` or `X-Screen-Region` when a
selector was used.

## Input realism (hands)

Typing and pointing are human-paced by default, and both behaviors are
planned by pure functions (`_plan_key_delays`, `_plan_path`) so the
timing and geometry are unit-tested off-desktop
(`hands/test_capture_realism.py`):

- **Keystroke jitter** (`POST /type`, default on): each character is
  written individually with a delay after it — base uniform
  0.04–0.16 s, plus 0.05–0.20 s after a space and 0.10–0.30 s after
  sentence-ending punctuation. If a plan would exceed
  `max(5 s, 0.16 s/char)` it is scaled down proportionally. The
  response reports `delays_planned` and `planned_seconds`.
  `{jitter: false}` restores the old single fast write.
- **Mouse dynamics** (`POST /move`, `POST /click`, default on): the
  cursor follows a curved quadratic-bezier path with an ease-in-out
  profile, a slight overshoot past the target, and a settle onto the
  exact target, over `clamp(0.25 + distance/2500, 0.3, 1.1)` seconds;
  a human click hovers 0.05–0.15 s before clicking.
  `{human: false}` restores the old direct move.

Live-only verification steps for these features (real multi-monitor
geometry, negative-origin regions, jitter/dynamics feel) are listed in
[docs/VERIFICATION.md](docs/VERIFICATION.md).

## Find text on screen

`POST /v1/find` (parser: `POST /find`) with
`{"image_b64": "...", "text": "...", "case_sensitive": false}` runs the
same parse pipeline and returns only the elements whose label contains
the text — `{"ok": true, "matches": [...], "count": N, "width": W,
"height": H}`, with each match keeping the full set-of-marks shape
(`id`, `label`, `bbox`, `center`, `interactable`). Empty text is a 400;
with no model loaded, /find returns the same 501 as /parse.

## Packaged app: autostart + self-heal

- **Start on login** (opt-in, off by default): the checkbox under the
  Start/Stop row in the packaged app writes/removes the value
  `HandsRelay v4` under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`,
  pointing at the app (the quoted executable when frozen, quoted Python
  plus the quoted script when run from source). Nothing is written until
  the owner toggles it.
- **Self-heal:** the app's periodic self-check feeds each service's
  liveness into a failure counter. Three consecutive failures restart a
  service the app itself started (at most 3 restarts per service), then
  the app logs that it is giving up and stops restarting it. A service
  that was already running elsewhere is never restarted, and if the
  gateway itself is down the other services' health is treated as
  unknown rather than failed, so one outage does not restart everything.

## Elevation bridge

Some programs only run elevated. The bridge lets an agent *request* an
elevated launch without ever being able to cause one:

- The operator pre-configures an allowlist in the hands config file
  (`hands/hands_relay_config.json`), key `elevate_allowlist`, mapping a
  request name to static `exe`/`args` strings, e.g.
  `{"notepad-elevated": {"exe": "notepad.exe", "args": ""}}`. The agent
  supplies only a name — never command text.
- `POST /v1/elevate {"name": "..."}` (hands: `POST /elevate`) creates a
  pending request and returns its id; an unconfigured allowlist is a
  501 and an unknown name is a 404. `GET /v1/elevate/<id>` reports the
  state: `pending`, `denied`, `expired` (pending for over 120 s),
  `launched`, or `launch_failed`.
- The request is shown in the packaged app window as a modal dialog
  naming the request and the exact configured exe/args. **Only the
  person at the PC clicking Approve (Yes) runs it** — elevated, via the
  Windows UAC `runas` verb, so the UAC prompt still appears too. Deny
  (No) does nothing. There is no auto-approve path, no timeout-approve,
  and no config flag that approves.
- A request that times out closes its card on time even if the agent
  never polls it. Settled requests are kept for an hour (then pruned; the
  table holds at most 200). The standalone hands app has no approval UI,
  so it denies elevation requests immediately and logs why.

## Hands (Windows)

The hands run **inside the packaged app** (above) — same Flask routes from
`hands/hands_relay_hands.py`, served headless on 127.0.0.1:5000 by the app
process, with the app-owned persistent secret described above. The standalone hands app supports split deployments; its optional transport settings are documented in [hands/README.md](hands/README.md). For a split deployment (services run separately, e.g. under Docker):

```
cd hands
build_hands_relay.bat   :: builds "HandsRelay.exe" (standalone hands)
```

`start-v4.bat` starts the parser and gateway in console windows for this
split setup and asks for the hands app's current secret.

## Security model

- The hands and the parser listen on loopback only. **The gateway listens
  on all interfaces (0.0.0.0) by default** — in the packaged app and when
  run directly — so an agent can reach it by the PC's tailnet IP; the token
  is what protects it. Set `RELAY_GATEWAY_HOST=127.0.0.1` when the gateway
  is only reached through Tailscale Serve or a local proxy (the app window
  logs where the gateway listens). Docker publishes it on 127.0.0.1 only.
  Tailscale is the preferred remote transport. Public tunnels are disabled by default.
- Two credentials are stored in the existing per-user compatibility directory
  (`%USERPROFILE%\.HandsRelayV0.1\`): the gateway token (presented by the
  agent) and the hands secret (used only on the local gateway-to-hands link).
  This directory name is intentionally retained so upgrades continue using
  existing credentials; do not rename or move it during an upgrade. Delete a
  credential file only to rotate that credential. The standalone hands app
  creates a fresh hands secret for each launch.
- Failed auth attempts are logged; 10 in 10 minutes from one address
  pauses auth for that address for 5 minutes. Gateway rejections and
  lockouts appear in the packaged app's log, and a lockout pushes a phone
  alert through the hands' ntfy topic (a standalone gateway alerts via
  `RELAY_NTFY_TOPIC`). The standalone hands app alerts on its own lockouts.
- Design rule inherited from v1's audit: the URL is not an access control; authentication and the transport are.

## Running the tests

```
pip install flask waitress requests pillow pytest
python tools/run_all_tests.py
```

runs every suite, each in its own process (the suites install
`sys.modules` fakes and must not share an interpreter): files with
pytest test functions via `python -m pytest`, the older script-style
files (`run()` + `check()`) directly. Plain `pytest` at the root is not
equivalent — it collects nothing from the script-style files. CI runs the
same command on every push and pull request
(`.github/workflows/tests.yml`). Windows-live behavior (real actuation,
hooks, UAC, lock detection, the display probe) is stub-tested only; the
live checklist, including the memory soak, is in
[docs/VERIFICATION.md](docs/VERIFICATION.md).

## Operating notes for agents

*For an agent handed this repo cold. The sections above are the protocol; these are the field rules that keep a session honest. Distilled from sessions where the failures were almost all around the relay — perception, app state, power, comms — not in it.*

**Before you say "ready"**
- A green `/v1/info` is necessary, not sufficient. Ready = relay up + target app in a known state + capture path proven live + power chain checked + owner channel working.
- "Ready", "started" and "running" require two independent live checks seconds apart — e.g. a frame under 3 s old *and* a log file growing between polls. One channel agreeing with itself proves nothing.
- A feed that reports the same frame hash forever may be fetching nothing. Verify liveness with a direct fetch before trusting it.

**Reading the screen**
- Verify-by-screenshot has a quality bar: compressed full-screen frames are not evidence for small text or gauges. Crop and zoom digital readouts; a value you cannot read cleanly is *unread*, never estimated.
- Physical-state claims come from underlying data — logs, digital values — never from a glance at a busy dashboard.
- When the owner states a physical fact (what he plugged in, switched on, or started), that outranks your screen interpretation. Reconcile your data to his statement, not the reverse.

**Acting**
- If an action's expected effect does not appear within its normal latency, stop and read the *full frame* for a modal dialog before any retry. A waiting dialog is the first suspect; input fired at a UI that is not listening goes nowhere — and this relay runs unelevated, so its input is silently dropped into elevated windows (UIPI): an act reporting success is not proof it landed. Verify by capture.
- Keys go to the *focused* window; clicks go to coordinates on the *captured* display. With several windows or monitors those can silently diverge — verify your target window is the focused, topmost one before acting on it.
- First contact with any app includes a recon pass: how start/stop actually work there — prompts, defaults, busy states, secondary capture paths. Note it before real work.
- After any stop or regroup, re-verify state from zero. Never resume from the pre-stop picture.

**Power & comms**
- Inventory every power source in the chain at session start: the PC's charge, the phone if it is the uplink, any device-side power the work depends on. Flag low readings early.
- Tethers and hotspots turn themselves off. A dead feed mid-session is a power/uplink suspect first, not a relay suspect.
- Note clock offsets on every device whose logs you will correlate; device timestamps are not wall time.

**Owner channel**
- A sent message is not a seen message. If the owner seems to be working from partial information, re-deliver the content itself — never point at "the message above".

## Relay Bench

The hands have a test page: **Relay Bench** — a scored battery (clicking, holding and dragging, a 6x6 mate-in-1, instruction traps, a flash-capture test) for grading a computer-use agent working through this relay.

**Run it: https://moorefolio.com/relay-bench/**

The bench lives on the portfolio site, not in this repo — this link is its only presence here.

MIT © 2026 Christopher Moore
