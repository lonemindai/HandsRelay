"""Tray + settings ticket (2026-11-15): the settings store, the pure tray
state mapping, the gateway's owner pause, and the window API routes.

The Windows tray icon itself (relay_tray.TrayIcon: Shell_NotifyIconW,
its menu and the tinted icons in the notification area) cannot be
exercised headless; its rendering needs a human look on Windows.

    python -m pytest -q tests/test_tray_settings.py
"""
import importlib.util
import itertools
import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import relay_settings as rs  # noqa: E402
import relay_tray as rt  # noqa: E402

_seq = itertools.count()


def _load(rel, tag):
    spec = importlib.util.spec_from_file_location(f"{tag}_{next(_seq)}", ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------ settings store

def test_defaults_equal_todays_behaviour():
    d = rs.defaults()
    assert d == {
        "owner_yield_idle_seconds": 5,
        "claim_ttl_default_seconds": 900,
        "queued_act_hold_seconds": 30,
        "watcher_timeout_seconds": 30,
        "paste_auto_min_chars": 120,
        "close_to_tray": False,
        "tray_icon": True,
    }
    assert rs.changed_from_defaults(d) == {}


def test_defaults_match_the_service_constants():
    gw = _load("services/gateway/app.py", "ts_gw")
    hands_src = (ROOT / "hands/hands_relay_hands.py").read_text(encoding="utf-8")
    d = rs.defaults()
    assert d["claim_ttl_default_seconds"] == gw.CLAIM_TTL_DEFAULT
    assert d["queued_act_hold_seconds"] == gw.HOLD_DEFAULT
    assert d["watcher_timeout_seconds"] == gw.WATCHER_TIMEOUT_DEFAULT
    assert "PASTE_AUTO_MIN_CHARS = 120" in hands_src
    assert 'os.environ.get("HANDS_YIELD_IDLE_SECONDS", "5")' in hands_src
    for s in rs.SETTINGS:
        if s.kind == "int":
            assert s.min <= s.default <= s.max
        assert s.label and s.help


def test_missing_file_is_defaults_without_warning(tmp_path):
    store = rs.SettingsStore(str(tmp_path / "settings.json"))
    assert store.load() == rs.defaults()
    assert store.warnings == []


def test_round_trip(tmp_path):
    path = tmp_path / "sub" / "settings.json"
    store = rs.SettingsStore(str(path))
    store.load()
    ok, errors = store.update({"close_to_tray": True, "owner_yield_idle_seconds": 12})
    assert ok and errors == []
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["version"] == rs.SCHEMA_VERSION
    again = rs.SettingsStore(str(path))
    values = again.load()
    assert values["close_to_tray"] is True
    assert values["owner_yield_idle_seconds"] == 12
    assert values["claim_ttl_default_seconds"] == 900
    assert again.warnings == []
    again.reset()
    assert rs.SettingsStore(str(path)).load() == rs.defaults()


@pytest.mark.parametrize(
    "text",
    ["{not json", "", "[1, 2]", '{"settings": {}}', '{"version": 1, "settings": 5}', '{"version": true, "settings": {}}'],
)
def test_corrupt_file_falls_back_to_defaults_with_warning(tmp_path, text):
    path = tmp_path / "settings.json"
    path.write_text(text, encoding="utf-8")
    store = rs.SettingsStore(str(path))
    assert store.load() == rs.defaults()
    assert store.warnings and "defaults" in store.warnings[0]
    assert path.read_text(encoding="utf-8") == text  # never overwritten on load


def test_newer_version_is_not_guessed(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"version": rs.SCHEMA_VERSION + 1, "settings": {"close_to_tray": True}}))
    store = rs.SettingsStore(str(path))
    assert store.load() == rs.defaults()
    assert "newer" in store.warnings[0]


def test_unknown_keys_ignored_and_bad_values_default_per_key(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "version": 1,
        "settings": {
            "future_toggle": True,
            "close_to_tray": "yes",          # wrong type
            "watcher_timeout_seconds": 999,  # out of range
            "claim_ttl_default_seconds": 60.0,
            "owner_yield_idle_seconds": True,  # bool is not an int
        },
    }))
    store = rs.SettingsStore(str(path))
    values = store.load()
    assert "future_toggle" not in values
    assert values["close_to_tray"] is False
    assert values["watcher_timeout_seconds"] == 30
    assert values["claim_ttl_default_seconds"] == 60
    assert values["owner_yield_idle_seconds"] == 5
    assert len(store.warnings) == 3


def test_update_rejects_bad_values_and_writes_nothing(tmp_path):
    path = tmp_path / "settings.json"
    store = rs.SettingsStore(str(path))
    store.load()
    ok, errors = store.update({"close_to_tray": True, "paste_auto_min_chars": 0, "nope": 1})
    assert not ok and len(errors) == 2
    assert not path.exists()
    assert store.values == rs.defaults()
    assert store.update("x")[0] is False


def test_schema_is_json_safe_and_complete():
    schema = rs.schema()
    json.dumps(schema)
    assert [s["key"] for s in schema] == [s.key for s in rs.SETTINGS]


# ------------------------------------------------------------ tray mapping

def _model(services=("ok", "ok", "ok"), claim=None, idle=None, mode="none"):
    h, g, p = services
    return {
        "services": {"hands": h, "gateway": g, "parser": p},
        "claim": claim,
        "mode": "control" if claim else mode,
        "pc": {"owner_idle_seconds": idle},
    }


@pytest.mark.parametrize(
    "view,variant,tip",
    [
        (None, "stopped", "HandsRelay: stopped"),
        ({"started": False}, "stopped", "HandsRelay: stopped"),
        ({"started": True, "model": None}, "starting", "HandsRelay: starting"),
        ({"started": True, "model": _model()}, "free", "HandsRelay: running, free"),
        ({"started": True, "model": _model(mode="watcher")}, "watching", "HandsRelay: free (agent watching)"),
        ({"started": True, "model": _model(claim={"holder": "muse"})}, "claimed", "HandsRelay: claimed by muse"),
        ({"started": True, "paused": True, "model": _model(claim={"holder": "muse"})}, "paused", "HandsRelay: PAUSED by you - no agent can act"),
        ({"started": True, "model": _model(services=("ok", "down", "unknown"))}, "fault", "HandsRelay: running, gateway down"),
        ({"started": True, "model": _model(services=("ok", "ok", "down"))}, "free", "HandsRelay: running, free; parser down"),
    ],
)
def test_tray_state_variants(view, variant, tip):
    st = rt.tray_state(view)
    assert st["variant"] == variant
    assert st["tooltip"] == tip
    assert st["variant"] in rt.VARIANTS


def test_tray_owner_active_uses_the_yield_threshold():
    view = {"started": True, "model": _model(claim={"holder": "muse"}, idle=2.0)}
    st = rt.tray_state(view, yield_idle_s=5)
    assert st["owner_active"] is True
    assert st["tooltip"].endswith("you are active, agent waits")
    assert rt.icon_key(st) == "claimed-owner"
    assert rt.tray_state(view, yield_idle_s=1)["owner_active"] is False
    assert rt.tray_state({"started": False, "model": _model(idle=0)})["owner_active"] is False


def test_tray_tooltip_is_bounded_and_flattened():
    holder = "agent\n" + "x" * 400
    st = rt.tray_state({"started": True, "model": _model(claim={"holder": holder})}, 5)
    assert len(st["tooltip"]) <= rt.TOOLTIP_MAX
    assert "\n" not in st["tooltip"]


def test_icon_variants_render(tmp_path):
    for variant in rt.VARIANTS:
        for owner in (False, True):
            path = rt.render_icon(str(ROOT / "relay.ico"), variant, owner, str(tmp_path))
            assert os.path.getsize(path) > 0


def test_tray_icon_is_a_no_op_off_windows():
    if sys.platform == "win32":
        pytest.skip("Windows: the real tray is checked by hand")
    tray = rt.TrayIcon(str(ROOT / "relay.ico"), *(lambda *a: None,) * 4)
    assert tray.start() is False
    tray.update(rt.tray_state(None))  # no window: must not raise


# ------------------------------------------------------------ gateway pause

@pytest.fixture
def gw(monkeypatch):
    monkeypatch.setenv("RELAY_GATEWAY_TOKEN", "t")
    monkeypatch.setenv("HANDS_SECRET", "s")
    mod = _load("services/gateway/app.py", "ts_gw_pause")
    pushes = []
    monkeypatch.setattr(mod, "_push_claim_locked", lambda claim, now: pushes.append(claim))
    mod.pushes = pushes
    return mod


def test_pause_releases_claim_and_refuses_claims_and_acts(gw):
    c = gw.app.test_client()
    h = {"X-Relay-Token": "t"}
    assert c.post("/v1/hands/claim", json={"holder": "muse"}, headers=h).get_json()["granted"]
    assert gw.set_paused(True) == "muse"
    assert gw.pushes[-1] is None  # the hands were told the claim is gone
    assert gw._current_claim() is None
    r = c.post("/v1/hands/claim", json={"holder": "muse"}, headers=h)
    assert r.status_code == 423 and r.get_json()["error_code"] == "paused"
    r = c.post("/v1/act", json={"op": "click", "x": 1, "y": 1}, headers=h)
    assert r.status_code == 423 and r.get_json()["error_code"] == "paused"
    assert gw.set_paused(False) is None
    assert not gw.PAUSED
    assert c.post("/v1/hands/claim", json={"holder": "muse"}, headers=h).get_json()["granted"]


# ------------------------------------------------------------ window API

class _Root:
    def after(self, ms, fn, *args):
        pass


@pytest.fixture
def appmod():
    return _load("hands_relay_v4_app.py", "ts_app")


@pytest.fixture
def relay(appmod, monkeypatch, tmp_path):
    monkeypatch.setattr(appmod, "_autostart_enabled", lambda: False)
    monkeypatch.setattr(appmod.relay_settings, "settings_path", lambda: str(tmp_path / "settings.json"))
    return appmod.RelayApp(_Root())


def test_settings_and_pause_routes(appmod, relay, monkeypatch):
    monkeypatch.delenv("HANDS_YIELD_IDLE_SECONDS", raising=False)
    c = appmod.build_ui_server(relay, "key").test_client()
    k = {"X-UI-Key": "key"}
    data = c.get("/api/settings", headers=k).get_json()
    assert data["values"] == rs.defaults() and data["warnings"] == []
    assert c.get("/api/settings").status_code == 401
    r = c.post("/api/settings", json={"settings": {"owner_yield_idle_seconds": 0}}, headers=k)
    assert r.status_code == 400
    r = c.post("/api/settings", json={"settings": {"owner_yield_idle_seconds": 9}}, headers=k)
    assert r.get_json()["values"]["owner_yield_idle_seconds"] == 9
    assert os.environ["HANDS_YIELD_IDLE_SECONDS"] == "9"  # live: the hands read it per act
    assert c.post("/api/settings", json={"reset": True}, headers=k).get_json()["values"] == rs.defaults()
    assert c.post("/api/pause", json={"paused": "yes"}, headers=k).status_code == 400
    assert c.post("/api/pause", json={"paused": True}, headers=k).get_json()["paused"] is True
    view = c.get("/api/state", headers=k).get_json()
    assert view["paused"] is True and view["settings_warnings"] == []


def test_untouched_settings_change_nothing_at_start(relay, monkeypatch):
    monkeypatch.delenv("HANDS_YIELD_IDLE_SECONDS", raising=False)
    monkeypatch.delenv("WATCHER_TIMEOUT_SECONDS", raising=False)
    relay._apply_settings(rs.changed_from_defaults(relay.settings.values))
    assert "HANDS_YIELD_IDLE_SECONDS" not in os.environ
    assert "WATCHER_TIMEOUT_SECONDS" not in os.environ


def test_corrupt_settings_warn_in_the_window(appmod, monkeypatch, tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{oops", encoding="utf-8")
    monkeypatch.setattr(appmod, "_autostart_enabled", lambda: False)
    monkeypatch.setattr(appmod.relay_settings, "settings_path", lambda: str(path))
    relay = appmod.RelayApp(_Root())
    assert relay.view()["settings_warnings"]
    queued = [relay.ui_queue.get_nowait()[1] for _ in range(relay.ui_queue.qsize())]
    assert any("WARNING: Settings file" in line for line in queued)


def test_paused_before_start_is_applied_when_the_gateway_loads(relay, monkeypatch, appmod):
    relay.set_paused(True)
    assert relay.paused and relay._gateway_module is None
