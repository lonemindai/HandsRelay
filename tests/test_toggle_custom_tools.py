"""Toggle: custom-tools registry (WP2, partner ruling 2026-11-15 18:57).
Tools are local, declared by the owner in a file, never fetched."""
import importlib.util
import itertools
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("RELAY_GATEWAY_TOKEN", "ct-token")
os.environ.setdefault("HANDS_SECRET", "ct-secret")
TOKEN = {"X-Relay-Token": os.environ["RELAY_GATEWAY_TOKEN"]}
_seq = itertools.count()

import relay_settings as rs  # noqa: E402


def _load(rel, tag):
    spec = importlib.util.spec_from_file_location(f"{tag}_{next(_seq)}", ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write(path, tools):
    path.write_text(json.dumps({"version": 1, "tools": tools}), encoding="utf-8")


@pytest.fixture
def gw(monkeypatch, tmp_path):
    monkeypatch.delenv("HANDSRELAY_CUSTOM_TOOLS", raising=False)
    monkeypatch.setenv("HANDSRELAY_TOOLS_FILE", str(tmp_path / "tools.json"))
    mod = _load("services/gateway/app.py", "ct_gw")
    mod._reset_coordination_state()
    _write(tmp_path / "tools.json", [
        {"name": "echo", "description": "print args", "allow_args": True,
         "command": [sys.executable, "-c", "import sys; print('|'.join(sys.argv[1:]))"]},
        {"name": "fixed", "command": [sys.executable, "-c", "print('hi')"]},
        {"name": "fails", "command": [sys.executable, "-c", "import sys; sys.stderr.write('bad'); sys.exit(3)"]},
        {"name": "slow", "timeout_seconds": 1, "command": [sys.executable, "-c", "import time; time.sleep(5)"]},
        {"name": "big", "command": [sys.executable, "-c", "print('x' * 70000)"]},
        {"name": "BAD NAME", "command": ["x"]},
        {"name": "nocmd", "command": []},
    ])
    return mod


def test_registered_off_and_available():
    s = rs.BY_KEY["custom_tools_registry"]
    assert s.default is False and s.available and s.risk and "cannot add" in s.help


def test_off_routes_identical_to_unknown(gw):
    c = gw.app.test_client()
    unknown = c.get("/v1/no-such-route", headers=TOKEN)
    for r in (c.get("/v1/tools", headers=TOKEN), c.post("/v1/tools/fixed/run", json={}, headers=TOKEN)):
        assert r.status_code == 404 and r.get_data() == unknown.get_data()


def test_list_shows_valid_tools_and_warnings(gw):
    gw.CUSTOM_TOOLS = True
    body = gw.app.test_client().get("/v1/tools", headers=TOKEN).get_json()
    assert [t["name"] for t in body["tools"]] == ["echo", "fixed", "fails", "slow", "big"]
    assert "command" not in body["tools"][0]
    assert len(body["warnings"]) == 2


def test_run_args_and_no_shell(gw):
    gw.CUSTOM_TOOLS = True
    c = gw.app.test_client()
    r = c.post("/v1/tools/echo/run", json={"args": ["a b", "; rm -rf /", "$(x)"]}, headers=TOKEN).get_json()
    assert r["ok"] and r["exit_code"] == 0 and r["stdout"].strip() == "a b|; rm -rf /|$(x)"
    assert c.post("/v1/tools/fixed/run", json={"args": ["x"]}, headers=TOKEN).status_code == 400
    assert c.post("/v1/tools/echo/run", json={"args": "x"}, headers=TOKEN).status_code == 400
    assert c.post("/v1/tools/echo/run", json={"args": ["x"] * 17}, headers=TOKEN).status_code == 400


def test_run_failure_timeout_truncation_unknown(gw):
    gw.CUSTOM_TOOLS = True
    c = gw.app.test_client()
    r = c.post("/v1/tools/fails/run", json={}, headers=TOKEN).get_json()
    assert not r["ok"] and r["exit_code"] == 3 and r["stderr"] == "bad"
    r = c.post("/v1/tools/slow/run", json={}, headers=TOKEN).get_json()
    assert r["timed_out"] and not r["ok"]
    r = c.post("/v1/tools/big/run", json={}, headers=TOKEN).get_json()
    assert r["truncated"] and len(r["stdout"]) == gw.TOOL_OUTPUT_MAX
    assert c.post("/v1/tools/nope/run", json={}, headers=TOKEN).status_code == 404
    assert c.post("/v1/tools/fixed/run", json={}).status_code == 401


def test_file_reread_each_request(gw, tmp_path):
    gw.CUSTOM_TOOLS = True
    c = gw.app.test_client()
    _write(tmp_path / "tools.json", [])
    assert c.get("/v1/tools", headers=TOKEN).get_json()["tools"] == []
    (tmp_path / "tools.json").write_text("{broken", encoding="utf-8")
    body = c.get("/v1/tools", headers=TOKEN).get_json()
    assert body["tools"] == [] and "could not be read" in body["warnings"][0]


def test_paused_refuses(gw):
    gw.CUSTOM_TOOLS = True
    gw.set_paused(True)
    try:
        assert gw.app.test_client().post("/v1/tools/fixed/run", json={}, headers=TOKEN).status_code == 423
    finally:
        gw.set_paused(False)


def test_no_network_code_path():
    src = (ROOT / "services" / "gateway" / "app.py").read_text(encoding="utf-8")
    block = src[src.index("# Toggle: custom-tools registry"):src.index("def _param_keys(")]
    assert "requests." not in block and "urllib" not in block and "shell=False" in block


def test_app_applies_live():
    app = _load("hands_relay_v4_app.py", "ct_app")
    relay = app.RelayApp.__new__(app.RelayApp)

    class G:
        CUSTOM_TOOLS = False
    relay._gateway_module, relay._hands_module = G, None
    relay._apply_settings({"custom_tools_registry": True})
    assert G.CUSTOM_TOOLS is True
