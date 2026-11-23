"""v5: real-key typing and visible pointer travel, both owner-interruptible.

Windows is stubbed (SendInput seam, pyautogui recorder, owner hook
counters); the live Windows receipts are in the ticket folder. Nothing
here models human timing statistics: the typing mode uses a fixed delay
and these tests assert it draws no random numbers.
"""
import importlib.util
import math
import random
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
URL40 = "https://www.spotify.com/account/privacy/"
UP, UNI = 0x0002, 0x0004


@pytest.fixture(scope="module")
def hands():
    spec = importlib.util.spec_from_file_location("v5_hands", ROOT / "hands/hands_relay_hands.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _status(resp):
    return resp[1] if isinstance(resp, tuple) else resp.status_code


def _body(resp):
    return resp[0].get_json() if isinstance(resp, tuple) else resp.get_json()


class Session:
    """Hook counters: our SendInput is INJECTED, the owner's is not."""

    def __init__(self, owner_on_sleep=None, owner_in_unit=None):
        self.owner = 0
        self.injected = 0
        self.batches = []       # real-key batches
        self.moves = []         # pointer steps
        self.downs = []
        self.sleeps = 0
        self.owner_on_sleep = owner_on_sleep
        self.owner_in_unit = owner_in_unit

    def snapshot(self):
        return (self.owner, self.injected)

    def _unit_done(self, n):
        self.injected += 1
        if self.owner_in_unit == n:
            self.owner += 1

    def send(self, events):
        self.batches.append(list(events))
        self._unit_done(len(self.batches))

    def moveTo(self, x, y, duration=None):
        self.moves.append((x, y))
        self._unit_done(len(self.moves))

    def sleep(self, _s):
        self.sleeps += 1
        if self.sleeps == self.owner_on_sleep:
            self.owner += 1


@pytest.fixture
def rig(hands, monkeypatch):
    s = Session()
    monkeypatch.setattr(hands, "_probe_lock_state", lambda: (None, None))
    monkeypatch.setattr(hands, "_probe_locked", lambda: False)
    monkeypatch.setattr(hands, "_yield_if_active", lambda: None)
    monkeypatch.setattr(hands, "_focus_guard", lambda: None)
    monkeypatch.setattr(hands._owner_monitor, "ensure_started", lambda wait=0.5: False)
    monkeypatch.setattr(hands, "_owner_input_snapshot", s.snapshot)
    monkeypatch.setattr(hands, "_probe_dwtime", lambda: None)
    monkeypatch.setattr(hands, "_tick_ms", lambda: None)
    monkeypatch.setattr(hands, "_INPUT", hands.motion.InputMutex(sleep=lambda x: None))
    monkeypatch.setattr(hands, "_send_key_events", s.send)
    monkeypatch.setattr(hands, "_vk_for_char", _fake_layout)
    monkeypatch.setattr(hands.time, "sleep", s.sleep)
    monkeypatch.setattr(hands.pyautogui, "moveTo", s.moveTo, raising=False)
    monkeypatch.delenv("HANDS_REAL_KEYS", raising=False)
    monkeypatch.setitem(hands._cfg, "real_keys", False) if isinstance(hands._cfg, dict) else None
    return s


def _fake_layout(ch):
    """US-layout stand-in: letters, digits and a few symbols only."""
    if ch.isascii() and ch.isalpha():
        return (ord(ch.upper()), 1 if ch.isupper() else 0)
    if ch.isascii() and ch.isdigit():
        return (ord(ch), 0)
    table = {" ": (0x20, 0), ".": (0xBE, 0), "/": (0xBF, 0), ":": (0xBA, 1), "-": (0xBD, 0)}
    return table.get(ch)


def _type(hands, text, method="real_keys", **extra):
    with hands.app.test_request_context(
        "/type", method="POST", json={"text": text, "method": method, **extra}
    ):
        resp = hands.type_text()
    return _status(resp), _body(resp)


# ===================================================== bar 1: real-key typing

def test_events_pair_every_down_with_an_up(hands, monkeypatch):
    monkeypatch.setattr(hands, "_vk_for_char", _fake_layout)
    for ch in "aZ9 .:-/\n\té€\U0001F600":
        ev = hands._real_key_events(ch)
        downs = [e for e in ev if not e[2] & UP]
        ups = [e for e in ev if e[2] & UP]
        assert len(downs) == len(ups) > 0, ch
        assert ev[0][2] & UP == 0 and ev[-1][2] & UP, ch  # opens with a down, ends with an up


def test_shifted_char_wraps_key_in_shift_down_up(hands, monkeypatch):
    monkeypatch.setattr(hands, "_vk_for_char", _fake_layout)
    ev = hands._real_key_events("A")
    assert ev == [(0x10, 0, 0), (0x41, 0, 0), (0x41, 0, UP), (0x10, 0, UP)]


def test_unmapped_char_uses_unicode_down_up(hands, monkeypatch):
    monkeypatch.setattr(hands, "_vk_for_char", _fake_layout)
    assert hands._real_key_events("é") == [(0, 0xE9, UNI), (0, 0xE9, UNI | UP)]
    # astral char: two UTF-16 units, each a down/up pair
    ev = hands._real_key_events("\U0001F600")
    assert [e[1] for e in ev] == [0xD83D, 0xD83D, 0xDE00, 0xDE00]
    assert all(e[2] & UNI for e in ev)


def test_real_keys_sends_events_not_pyautogui_or_clipboard(hands, rig, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not be called")
    monkeypatch.setattr(hands.pyautogui, "write", boom, raising=False)
    monkeypatch.setattr(hands, "_clipboard_backend", boom)
    monkeypatch.setattr(hands.random, "uniform", boom)
    monkeypatch.setattr(hands.random, "random", boom)
    status, body = _type(hands, URL40)
    assert status == 200 and body["ok"] is True
    assert body["method_used"] == "real_keys" and body["chars"] == 40
    assert len(rig.batches) == 40
    assert body["planned_seconds"] == pytest.approx(40 * hands.REAL_KEYS_DELAY_S)


def test_real_keys_sends_text_that_paste_would_reject(hands, rig):
    status, body = _type(hands, "Café €5\nok")
    assert status == 200
    flat = [e for b in rig.batches for e in b]
    assert flat and sum(1 for e in flat if e[2] & UP) * 2 == len(flat)


def test_default_auto_is_unchanged_paste_for_long_text(hands, rig, monkeypatch):
    pasted = []

    class Clip:
        def get_text(self):
            return "old"

        def set_text(self, t):
            pasted.append(t)

    monkeypatch.setattr(hands, "_clipboard_backend", lambda: Clip())
    monkeypatch.setattr(hands.pyautogui, "hotkey", lambda *a, **k: None, raising=False)
    status, body = _type(hands, "x" * 200, method="auto")
    assert status == 200 and body["method_used"] == "paste"
    assert rig.batches == []
    status, body = _type(hands, "x" * 200, method="paste")
    assert body["method_used"] == "paste"


def test_session_setting_turns_auto_into_real_keys_and_request_still_wins(hands, rig, monkeypatch):
    monkeypatch.setenv("HANDS_REAL_KEYS", "1")
    assert hands._real_keys_default() is True
    status, body = _type(hands, "x" * 200, method="auto")
    assert body["method_used"] == "real_keys" and len(rig.batches) == 200
    # an explicit paste request still pastes
    monkeypatch.setattr(hands, "_clipboard_backend", lambda: type(
        "C", (), {"get_text": lambda s: "o", "set_text": lambda s, t: None})())
    monkeypatch.setattr(hands.pyautogui, "hotkey", lambda *a, **k: None, raising=False)
    status, body = _type(hands, "x" * 200, method="paste")
    assert body["method_used"] == "paste"


def test_real_keys_off_by_default(hands, monkeypatch):
    monkeypatch.delenv("HANDS_REAL_KEYS", raising=False)
    assert hands._real_keys_default() is False


def test_bad_method_still_rejected(hands, rig):
    status, body = _type(hands, "hi", method="telepathy")
    assert status == 400 and "real_keys" in body["error"]


def test_real_keys_owner_abort_stops_next_char(hands, rig):
    rig.owner_on_sleep = 18  # owner touches the keyboard after char 18
    status, body = _type(hands, URL40)
    assert status == 409
    assert body["error_code"] == "type_aborted" and body["abort_reason"] == "owner"
    assert body["method_used"] == "real_keys" and body["chars_typed"] == 18
    assert len(rig.batches) == 18


def test_real_keys_owner_event_inside_unit_stops_following_unit(hands, rig):
    rig.owner_in_unit = 5
    status, body = _type(hands, URL40)
    assert status == 409 and body["chars_typed"] == 5 and len(rig.batches) == 5


def test_real_keys_locked_aborts(hands, rig, monkeypatch):
    monkeypatch.setattr(hands, "_probe_locked", lambda: len(rig.batches) >= 7)
    status, body = _type(hands, URL40)
    assert status == 409 and body["abort_reason"] == "locked" and body["chars_typed"] == 7


def test_short_sendinput_is_an_error_and_releases_modifiers(hands, rig, monkeypatch):
    released = []
    monkeypatch.setattr(hands.pyautogui, "keyUp", lambda k, **kw: released.append(k), raising=False)

    def refuse(events):
        raise RuntimeError("SendInput accepted 0 of 2 key events")

    monkeypatch.setattr(hands, "_send_key_events", refuse)
    with pytest.raises(RuntimeError):
        _type(hands, "abc")
    assert {"shift", "ctrl", "alt"} <= set(released)


def test_real_keys_refused_off_windows_without_a_seam(hands, monkeypatch):
    monkeypatch.setattr(hands, "_send_key_events", hands._SEND_KEY_EVENTS_NATIVE)
    monkeypatch.setattr(hands.os, "name", "posix")
    status, body = _type(hands, "hi")
    assert status == 501 and body["error_code"] == "real_keys_unavailable"


def test_settings_panel_has_plainly_labelled_off_by_default_switch():
    spec = importlib.util.spec_from_file_location("v5_settings", ROOT / "relay_settings.py")
    rs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rs)
    s = rs.BY_KEY["type_real_keys"]
    assert s.default is False and s.kind == "bool" and s.available
    assert "real key" in s.label.lower() and "reject paste" in s.label.lower()


# ====================================================== bar 2/3: pointer travel

class PointerPag:
    def __init__(self, start=(100, 100)):
        self.pos = start
        self.log = []

    def position(self):
        return self.pos

    def moveTo(self, x, y, duration=None):
        self.pos = (x, y)
        self.log.append(("move", x, y))

    def mouseDown(self, x=None, y=None, button="left"):
        self.log.append(("down", x, y))

    def mouseUp(self, x=None, y=None, button="left"):
        self.log.append(("up", x, y))


def _click(hands, monkeypatch, rig, pag, x, y, **body):
    monkeypatch.setattr(hands.pyautogui, "position", pag.position, raising=False)
    monkeypatch.setattr(hands.pyautogui, "moveTo", lambda X, Y, duration=None: (
        pag.moveTo(X, Y), rig.moveTo(X, Y)), raising=False)
    monkeypatch.setattr(hands.pyautogui, "mouseDown", pag.mouseDown, raising=False)
    monkeypatch.setattr(hands.pyautogui, "mouseUp", pag.mouseUp, raising=False)
    monkeypatch.setattr(hands, "_before_effect", lambda: None)
    monkeypatch.setattr(hands.random, "random", random.Random(5).random)
    with hands.app.test_request_context("/click", method="POST", json={"x": x, "y": y, **body}):
        out = hands._do_click({"x": x, "y": y, **body})
    return out


@pytest.mark.parametrize("target", [(1500, 900), (60, 700), (400, 120)])
def test_pointer_travels_a_continuous_path_then_clicks_on_target(hands, rig, monkeypatch, target):
    pag = PointerPag(start=(100, 100))
    out = _click(hands, monkeypatch, rig, pag, *target)
    body = out if isinstance(out, dict) else _body(out)
    assert body["ok"] is True
    moves = [(x, y) for k, x, y in pag.log if k == "move"]
    dist = math.hypot(target[0] - 100, target[1] - 100)
    assert len(moves) >= 6                       # not a jump
    assert moves[0] != target                    # first step is not the target
    pts = [(100, 100)] + moves
    biggest = max(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:]))
    assert biggest < max(60, dist * 0.25)        # every hop is a short visible step
    assert moves[-1] == target                   # lands exactly on the target
    downs = [e for e in pag.log if e[0] == "down"]
    assert downs == [("down", target[0], target[1])]
    assert pag.log[-1] == ("up", target[0], target[1])
    assert pag.log.index(downs[0]) > len(moves) - 1  # all travel happens before the press


def test_pointer_motion_is_the_r9_planner_and_on_by_default(hands, monkeypatch):
    monkeypatch.delenv("HANDS_HUMAN_MOTION", raising=False)
    assert hands._human_motion_default() is True
    assert hands.motion.__name__.endswith("motion")
    assert callable(hands.motion.plan_move)


def test_owner_input_mid_travel_stops_the_pointer_and_never_clicks(hands, rig, monkeypatch):
    rig.owner_in_unit = 5                      # owner moves the mouse after step 5
    pag = PointerPag()
    monkeypatch.setattr(hands.pyautogui, "position", pag.position, raising=False)
    monkeypatch.setattr(hands.pyautogui, "moveTo", lambda X, Y, duration=None: (
        pag.moveTo(X, Y), rig.moveTo(X, Y)), raising=False)
    monkeypatch.setattr(hands.pyautogui, "mouseDown", pag.mouseDown, raising=False)
    monkeypatch.setattr(hands.pyautogui, "mouseUp", pag.mouseUp, raising=False)
    monkeypatch.setattr(hands, "_before_effect", lambda: None)
    with hands.app.test_request_context("/click", method="POST", json={"x": 1500, "y": 900}):
        with pytest.raises(hands._InputRefused) as exc:
            hands._do_click({"x": 1500, "y": 900})
    resp = exc.value.response
    assert _status(resp) == 409
    body = _body(resp)
    assert body["aborted"] is True and body["error_code"] == "input_aborted"
    assert body["abort_reason"] == "owner"
    assert len([e for e in pag.log if e[0] == "move"]) == 5   # stopped right after the owner event
    assert not [e for e in pag.log if e[0] in ("down", "up")]


def test_owner_input_before_first_step_yields_without_moving(hands, rig, monkeypatch):
    pag = PointerPag()
    monkeypatch.setattr(hands.pyautogui, "position", pag.position, raising=False)
    monkeypatch.setattr(hands.pyautogui, "moveTo", lambda X, Y, duration=None: pag.moveTo(X, Y), raising=False)
    monkeypatch.setattr(hands.pyautogui, "mouseDown", pag.mouseDown, raising=False)
    monkeypatch.setattr(hands, "_before_effect", lambda: None)
    # owner counter advances between the guard's baseline and its first sample
    real_init = hands._TypeOwnerGuard.__init__

    def init(self):
        real_init(self)
        rig.owner += 1

    monkeypatch.setattr(hands._TypeOwnerGuard, "__init__", init)
    with hands.app.test_request_context("/click", method="POST", json={"x": 900, "y": 900}):
        with pytest.raises(hands._InputRefused) as exc:
            hands._do_click({"x": 900, "y": 900})
    assert _status(exc.value.response) == 409
    assert _body(exc.value.response)["error_code"] == "owner_active"
    assert pag.log == []


# ================================================================ DPI

@pytest.mark.parametrize("percent", [100, 125, 150])
def test_process_is_dpi_aware_so_coordinates_are_physical_at_any_scale(hands, monkeypatch, percent):
    calls = []

    class FakeUser32:
        def SetProcessDPIAware(self):
            calls.append("aware")
            return 1

        def GetDpiForSystem(self):
            return int(96 * percent / 100)

    class FakeWindll:
        user32 = FakeUser32()

    monkeypatch.setattr(hands.os, "name", "nt")
    monkeypatch.setattr(hands.ctypes, "windll", FakeWindll(), raising=False)
    assert hands._ensure_dpi_aware() == "system" and calls == ["aware"]
    assert hands.dpi_scale_percent() == percent


def test_request_thread_enters_per_monitor_v2(hands, monkeypatch):
    """Retry fix: input requests run in a per-monitor-v2 thread context so clicks stay on
    target after a live scale change (the process system DPI keeps its logon value)."""
    seen = []

    class Fn:
        restype = None
        argtypes = None

        def __call__(self, ctx):
            seen.append(getattr(ctx, "value", ctx))
            return 1234

    class FakeUser32:
        SetThreadDpiAwarenessContext = Fn()

    class FakeWindll:
        user32 = FakeUser32()

    monkeypatch.setattr(hands.os, "name", "nt")
    monkeypatch.setattr(hands.ctypes, "windll", FakeWindll(), raising=False)
    assert hands.enter_physical_dpi_thread() is True
    assert [v - (1 << 64) if v > 0 else v for v in seen] == [-4]
    monkeypatch.setattr(hands.os, "name", "posix")
    assert hands.enter_physical_dpi_thread() is False


def test_every_request_runs_the_physical_dpi_hook(hands):
    names = [f.__name__ for f in hands.app.before_request_funcs.get(None, [])]
    assert "_physical_dpi_for_request" in names
