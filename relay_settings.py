"""HandsRelay v4 settings store (tray + settings ticket, 2026-11-15).

One versioned JSON file in the app's per-machine folder
(%USERPROFILE%\\.HandsRelayV0.1\\settings.json, next to the token and
hands secret). Every setting's default equals the behaviour v4 had
before the store existed, so an absent, empty or default file changes
nothing.

Load rules (all pure apart from the file read):
  * missing file          -> defaults, no warning
  * unreadable / bad JSON -> defaults + a visible warning (the corrupt
                             file is left in place, never overwritten
                             until the owner saves from the panel)
  * newer "version"       -> defaults + warning (do not guess a future
                             schema)
  * unknown keys          -> ignored (kept out of the result)
  * a bad value           -> that key falls back to its default + warning

Adding a setting (the hook for the partner's 'toggle' rulings): append
one Setting(...) to SETTINGS below with a plain-language label and a
default equal to today's behaviour, then read it where the behaviour
lives (relay.settings.get(key), or an APPLY entry for a module
constant / env var). The panel renders straight from SETTINGS, so no
UI change is needed. A ruled toggle whose feature is not built yet is
registered with available=False (and default False, a risk note): it
is kept in the schema and in a saved file, but the panel does not show
it, because a switch that does nothing is worse than none. Flip
available=True in the same commit that builds the feature. Bump SCHEMA_VERSION only when an existing key
changes meaning; adding keys does not need a bump.
"""

import json
import math
import os
import tempfile
from dataclasses import dataclass, field

SCHEMA_VERSION = 1
FILE_NAME = "settings.json"


@dataclass(frozen=True)
class Setting:
    key: str
    label: str  # plain language, shown in the panel
    help: str
    kind: str  # "bool" | "int"
    default: object
    min: int = None
    max: int = None
    # "live": takes effect when saved; "restart": on the next Start.
    applies: str = "restart"
    group: str = "Relay"
    # Where today's behaviour lives (inventory, for the report/readers).
    source: str = ""
    # False: the feature is not built; the panel hides the switch.
    available: bool = True
    risk: str = ""  # shown under the switch for risky capabilities
    extra: dict = field(default_factory=dict)


SETTINGS = (
    Setting(
        key="owner_yield_idle_seconds",
        label="Give way to me: wait this many seconds after my last mouse or key input before the agent may act",
        help="While you are using the PC the agent's actions are held back. Lower means the agent resumes sooner.",
        kind="int", default=5, min=1, max=120, applies="live", group="Sharing the PC",
        source="hands/hands_relay_hands.py _yield_if_active (env HANDS_YIELD_IDLE_SECONDS, default 5)",
    ),
    Setting(
        key="claim_ttl_default_seconds",
        label="Hands control lasts this many seconds unless the agent asks for a different time",
        help="An agent's claim on the hands expires after this unless it renews. Agents may still ask for 30-3600 s.",
        kind="int", default=900, min=30, max=3600, applies="live", group="Hands control",
        source="services/gateway/app.py CLAIM_TTL_DEFAULT",
    ),
    Setting(
        key="queued_act_hold_seconds",
        label="Hold a queued action this many seconds while I am busy, then give up",
        help="Default wait for /v1/act/queued when the agent does not name one (1-120 s).",
        kind="int", default=30, min=1, max=120, applies="live", group="Sharing the PC",
        source="services/gateway/app.py HOLD_DEFAULT",
    ),
    Setting(
        key="watcher_timeout_seconds",
        label="Forget a watching agent after this many seconds without a screen poll",
        help="Eyes-only agents drop off the dashboard after this silence (30-60 s).",
        kind="int", default=30, min=30, max=60, applies="restart", group="Hands control",
        source="services/gateway/app.py WATCHER_TIMEOUT_DEFAULT (env WATCHER_TIMEOUT_SECONDS)",
    ),
    Setting(
        key="paste_auto_min_chars",
        label="Type long text by pasting once it is longer than this many characters",
        help="Shorter text is typed key by key. Your clipboard is saved and put back after a paste.",
        kind="int", default=120, min=1, max=100000, applies="live", group="Typing",
        source="hands/hands_relay_hands.py _type_dispatch (was the literal 120)",
    ),
    Setting(
        key="close_to_tray",
        label="Closing the window keeps the relay running in the tray",
        help="Off (today's behaviour): closing the window stops all three services. On: it hides to the tray; use Quit there.",
        kind="bool", default=False, applies="live", group="Window and tray",
        source="hands_relay_v4_app.py main (window closed -> relay.stop)",
    ),
    Setting(
        key="tray_icon",
        label="Show the relay in the system tray",
        help="The tray icon shows whether the relay is running, who holds the hands, and whether you are active.",
        kind="bool", default=True, applies="restart", group="Window and tray",
        source="new with this ticket (no tray existed before)",
    ),
)

# Partner toggle rulings, 2026-11-15 18:57 CST (BOARD 19:00:00, "we
# shouldn't babysit how people use the app"): risky capabilities ship as
# toggles, default OFF. None of these features exists yet, so all are
# available=False until their own build lands.
_RULED = "partner ruling 2026-11-15 (toggle); feature not built yet"
PLANNED_TOGGLES = (
    Setting(
        key="batched_actions", label="Let an agent send several actions in one request",
        help="Adds POST /v1/act/batch (up to 50 acts in order). The batch stops when you take over or pause the relay.",
        risk="A batch can do several things before your input interrupts it.",
        kind="bool", default=False, applies="live", group="Agent capabilities",
        source="partner ruling 2026-11-15 (toggle); services/gateway/app.py BATCHED_ACTIONS (env HANDSRELAY_BATCHED_ACTIONS)",
    ),
    Setting(
        key="trajectory_recording", label="Record what the agent does (screens and actions) to disk",
        help="Saves every agent action and every screenshot it takes to a local folder "
             "(%USERPROFILE%\\.HandsRelayV0.1\\trajectories). Nothing is uploaded. Typed text is "
             "blanked out unless you also turn on the next switch.",
        risk="Recordings keep everything on screen and every typed text, passwords included.",
        kind="bool", default=False, applies="live", group="Agent capabilities",
        source="partner ruling 2026-11-15 (toggle); services/gateway/app.py TRAJECTORY_RECORDING (env HANDSRELAY_TRAJECTORY_RECORDING)",
    ),
    Setting(
        key="trajectory_record_typed_text", label="Also keep the text the agent types in recordings",
        help="Only matters while recording is on. Off: typed text is saved as its length only.",
        risk="Typed text often includes passwords, card numbers and private messages; they are saved in plain text.",
        kind="bool", default=False, applies="live", group="Agent capabilities",
        source="sub-option proposed with trajectory recording (toggles wave 1); services/gateway/app.py TRAJECTORY_TYPED_TEXT",
    ),
    Setting(
        key="custom_tools_registry", label="Let agents run the custom tools I declare",
        help="Agents may list and run the programs you name in %USERPROFILE%\\.HandsRelayV0.1\\tools.json. "
             "Agents cannot add tools, and nothing is downloaded.",
        risk="A custom tool runs whatever its author wrote, on this PC.",
        kind="bool", default=False, applies="live", group="Agent capabilities",
        source="partner ruling 2026-11-15 (toggle); services/gateway/app.py CUSTOM_TOOLS (env HANDSRELAY_CUSTOM_TOOLS)",
    ),
    Setting(
        key="ui_automation_tree", label="Let agents read the Windows UI Automation tree",
        help="Agents can see buttons and fields by name instead of only by screenshot.",
        risk="Exposes the text and structure of every open window.",
        kind="bool", default=False, group="Agent capabilities", available=False, source=_RULED,
    ),
    Setting(
        key="browser_dom_adapter", label="Let agents read and drive web pages through the browser's page structure",
        help="Works with any model driving the relay, not only Muse.",
        risk="Page contents, including signed-in sites, become readable to the agent.",
        kind="bool", default=False, group="Agent capabilities", available=False, source=_RULED,
    ),
    Setting(
        key="input_injecting_smoke_script", label="Allow the self-test that moves the mouse and types",
        help="Lets tools/smoke_inject.py run: it moves the pointer and, if you name a window, types a marker into it.",
        risk="It moves the pointer and types while it runs.",
        kind="bool", default=False, applies="live", group="Agent capabilities",
        source="partner ruling 2026-11-15 (toggle); tools/smoke_inject.py reads it from settings.json",
    ),
    Setting(
        key="nested_desktop", label="Run the agent on a separate (nested) desktop",
        help="Optional mode: the agent works on its own desktop instead of yours.",
        risk="Experimental mode; see docs/SPIKE-NESTED-DESKTOP.md.",
        kind="bool", default=False, group="Agent capabilities", available=False, source=_RULED,
    ),
)
SETTINGS = SETTINGS + PLANNED_TOGGLES

BY_KEY = {s.key: s for s in SETTINGS}


def defaults():
    return {s.key: s.default for s in SETTINGS}


def settings_path():
    return os.path.join(os.path.expanduser("~"), ".HandsRelayV0.1", FILE_NAME)


def validate_value(spec, value):
    """(ok, normalized). Booleans are strict; ints reject bools, floats
    with a fraction, non-finite numbers and out-of-range values."""
    if spec.kind == "bool":
        return (True, value) if isinstance(value, bool) else (False, None)
    if spec.kind == "int":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False, None
        if isinstance(value, float) and (not math.isfinite(value) or value != int(value)):
            return False, None
        value = int(value)
        if spec.min is not None and value < spec.min:
            return False, None
        if spec.max is not None and value > spec.max:
            return False, None
        return True, value
    return False, None


def parse(text):
    """Settings text -> (values, warnings). Never raises."""
    values = defaults()
    try:
        doc = json.loads(text)
    except (ValueError, TypeError) as exc:
        return values, [f"Settings file is not valid JSON ({exc.__class__.__name__}); using defaults."]
    if not isinstance(doc, dict):
        return values, ["Settings file is not a JSON object; using defaults."]
    version = doc.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        return values, ["Settings file has no valid version; using defaults."]
    if version > SCHEMA_VERSION:
        return values, [f"Settings file is version {version}, newer than this app ({SCHEMA_VERSION}); using defaults."]
    stored = doc.get("settings")
    if not isinstance(stored, dict):
        return values, ["Settings file has no settings object; using defaults."]
    warnings = []
    for key, raw in stored.items():
        spec = BY_KEY.get(key)
        if spec is None:
            continue  # unknown keys are ignored (a newer app's toggle, a typo)
        ok, value = validate_value(spec, raw)
        if ok:
            values[key] = value
        else:
            warnings.append(f"Setting '{key}' has an invalid value; using the default ({spec.default!r}).")
    return values, warnings


def serialize(values):
    clean = {}
    for spec in SETTINGS:
        ok, value = validate_value(spec, values.get(spec.key, spec.default))
        clean[spec.key] = value if ok else spec.default
    return json.dumps({"version": SCHEMA_VERSION, "settings": clean}, indent=2, sort_keys=True) + "\n"


def changed_from_defaults(values):
    return {k: v for k, v in values.items() if k in BY_KEY and v != BY_KEY[k].default}


def schema(include_unavailable=False):
    """The panel's view of SETTINGS (JSON-safe); only built features
    unless include_unavailable."""
    return [
        {
            "key": s.key, "label": s.label, "help": s.help, "kind": s.kind,
            "default": s.default, "min": s.min, "max": s.max,
            "applies": s.applies, "group": s.group, "risk": s.risk,
            "available": s.available,
        }
        for s in SETTINGS
        if s.available or include_unavailable
    ]


class SettingsStore:
    def __init__(self, path=None):
        self.path = path or settings_path()
        self.values = defaults()
        self.warnings = []

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except FileNotFoundError:
            self.values, self.warnings = defaults(), []
            return self.values
        except OSError as exc:
            self.values = defaults()
            self.warnings = [f"Settings file could not be read ({exc.__class__.__name__}); using defaults."]
            return self.values
        self.values, self.warnings = parse(text)
        return self.values

    def get(self, key):
        return self.values.get(key, BY_KEY[key].default)

    def update(self, changes):
        """Validate a partial update, then save atomically.
        Returns (ok, errors). Nothing is written when any value is bad."""
        if not isinstance(changes, dict):
            return False, ["settings must be an object"]
        errors, staged = [], dict(self.values)
        for key, raw in changes.items():
            spec = BY_KEY.get(key)
            if spec is None:
                errors.append(f"unknown setting '{key}'")
                continue
            if not spec.available:
                errors.append(f"'{key}' is not available yet (its feature is not built)")
                continue
            ok, value = validate_value(spec, raw)
            if not ok:
                bounds = f" ({spec.min}-{spec.max})" if spec.kind == "int" else ""
                errors.append(f"'{key}' must be a {spec.kind}{bounds}")
                continue
            staged[key] = value
        if errors:
            return False, errors
        self._write(serialize(staged))
        self.values, self.warnings = staged, []
        return True, []

    def reset(self):
        self._write(serialize(defaults()))
        self.values, self.warnings = defaults(), []

    def _write(self, text):
        folder = os.path.dirname(self.path) or "."
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=folder)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
