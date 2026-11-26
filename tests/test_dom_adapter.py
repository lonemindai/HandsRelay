"""Browser DOM adapter (partner ruling 2026-11-15): fake-CDP tests.

A stdlib fake of the browser's debug endpoint (HTTP /json/list plus a
websocket that answers CDP calls) runs on 127.0.0.1. Nothing here starts
a real browser.
"""
import base64
import hashlib
import importlib.util
import itertools
import json
import os
import socket
import socketserver
import struct
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
from base_fixtures import base_bytes

BASE_COMMIT = "1b585ac"  # v4-rc1 just before the DOM merge (wave 1 + nested + uia): the gateway without the adapter
_seq = itertools.count()


def _load(path, tag):
    spec = importlib.util.spec_from_file_location(f"{tag}_{next(_seq)}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------- fake CDP

PAGE = {
    "url": "https://example.test/login", "title": "Sign in",
    "nodes": [
        {"d": 0, "tag": "h1", "sel": "h1", "text": "Sign in"},
        {"d": 1, "tag": "input", "sel": "#user", "type": "text", "value": "[REDACTED]"},
        # A misbehaving page script leaking a password must still be redacted.
        {"d": 1, "tag": "input", "sel": "#pw", "type": "password", "value": "hunter2"},
        {"d": 1, "tag": "button", "sel": "#go", "text": "Go"},
    ] + [{"d": 2, "tag": "p", "sel": f"p:nth-of-type({i})", "text": "x" * 80} for i in range(400)],
    "truncated": False,
}


def _ws_frame(opcode, payload, fin=True):
    head = bytes([(0x80 if fin else 0) | opcode])
    n = len(payload)
    if n < 126:
        head += bytes([n])
    elif n < 65536:
        head += bytes([126]) + struct.pack(">H", n)
    else:
        head += bytes([127]) + struct.pack(">Q", n)
    return head + payload


class FakeBrowser:
    def __init__(self, ws_host="127.0.0.1", elements=None):
        self.calls = []
        self.ws_host = ws_host
        self.elements = elements if elements is not None else {
            "#go": {"found": True, "tag": "button", "x": 50, "y": 60, "w": 10, "h": 10, "editable": False, "disabled": False},
            "#user": {"found": True, "tag": "input", "x": 5, "y": 6, "w": 100, "h": 20, "editable": True, "disabled": False},
        }
        fake = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                fake._handle(self.request)

        socketserver.ThreadingTCPServer.allow_reuse_address = True
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def _tabs(self):
        return [
            {"id": "T1", "type": "page", "title": "Sign in", "url": "https://example.test/login",
             "webSocketDebuggerUrl": f"ws://{self.ws_host}:{self.port}/devtools/page/T1"},
            {"id": "W1", "type": "service_worker", "title": "sw", "url": "https://example.test/sw.js",
             "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/page/W1"},
        ]

    def _handle(self, sock):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                return
            data += chunk
        head = data.split(b"\r\n\r\n", 1)[0].decode()
        line = head.split("\r\n")[0]
        if "Upgrade: websocket" not in head:
            body = json.dumps(self._tabs()).encode() if line.startswith("GET /json/list") else b"{}"
            sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
            return
        key = [h.split(": ", 1)[1] for h in head.split("\r\n") if h.lower().startswith("sec-websocket-key")][0]
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        sock.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n")
        buf = b""

        def need(n):
            nonlocal buf
            while len(buf) < n:
                c = sock.recv(65536)
                if not c:
                    raise EOFError
                buf += c
            out, buf = buf[:n], buf[n:]
            return out

        try:
            while True:
                b1, b2 = need(2)
                assert b2 & 0x80, "client frames must be masked"
                n = b2 & 0x7F
                if n == 126:
                    n = struct.unpack(">H", need(2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", need(8))[0]
                mask = need(4)
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(need(n)))
                if b1 & 0x0F == 0x8:
                    return
                if b1 & 0x0F != 0x1:
                    continue
                msg = json.loads(payload)
                self.calls.append(msg)
                result = self._answer(msg)
                # An unrelated event and a ping first: the client must skip both.
                sock.sendall(_ws_frame(0x1, json.dumps({"method": "Page.loadEventFired", "params": {}}).encode()))
                sock.sendall(_ws_frame(0x9, b"hi"))
                out = json.dumps({"id": msg["id"], **result}).encode()
                half = len(out) // 2  # fragmented reply
                sock.sendall(_ws_frame(0x1, out[:half], fin=False) + _ws_frame(0x0, out[half:]))
        except (EOFError, OSError):
            return

    def _answer(self, msg):
        method, params = msg["method"], msg.get("params", {})
        if method == "Runtime.evaluate":
            expr = params["expression"]
            if "walk(document.body" in expr:
                values = '"values": true' in expr
                page = json.loads(json.dumps(PAGE))
                if values:
                    page["nodes"][1]["value"] = "alice"
                return {"result": {"result": {"type": "object", "value": page}}}
            sel = json.loads(expr[expr.rindex("})(") + 3:-2])["selector"]
            return {"result": {"result": {"type": "object", "value": self.elements.get(sel, {"found": False})}}}
        if method.startswith("Input."):
            return {"result": {}}
        return {"error": {"code": -32601, "message": f"{method} wasn't found"}}


@pytest.fixture
def browser(monkeypatch):
    fake = FakeBrowser()
    monkeypatch.setenv("RELAY_DOM_CDP_PORT", str(fake.port))
    yield fake
    fake.close()


@pytest.fixture
def dom():
    return _load(ROOT / "services/gateway/dom_adapter.py", "dom")


# --------------------------------------------------------------- adapter

def test_host_is_loopback_and_not_configurable(dom, monkeypatch):
    monkeypatch.setenv("RELAY_DOM_CDP_HOST", "0.0.0.0")
    assert dom.CDP_HOST == "127.0.0.1"
    src = (ROOT / "services/gateway/dom_adapter.py").read_text()
    assert "subprocess" not in src and "Popen" not in src and "os.system" not in src  # never launches a browser


def test_list_tabs_pages_only(dom, browser):
    assert dom.list_tabs() == [{"id": "T1", "title": "Sign in", "url": "https://example.test/login"}]


def test_no_browser_is_a_clear_503(dom, monkeypatch):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listening
    monkeypatch.setenv("RELAY_DOM_CDP_PORT", str(port))
    with pytest.raises(dom.DomError) as e:
        dom.list_tabs()
    assert e.value.code == "no_browser" and e.value.status == 503
    assert "never starts your browser" in e.value.message


def test_debugger_url_off_loopback_is_refused(dom, monkeypatch):
    fake = FakeBrowser(ws_host="10.0.0.9")
    monkeypatch.setenv("RELAY_DOM_CDP_PORT", str(fake.port))
    try:
        with pytest.raises(dom.DomError) as e:
            dom.snapshot("T1")
        assert e.value.code == "not_loopback"
        assert fake.calls == []  # never connected
    finally:
        fake.close()


def test_snapshot_redacts_and_caps(dom, browser):
    snap = dom.snapshot("T1", max_chars=2000)
    byname = {n["sel"]: n for n in snap["nodes"]}
    assert byname["#pw"]["value"] == "[REDACTED:password]"
    assert byname["#user"]["value"] == "[REDACTED]"
    assert snap["truncated"] is True and snap["values_redacted"] is True
    assert len(json.dumps(snap["nodes"])) <= 2000 + len(snap["nodes"]) * 2
    assert "hunter2" not in json.dumps(snap)


def test_snapshot_include_values_still_hides_passwords(dom, browser):
    snap = dom.snapshot("T1", max_chars=200000, include_values=True)
    byname = {n["sel"]: n for n in snap["nodes"]}
    assert byname["#user"]["value"] == "alice"
    assert byname["#pw"]["value"] == "[REDACTED:password]"
    assert snap["truncated"] is False


def test_unknown_tab_and_element(dom, browser):
    with pytest.raises(dom.DomError) as e:
        dom.snapshot("nope")
    assert e.value.code == "no_tab" and e.value.status == 404
    with pytest.raises(dom.DomError) as e:
        dom.click("T1", "#missing")
    assert e.value.code == "no_element"


def test_click_dispatches_real_mouse_events(dom, browser):
    out = dom.click("T1", "#go")
    assert out == {"tag": "button", "x": 50, "y": 60}
    kinds = [c["params"]["type"] for c in browser.calls if c["method"] == "Input.dispatchMouseEvent"]
    assert kinds == ["mouseMoved", "mousePressed", "mouseReleased"]


def test_type_needs_an_editable_target(dom, browser):
    assert dom.type_text("T1", "#user", "hello") == {"tag": "input", "chars": 5}
    assert {"text": "hello"} in [c["params"] for c in browser.calls if c["method"] == "Input.insertText"]
    with pytest.raises(dom.DomError) as e:
        dom.type_text("T1", "#go", "x")
    assert e.value.code == "not_editable"


def test_selector_is_json_encoded_into_the_page(dom, browser):
    nasty = '#a"); alert(1); ("'
    with pytest.raises(dom.DomError):
        dom.click("T1", nasty)
    expr = [c for c in browser.calls if c["method"] == "Runtime.evaluate"][-1]["params"]["expression"]
    assert json.dumps({"selector": nasty, "focus": False}) in expr


# --------------------------------------------------------------- gateway

def _gateway(monkeypatch, path=ROOT / "services/gateway/app.py"):
    monkeypatch.setenv("RELAY_GATEWAY_TOKEN", "t")
    monkeypatch.setenv("HANDS_SECRET", "s")
    mod = _load(path, "dom_gw")
    monkeypatch.setattr(mod, "_push_claim_locked", lambda claim, now: None)
    return mod


H = {"X-Relay-Token": "t"}


def test_off_every_dom_route_refuses_clearly(monkeypatch, browser):
    gw = _gateway(monkeypatch)
    assert gw.DOM_ADAPTER_ENABLED is False
    c = gw.app.test_client()
    for method, path in (("get", "/v1/dom/tabs"), ("post", "/v1/dom/snapshot"), ("post", "/v1/dom/act")):
        r = getattr(c, method)(path, json={"holder": "m", "tab_id": "T1", "op": "click", "selector": "#go"}, headers=H)
        assert r.status_code == 403 and r.get_json()["error_code"] == "dom_adapter_off", path
    assert browser.calls == []  # never attached while off


def test_off_is_byte_for_byte_the_old_gateway(monkeypatch, tmp_path):
    """Every non-DOM route answers exactly what the pre-adapter gateway did."""
    old_path = tmp_path / "app_old.py"
    old_path.write_bytes(base_bytes(BASE_COMMIT, "services/gateway/app.py"))
    import requests
    import time as _time

    class Dead(requests.RequestException):
        pass

    def dead(*a, **k):
        raise Dead("no hands in tests")

    monkeypatch.setattr(requests, "get", dead)
    monkeypatch.setattr(requests, "post", dead)
    monkeypatch.setattr(requests.Session, "request", dead)
    monkeypatch.setattr(_time, "time", lambda: 1_800_000_000.0)
    script = [
        ("get", "/health", None), ("get", "/v1/info", None), ("get", "/v1/hands/claim", None),
        ("post", "/v1/hands/claim", {"holder": "m", "purpose": "p"}), ("get", "/v1/hands/claim", None),
        ("post", "/v1/act", {"op": "click", "x": 1, "y": 2}), ("post", "/v1/act", {"op": "nope"}),
        ("get", "/v1/watch", None), ("post", "/v1/hands/release", {"holder": "m"}),
        ("get", "/v1/nonexistent", None), ("get", "/v1/info", {"bad": "token"}),
    ]

    def run(gw):
        c = gw.app.test_client()
        out = []
        for method, path, body in script:
            hdr = {"X-Relay-Token": "wrong"} if body == {"bad": "token"} else H
            r = getattr(c, method)(path, json=body, headers=hdr)
            out.append((r.status_code, r.get_data(), sorted((k, v) for k, v in r.headers if k != "Date")))
        return out

    new = run(_gateway(monkeypatch))
    old = run(_gateway(monkeypatch, old_path))
    from r13_contract import compare
    compare([p for _, p, _ in script], new, old)
    old_rules = {r.rule for r in _gateway(monkeypatch, old_path).app.url_map.iter_rules()}
    new_rules = {r.rule for r in _gateway(monkeypatch).app.url_map.iter_rules()}
    # claim-yield (2026-11-16) adds the read-only claim status route.
    assert new_rules - old_rules == {"/v1/dom/tabs", "/v1/dom/snapshot", "/v1/dom/act", "/v1/hands/claim/status"}


@pytest.fixture
def gw_on(monkeypatch, browser):
    gw = _gateway(monkeypatch)
    gw.set_dom_adapter(True)
    monkeypatch.setenv("HANDS_YIELD_IDLE_SECONDS", "5")
    gw.idle = {"v": 60.0}
    monkeypatch.setattr(gw, "_fetch_pc_state", lambda: None if gw.idle["v"] is None
                        else {"ok": True, "owner_idle_seconds": gw.idle["v"]})
    gw.browser = browser
    return gw


def test_on_reads_tabs_and_snapshot(gw_on):
    c = gw_on.app.test_client()
    r = c.get("/v1/dom/tabs", headers=H)
    assert r.status_code == 200 and r.get_json()["tabs"][0]["id"] == "T1"
    r = c.post("/v1/dom/snapshot", json={"tab_id": "T1"}, headers=H)
    body = r.get_json()
    assert r.status_code == 200 and body["values_redacted"] is True
    assert "hunter2" not in r.get_data(as_text=True)
    r = c.post("/v1/dom/snapshot", json={"tab_id": "T1", "include_values": "yes"}, headers=H)
    assert r.get_json()["values_redacted"] is True  # only a JSON true opts in


def test_act_requires_the_claim(gw_on):
    c = gw_on.app.test_client()
    act = {"holder": "muse", "tab_id": "T1", "op": "click", "selector": "#go"}
    r = c.post("/v1/dom/act", json=act, headers=H)
    assert r.status_code == 409 and r.get_json()["error_code"] == "claim_required"
    c.post("/v1/hands/claim", json={"holder": "other"}, headers=H)
    r = c.post("/v1/dom/act", json=act, headers=H)
    assert r.status_code == 409 and r.get_json()["claim"]["holder"] == "other"
    assert not [x for x in gw_on.browser.calls if x["method"].startswith("Input.")]
    c.post("/v1/hands/release", json={"holder": "other"}, headers=H)
    c.post("/v1/hands/claim", json={"holder": "muse"}, headers=H)
    r = c.post("/v1/dom/act", json=act, headers=H)
    assert r.status_code == 200 and r.get_json()["op"] == "click"
    r = c.post("/v1/dom/act", json={**act, "op": "type", "selector": "#user", "text": "hi"}, headers=H)
    assert r.status_code == 200 and r.get_json()["chars"] == 2
    r = c.post("/v1/dom/act", json={**act, "op": "eval"}, headers=H)
    assert r.status_code == 400


def test_act_gives_way_to_the_owner(gw_on):
    c = gw_on.app.test_client()
    c.post("/v1/hands/claim", json={"holder": "muse"}, headers=H)
    act = {"holder": "muse", "tab_id": "T1", "op": "click", "selector": "#go"}
    gw_on.idle["v"] = 1.0
    r = c.post("/v1/dom/act", json=act, headers=H)
    assert r.status_code == 409 and r.get_json()["error_code"] == "owner_active"
    gw_on.idle["v"] = None
    r = c.post("/v1/dom/act", json=act, headers=H)
    assert r.status_code == 503 and r.get_json()["error_code"] == "owner_state_unknown"
    assert not [x for x in gw_on.browser.calls if x["method"].startswith("Input.")]


def test_pause_stops_reads_and_acts(gw_on):
    c = gw_on.app.test_client()
    c.post("/v1/hands/claim", json={"holder": "muse"}, headers=H)
    gw_on.set_paused(True)
    assert c.get("/v1/dom/tabs", headers=H).status_code == 423
    r = c.post("/v1/dom/act", json={"holder": "muse", "tab_id": "T1", "op": "click", "selector": "#go"}, headers=H)
    assert r.status_code == 423 and r.get_json()["error_code"] == "paused"
    gw_on.set_paused(False)
    assert c.get("/v1/dom/tabs", headers=H).status_code == 200


def test_token_still_required(gw_on):
    assert gw_on.app.test_client().get("/v1/dom/tabs").status_code == 401


def test_toggle_off_again_refuses(gw_on):
    gw_on.set_dom_adapter(False)
    assert gw_on.app.test_client().get("/v1/dom/tabs", headers=H).status_code == 403
