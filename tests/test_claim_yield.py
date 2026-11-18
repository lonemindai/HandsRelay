"""Wishlist claim-yield (muse 2026-11-16): a held claim must not look
like a bare 'yield'. Explicit claim_held errors, read-only status,
stale force-release, non-holder release refused."""
import importlib.util
import itertools
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_seq = itertools.count()
H = {"X-Relay-Token": "t"}


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def gw(monkeypatch):
    monkeypatch.setenv("RELAY_GATEWAY_TOKEN", "t")
    monkeypatch.setenv("HANDS_SECRET", "s")
    spec = importlib.util.spec_from_file_location(f"cy_gw_{next(_seq)}", ROOT / "services/gateway/app.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "_push_claim_locked", lambda claim, now: None)
    clock = Clock()
    monkeypatch.setattr(mod.time, "time", clock)
    mod._clock = clock
    mod._hands_calls = []

    def fake_once(op, payload, path=None):
        mod._hands_calls.append((op, payload))
        return mod._hands_answer

    mod._hands_answer = (200, {"ok": True})
    monkeypatch.setattr(mod, "_forward_act_once", fake_once)
    return mod


def _claim(c, holder, ttl=900):
    r = c.post("/v1/hands/claim", json={"holder": holder, "ttl_seconds": ttl}, headers=H)
    assert r.status_code == 200 and r.get_json()["granted"], r.get_json()
    return r


def test_blocked_act_returns_explicit_claim_error(gw):
    c = gw.app.test_client()
    _claim(c, "agent:muse:main")
    gw._clock.t += 120
    r = c.post("/v1/act", json={"op": "click", "x": 1, "y": 2, "holder": "agent:other:main"}, headers=H)
    body = r.get_json()
    assert r.status_code == 409
    assert body["error_code"] == "claim_held"
    assert "agent:muse:main" in body["error"] and "age 120s" in body["error"] and "expires in 780s" in body["error"]
    assert body["claim_info"]["holder"] == "agent:muse:main"
    assert body["claim_info"]["age_s"] == 120 and body["claim_info"]["expires_in_s"] == 780
    assert gw._hands_calls == []  # never reached the hands
    # Header form works too.
    r = c.post("/v1/act", json={"op": "click", "x": 1, "y": 2}, headers={**H, "X-Hands-Holder": "agent:other:main"})
    assert r.status_code == 409 and r.get_json()["error_code"] == "claim_held"


def test_holder_act_forwarded_without_holder_field(gw):
    c = gw.app.test_client()
    _claim(c, "agent:muse:main")
    r = c.post("/v1/act", json={"op": "click", "x": 1, "y": 2, "holder": "agent:muse:main"}, headers=H)
    assert r.status_code == 200
    assert gw._hands_calls == [("click", {"x": 1, "y": 2})]  # holder stripped


def test_unnamed_caller_yield_names_the_holder(gw):
    c = gw.app.test_client()
    _claim(c, "agent:muse:main")
    gw._hands_answer = (409, {"ok": False, "error": "user active — control yielded", "error_code": "owner_active"})
    r = c.post("/v1/act", json={"op": "click", "x": 1, "y": 2}, headers=H)
    body = r.get_json()
    assert r.status_code == 409 and body["error_code"] == "claim_held"  # held claim explains the yield
    assert body["claim_held_by"] == "agent:muse:main"
    assert "claimed by 'agent:muse:main'" in body["error"]


def test_status_is_read_only(gw):
    c = gw.app.test_client()
    r = c.get("/v1/hands/claim/status", headers=H)
    assert r.get_json()["claim"] is None and r.get_json()["claim_info"] is None
    _claim(c, "agent:muse:main", ttl=600)
    gw._clock.t += 60
    before = dict(gw._claim)
    for _ in range(2):
        info = c.get("/v1/hands/claim/status", headers=H).get_json()["claim_info"]
        assert info["holder"] == "agent:muse:main"
        assert info["age_s"] == 60 and info["expires_in_s"] == 540 and info["stale"] is False
    assert "claim_info" not in c.get("/v1/hands/claim", headers=H).get_json()  # legacy shape kept
    assert gw._claim == before  # nothing refreshed or released


def test_stale_claim_force_release(gw):
    c = gw.app.test_client()
    _claim(c, "agent:muse:main", ttl=3600)
    gw._clock.t += 100
    r = c.post("/v1/hands/release", json={"holder": "agent:ops:main", "force": True}, headers=H)
    assert r.status_code == 409 and r.get_json()["error_code"] == "claim_not_stale"
    assert gw._claim is not None
    gw._clock.t += gw.CLAIM_TTL_DEFAULT  # idle past the TTL, lease not yet expired
    assert c.get("/v1/hands/claim/status", headers=H).get_json()["claim_info"]["stale"] is True
    r = c.post("/v1/hands/release", json={"holder": "agent:ops:main", "force": True}, headers=H)
    body = r.get_json()
    assert r.status_code == 200 and body["released"] and body["forced"] and body["released_holder"] == "agent:muse:main"
    assert gw._claim is None


def test_refresh_resets_staleness(gw):
    c = gw.app.test_client()
    _claim(c, "agent:muse:main", ttl=3600)
    gw._clock.t += gw.CLAIM_TTL_DEFAULT - 10
    _claim(c, "agent:muse:main", ttl=3600)  # holder renews
    gw._clock.t += 20
    r = c.post("/v1/hands/release", json={"holder": "agent:ops:main", "force": True}, headers=H)
    assert r.status_code == 409


def test_non_holder_release_refused(gw):
    c = gw.app.test_client()
    _claim(c, "agent:muse:main")
    r = c.post("/v1/hands/release", json={"holder": "agent:ops:main"}, headers=H)
    body = r.get_json()
    assert r.status_code == 403 and body["error_code"] == "not_holder"
    assert body["claim_info"]["holder"] == "agent:muse:main"
    assert gw._claim is not None
    # Even stale: without force it is still refused (but says force is possible).
    gw._clock.t += gw.CLAIM_TTL_DEFAULT - 1
    _ = c.get("/v1/hands/claim", headers=H)
    r = c.post("/v1/hands/release", json={"holder": "agent:ops:main"}, headers=H)
    assert r.status_code == 403
    # Holder may release any time.
    r = c.post("/v1/hands/release", json={"holder": "agent:muse:main"}, headers=H)
    assert r.status_code == 200 and r.get_json()["released"] is True


def test_competing_claim_409_is_explicit(gw):
    c = gw.app.test_client()
    _claim(c, "agent:muse:main")
    gw._clock.t += 30
    r = c.post("/v1/hands/claim", json={"holder": "agent:ops:main"}, headers=H)
    body = r.get_json()
    assert r.status_code == 409 and body["error_code"] == "claim_held"
    assert "age 30s" in body["error"] and body["claim_info"]["expires_in_s"] == 870
