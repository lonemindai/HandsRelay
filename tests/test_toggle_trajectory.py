"""Toggle: trajectory recording (WP4, partner ruling 2026-11-15 18:57),
plus the typed-text sub-option (proposed in toggles wave 1)."""
import importlib.util
import itertools
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("RELAY_GATEWAY_TOKEN", "tr-token")
os.environ.setdefault("HANDS_SECRET", "tr-secret")
TOKEN = {"X-Relay-Token": os.environ["RELAY_GATEWAY_TOKEN"]}
_seq = itertools.count()

import relay_settings as rs  # noqa: E402


def _load(rel, tag):
    spec = importlib.util.spec_from_file_location(f"{tag}_{next(_seq)}", ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def gw(monkeypatch, tmp_path):
    for k in ("HANDSRELAY_TRAJECTORY_RECORDING", "HANDSRELAY_TRAJECTORY_TYPED_TEXT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HANDSRELAY_TRAJECTORY_DIR", str(tmp_path / "traj"))
    mod = _load("services/gateway/app.py", "tr_gw")
    mod._reset_coordination_state()
    monkeypatch.setattr(mod, "_forward_act_once", lambda op, p, path=None: (200, {"ok": True, "op": op}))
    monkeypatch.setattr(mod, "_fetch_screenshot", lambda **kw: (b"\x89PNG-fake", "image/png", None))
    monkeypatch.setattr(mod, "_frame_hash", lambda *a, **k: (_ for _ in ()).throw(mod._HashUnavailable()))
    return mod


def _lines(tmp_path):
    files = list((tmp_path / "traj").glob("*/actions.jsonl"))
    assert len(files) == 1
    return [json.loads(x) for x in files[0].read_text(encoding="utf-8").splitlines()]


def test_settings_registered():
    for key in ("trajectory_recording", "trajectory_record_typed_text"):
        spec = rs.BY_KEY[key]
        assert spec.default is False and spec.available and spec.applies == "live" and spec.risk
    assert "passwords" in rs.BY_KEY["trajectory_recording"].risk


def test_off_writes_nothing_and_responses_unchanged(gw, tmp_path):
    c = gw.app.test_client()
    r = c.post("/v1/act", json={"op": "type", "text": "secret"}, headers=TOKEN)
    assert r.status_code == 200 and r.get_json() == {"ok": True, "op": "type"}
    s = c.get("/v1/screen", headers=TOKEN)
    assert s.get_data() == b"\x89PNG-fake"
    assert not (tmp_path / "traj").exists()


def test_on_records_acts_redacted_and_screens(gw, tmp_path):
    gw.TRAJECTORY_RECORDING = True
    c = gw.app.test_client()
    r = c.post("/v1/act", json={"op": "type", "text": "hunter2"}, headers=TOKEN)
    assert r.get_json() == {"ok": True, "op": "type"}  # the act's answer is unchanged
    c.post("/v1/act", json={"op": "click", "x": 3, "y": 4}, headers=TOKEN)
    c.get("/v1/screen", headers=TOKEN)
    lines = _lines(tmp_path)
    assert lines[0]["op"] == "type" and lines[0]["params"]["text"] == {"redacted": True, "chars": 7}
    assert lines[0]["typed_text"] is False
    assert lines[1]["params"] == {"x": 3, "y": 4} and lines[1]["ok"] is True
    assert lines[2]["kind"] == "screen"
    assert (next((tmp_path / "traj").glob("*")) / lines[2]["file"]).read_bytes() == b"\x89PNG-fake"
    raw = next((tmp_path / "traj").glob("*/actions.jsonl")).read_text(encoding="utf-8")
    assert "hunter2" not in raw


def test_typed_text_sub_option(gw, tmp_path):
    gw.TRAJECTORY_RECORDING = True
    gw.TRAJECTORY_TYPED_TEXT = True
    gw.app.test_client().post("/v1/act", json={"op": "clipboard_set", "text": "abc"}, headers=TOKEN)
    line = _lines(tmp_path)[0]
    assert line["params"]["text"] == "abc" and line["typed_text"] is True


def test_sub_option_alone_records_nothing(gw, tmp_path):
    gw.TRAJECTORY_TYPED_TEXT = True
    gw.app.test_client().post("/v1/act", json={"op": "type", "text": "abc"}, headers=TOKEN)
    assert not (tmp_path / "traj").exists()


def test_queued_and_batch_paths_recorded(gw, tmp_path):
    gw.TRAJECTORY_RECORDING = True
    gw.BATCHED_ACTIONS = True
    c = gw.app.test_client()
    c.post("/v1/act/queued", json={"op": "key", "key": "enter"}, headers=TOKEN)
    c.post("/v1/act/batch", json={"actions": [{"op": "click"}, {"op": "scroll", "clicks": 2}]}, headers=TOKEN)
    assert [x["op"] for x in _lines(tmp_path)] == ["key", "click", "scroll"]


def test_write_failure_never_fails_the_act(gw, tmp_path, monkeypatch):
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x")
    monkeypatch.setattr(gw, "TRAJECTORY_DIR", str(blocker))
    gw.TRAJECTORY_RECORDING = True
    r = gw.app.test_client().post("/v1/act", json={"op": "click"}, headers=TOKEN)
    assert r.status_code == 200


def test_nested_values_bounded(gw):
    out = gw._trajectory_params({"keys": ["ctrl", "s"], "big": ["x" * 5000], "expect_focus": {"hwnd": 5}})
    assert out["keys"] == ["ctrl", "s"] and out["big"] == {"truncated": True}
    assert out["expect_focus"] == {"hwnd": 5}


def test_app_applies_both_live():
    app = _load("hands_relay_v4_app.py", "tr_app")
    relay = app.RelayApp.__new__(app.RelayApp)

    class G:
        TRAJECTORY_RECORDING = False
        TRAJECTORY_TYPED_TEXT = False
    relay._gateway_module, relay._hands_module = G, None
    relay._apply_settings({"trajectory_recording": True, "trajectory_record_typed_text": True})
    assert G.TRAJECTORY_RECORDING and G.TRAJECTORY_TYPED_TEXT
