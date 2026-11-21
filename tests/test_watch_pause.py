"""Item 32 (partner ruling 2026-11-15 19:02): per-watcher pause state.

watching / paused / stopped (released or timed out), resume on the next
touch, dashboard + tray mapping, and backward compatibility for clients
that never pause."""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("RELAY_GATEWAY_TOKEN", "wp-token")
os.environ.setdefault("HANDS_SECRET", "wp-secret")
TOKEN = {"X-Relay-Token": os.environ["RELAY_GATEWAY_TOKEN"]}

import relay_tray as rt  # noqa: E402


def _load(rel, tag):
    spec = importlib.util.spec_from_file_location(tag, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def gw():
    return _load("services/gateway/app.py", "wp_gateway")


@pytest.fixture(scope="module")
def appmod():
    return _load("hands_relay_v4_app.py", "wp_app")


@pytest.fixture(autouse=True)
def _clean(gw):
    gw._reset_coordination_state()
    yield
    gw._reset_coordination_state()


def test_never_pausing_client_unchanged(gw):
    gw._touch_watcher("eyes", now=1000.0)
    live = gw._current_watchers(now=1010.0)
    assert [w["holder"] for w in live] == ["eyes"]
    assert live[0]["state"] == "watching"
    assert {"holder", "since", "last_seen", "timeout_seconds", "state"} <= set(live[0])
    assert live[0]["paused"] is False  # R13 observation fields ride alongside
    assert gw._current_watchers(now=1030.5) == []  # lapses exactly as before


def test_pause_outlives_poll_timeout_then_stops(gw):
    gw._touch_watcher("eyes", now=1000.0)
    e = gw._pause_watcher("eyes", reason="owner driving", now=1005.0)
    assert e["state"] == "paused" and e["pause_timeout_seconds"] == 900
    assert e["pause_reason"] == "owner driving"
    assert gw._current_watchers(now=1500.0)[0]["state"] == "paused"
    assert gw._current_watchers(now=1905.5) == []
    stopped = gw._stopped_watcher_list(now=1906.0)
    assert stopped == [{"holder": "eyes", "state": "stopped", "how": "timed_out", "stopped_at": 1905.5}]


def test_pause_timeout_clamped(gw):
    assert gw._pause_watcher("a", timeout_seconds=1, now=0.0)["pause_timeout_seconds"] == 30
    assert gw._pause_watcher("b", timeout_seconds=99999, now=0.0)["pause_timeout_seconds"] == 3600


def test_touch_resumes(gw):
    gw._pause_watcher("eyes", reason="x", now=1000.0)
    e = gw._touch_watcher("eyes", now=1100.0)
    assert e["state"] == "watching" and "paused_at" not in e and "pause_reason" not in e
    assert e["since"] == 1000.0  # same watching session
    assert gw._current_watchers(now=1131.0) == []  # back on the poll timeout


def test_release_marks_stopped_and_touch_clears_it(gw):
    c = gw.app.test_client()
    c.post("/v1/watch", json={"holder": "eyes"}, headers=TOKEN)
    assert c.post("/v1/watch/release", json={"holder": "eyes"}, headers=TOKEN).get_json()["released"]
    body = c.get("/v1/watch", headers=TOKEN).get_json()
    assert body["watchers"] == []
    assert [(w["holder"], w["how"]) for w in body["watchers_stopped"]] == [("eyes", "released")]
    c.post("/v1/watch", json={"holder": "eyes"}, headers=TOKEN)
    assert c.get("/v1/watch", headers=TOKEN).get_json()["watchers_stopped"] == []


def test_stopped_record_forgotten_after_keep(gw):
    gw._touch_watcher("eyes", now=0.0)
    gw._release_watcher("eyes", now=10.0)
    assert gw._stopped_watcher_list(now=309.0)
    assert gw._stopped_watcher_list(now=310.5) == []


def test_pause_route(gw, monkeypatch):
    monkeypatch.setattr(gw, "_reachability", lambda: (False, False, None))
    c = gw.app.test_client()
    assert c.post("/v1/watch/pause", json={}, headers=TOKEN).status_code == 400
    assert c.post("/v1/watch/pause", json={"holder": "m"}).status_code == 401  # token-gated
    r = c.post("/v1/watch/pause", json={"holder": "m", "reason": "r" * 500}, headers=TOKEN)
    assert r.status_code == 200 and r.get_json()["watcher"]["state"] == "paused"
    assert len(r.get_json()["watcher"]["pause_reason"]) == 120
    info = c.get("/v1/info", headers=TOKEN).get_json()
    assert info["watchers"][0]["state"] == "paused" and info["watchers_stopped"] == []
    assert gw._current_claim() is None  # pausing never touches the claim


def _info(watchers, stopped=None):
    return {"hands_reachable": True, "parser_reachable": True, "pc_state": None,
            "watchers": watchers, "watchers_stopped": stopped or [], "watcher_timeout_seconds": 30}


def test_dashboard_model_states(appmod):
    w = [{"holder": "a", "state": "watching", "last_seen": 95.0},
         {"holder": "b", "state": "paused", "last_seen": 90.0, "pause_reason": "owner"}]
    m = appmod.build_dashboard_model(_info(w, [{"holder": "c", "how": "released", "stopped_at": 40.0}]),
                                     None, [], None, [], 100.0)
    assert m["mode"] == "watcher" and m["watchers"] == ["a"] and m["watchers_paused"] == ["b"]
    assert [x["state"] for x in m["watcher_activity"]] == ["watching", "paused"]
    assert m["watcher_activity"][1]["pause_reason"] == "owner"
    assert m["watchers_stopped"] == [{"holder": "c", "how": "released", "stopped_ago_s": 60.0}]
    m2 = appmod.build_dashboard_model(_info(w[1:]), None, [], None, [], 100.0)
    assert m2["mode"] == "watcher-paused" and m2["watchers"] == []
    assert "Mode: watcher paused" in appmod.format_diagnostics(m2, "now")


def test_dashboard_model_old_gateway_without_state(appmod):
    m = appmod.build_dashboard_model(
        {"hands_reachable": True, "parser_reachable": True, "watchers": [{"holder": "a", "last_seen": 99.0}]},
        None, [], None, [], 100.0)
    assert m["mode"] == "watcher" and m["watchers"] == ["a"] and m["watchers_stopped"] == []


def test_tray_paused_watcher_is_free():
    base = {"services": {"hands": "ok", "gateway": "ok", "parser": "ok"}, "claim": None, "pc": {}}
    st = rt.tray_state({"started": True, "model": dict(base, mode="watcher-paused")})
    assert st["variant"] == "free" and "paused watching" in st["tooltip"]
    assert rt.tray_state({"started": True, "model": dict(base, mode="watcher")})["variant"] == "watching"


def test_ui_renders_paused_and_stopped():
    js = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
    assert 'mode === "watcher-paused"' in js and "watchers_stopped" in js and "timed out" in js
