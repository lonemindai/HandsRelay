"""Toggle: batched actions (WP3, partner ruling 2026-11-15 18:57)."""
import importlib.util
import itertools
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("RELAY_GATEWAY_TOKEN", "ba-token")
os.environ.setdefault("HANDS_SECRET", "ba-secret")
TOKEN = {"X-Relay-Token": os.environ["RELAY_GATEWAY_TOKEN"]}
_seq = itertools.count()

import relay_settings as rs  # noqa: E402


def _load(rel, tag):
    spec = importlib.util.spec_from_file_location(f"{tag}_{next(_seq)}", ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def gw(monkeypatch):
    monkeypatch.delenv("HANDSRELAY_BATCHED_ACTIONS", raising=False)
    mod = _load("services/gateway/app.py", "ba_gw")
    mod._reset_coordination_state()
    return mod


def _fake_forward(script):
    calls = []

    def forward(op, payload):
        calls.append((op, dict(payload)))
        return script(op, payload, len(calls) - 1)

    return forward, calls


def test_setting_registered_off_and_available():
    spec = rs.BY_KEY["batched_actions"]
    assert spec.default is False and spec.available and spec.applies == "live" and spec.risk


def test_off_is_byte_for_byte_an_unknown_route(gw, monkeypatch):
    forward, calls = _fake_forward(lambda op, p, i: (200, {"ok": True}))
    monkeypatch.setattr(gw, "_forward_act", forward)
    assert gw.BATCHED_ACTIONS is False
    c = gw.app.test_client()
    body = {"actions": [{"op": "click", "x": 1, "y": 2}]}
    off = c.post("/v1/act/batch", json=body, headers=TOKEN)
    unknown = c.post("/v1/act/no-such-route", json=body, headers=TOKEN)
    assert off.status_code == unknown.status_code == 404
    assert off.get_data() == unknown.get_data()
    assert sorted(off.headers.items()) == sorted(unknown.headers.items())
    assert calls == []  # nothing forwarded


def test_off_leaves_single_act_unchanged(gw, monkeypatch):
    forward, calls = _fake_forward(lambda op, p, i: (200, {"ok": True, "op": op}))
    monkeypatch.setattr(gw, "_forward_act", forward)
    c = gw.app.test_client()
    r = c.post("/v1/act", json={"op": "click", "x": 1}, headers=TOKEN)
    assert r.status_code == 200 and r.get_json() == {"ok": True, "op": "click"}
    assert calls == [("click", {"x": 1})]


def test_env_flag_enables(monkeypatch):
    monkeypatch.setenv("HANDSRELAY_BATCHED_ACTIONS", "1")
    assert _load("services/gateway/app.py", "ba_env").BATCHED_ACTIONS is True


def test_on_runs_in_order(gw, monkeypatch):
    forward, calls = _fake_forward(lambda op, p, i: (200, {"ok": True}))
    monkeypatch.setattr(gw, "_forward_act", forward)
    gw.BATCHED_ACTIONS = True
    c = gw.app.test_client()
    acts = [{"op": "click", "x": 1, "y": 2}, {"op": "type", "text": "hi"}, {"op": "key", "key": "enter"}]
    r = c.post("/v1/act/batch", json={"actions": acts}, headers=TOKEN).get_json()
    assert calls == [("click", {"x": 1, "y": 2}), ("type", {"text": "hi"}), ("key", {"key": "enter"})]
    assert r["ok"] and r["completed"] == 3 and r["total"] == 3 and r["stopped_at"] is None
    assert [x["index"] for x in r["results"]] == [0, 1, 2]


def test_on_stops_when_owner_takes_over(gw, monkeypatch):
    def script(op, p, i):
        if i == 1:
            return 409, {"ok": False, "error_code": "yield", "error": "yield: user active"}
        return 200, {"ok": True}
    forward, calls = _fake_forward(script)
    monkeypatch.setattr(gw, "_forward_act", forward)
    gw.BATCHED_ACTIONS = True
    acts = [{"op": "click"}, {"op": "click"}, {"op": "click"}]
    r = gw.app.test_client().post(
        "/v1/act/batch", json={"actions": acts, "stop_on_error": False}, headers=TOKEN).get_json()
    assert len(calls) == 2 and r["stopped_at"] == 1 and r["stop_reason"] == "owner"
    assert r["completed"] == 1 and not r["ok"]


def test_on_stops_when_paused(gw, monkeypatch):
    gw.BATCHED_ACTIONS = True
    gw.set_paused(True)
    r = gw.app.test_client().post(
        "/v1/act/batch", json={"actions": [{"op": "click"}, {"op": "click"}]}, headers=TOKEN).get_json()
    assert r["stop_reason"] == "paused" and r["stopped_at"] == 0 and r["completed"] == 0
    gw.set_paused(False)


def test_stop_on_error_default_and_off(gw, monkeypatch):
    script = lambda op, p, i: (400, {"ok": False}) if op == "x" else (200, {"ok": True})  # noqa: E731
    forward, calls = _fake_forward(script)
    monkeypatch.setattr(gw, "_forward_act", forward)
    gw.BATCHED_ACTIONS = True
    c = gw.app.test_client()
    r = c.post("/v1/act/batch", json={"actions": [{"op": "x"}, {"op": "click"}]}, headers=TOKEN).get_json()
    assert r["stop_reason"] == "error" and len(calls) == 1
    r = c.post("/v1/act/batch", json={"actions": [{"op": "x"}, {"op": "click"}], "stop_on_error": False},
               headers=TOKEN).get_json()
    assert r["stop_reason"] is None and r["completed"] == 1 and len(calls) == 3


@pytest.mark.parametrize("body", [{}, {"actions": []}, {"actions": "click"}, {"actions": [1]},
                                  {"actions": [{"op": "click"}] * 51}])
def test_on_validates(gw, body):
    gw.BATCHED_ACTIONS = True
    assert gw.app.test_client().post("/v1/act/batch", json=body, headers=TOKEN).status_code == 400


def test_on_requires_token(gw):
    gw.BATCHED_ACTIONS = True
    assert gw.app.test_client().post("/v1/act/batch", json={"actions": [{"op": "click"}]}).status_code == 401


def test_app_applies_setting_live():
    app = _load("hands_relay_v4_app.py", "ba_app")
    relay = app.RelayApp.__new__(app.RelayApp)

    class G:
        BATCHED_ACTIONS = False
    relay._gateway_module, relay._hands_module = G, None
    relay._apply_settings({"batched_actions": True})
    assert G.BATCHED_ACTIONS is True
