"""Agent browser, invisible (order v4-toggle-nested): fake-CDP tests.

A stdlib fake of the browser's debug endpoint runs on 127.0.0.1 and a fake
Popen writes DevToolsActivePort, so no real browser starts here.
"""
import base64
import hashlib
import importlib.util
import itertools
import json
import os
import socketserver
import struct
import subprocess
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BASE = "925f9be"  # v4-toggles-wave1 head, before the agent browser
_seq = itertools.count()
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()


def _load(path, tag):
    spec = importlib.util.spec_from_file_location(f"{tag}_{next(_seq)}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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
            e = params["expression"]
            table = {"document.readyState": "complete", "location.href": "https://example.test/",
                     "document.title": "Example Title"}
            if e in table:
                v = table[e]
            elif "innerText" in e:
                v = "Hello body " * 10
            elif "outerHTML" in e:
                v = "<html><body>hi</body></html>"
            elif "getBoundingClientRect" in e:
                v = [10, 20, "button"] if '"#go"' in e else None
            elif "isContentEditable" in e:
                v = "input" if '"#user"' in e else ("!" if '"#go"' in e else None)
            else:
                v = None
            return {"result": {"result": {"type": "object", "value": v}}}
        if method == "Page.captureScreenshot":
            return {"result": {"data": PNG}}
        if method in ("Page.enable", "Page.navigate") or method.startswith("Input."):
            return {"result": {}}
        return {"error": {"code": -32601, "message": f"{method} wasn't found"}}


class FakeProc:
    def __init__(self):
        self.alive, self.terminated = True, False

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.alive, self.terminated = False, True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.alive = False


@pytest.fixture
def fake():
    f = FakeBrowser()
    yield f
    f.close()


@pytest.fixture
def ab(tmp_path):
    return _load(ROOT / "services/gateway/agent_browser.py", "ab")


def _make(ab, tmp_path, fake, launched):
    def popen(argv, **kw):
        launched.append((argv, kw))
        prof = [a.split("=", 1)[1] for a in argv if a.startswith("--user-data-dir=")][0]
        with open(os.path.join(prof, "DevToolsActivePort"), "w") as fh:
            fh.write(f"{fake.port}\n/devtools/browser/x\n")
        launched[-1] = (argv, kw, FakeProc())
        return launched[-1][2]
    return ab.AgentBrowser(data_dir=str(tmp_path / "agent-browser"), popen=popen,
                           exe_finder=lambda: "C:/fake/msedge.exe")


def test_launch_is_headless_loopback_random_port_own_profile(ab, tmp_path, fake):
    launched = []
    b = _make(ab, tmp_path, fake, launched)
    st = b.start()
    argv = launched[0][0]
    assert "--headless=new" in argv
    assert "--remote-debugging-address=127.0.0.1" in argv
    assert "--remote-debugging-port=0" in argv
    assert f"--user-data-dir={tmp_path / 'agent-browser' / 'profile'}" in argv
    assert st["running"] and st["port"] == fake.port and "not a full desktop" in st["scope"]
    assert ab.CDP_HOST == "127.0.0.1"
    b.start()
    assert len(launched) == 1  # one instance at a time


def test_never_uses_sendinput_or_pyautogui(ab):
    import ast
    tree = ast.parse((ROOT / "services/gateway/agent_browser.py").read_text())
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                for a in getattr(n, "names", [])} | {getattr(n, "module", None) for n in ast.walk(tree)
                                                    if isinstance(n, ast.ImportFrom)}
    assert not imported & {"pyautogui", "ctypes", "win32api", "pynput"}
    code = "".join(ast.unparse(n) for n in tree.body[1:])  # minus the docstring
    for banned in ("SendInput", "user32", "shell=True"):
        assert banned not in code


def test_open_read_screenshot_click_type(ab, tmp_path, fake):
    b = _make(ab, tmp_path, fake, [])
    b.start()
    assert b.open("https://example.test/") == {"url": "https://example.test/", "title": "Example Title"}
    r = b.read(max_chars=100)
    assert r["title"] == "Example Title" and len(r["text"]) == 100 and r["truncated"]
    assert "<body>" in b.read(html=True)["html"]
    s = b.screenshot()
    assert s["png_base64"] == PNG and open(s["path"], "rb").read().startswith(b"\x89PNG")
    assert b.click("#go")["tag"] == "button"
    kinds = [c["params"]["type"] for c in fake.calls if c["method"] == "Input.dispatchMouseEvent"]
    assert kinds == ["mouseMoved", "mousePressed", "mouseReleased"]
    assert b.type_text("#user", "hi") == {"op": "type", "tag": "input", "chars": 2}
    with pytest.raises(ab.BrowserError) as e:
        b.type_text("#go", "x")
    assert e.value.code == "not_editable"
    with pytest.raises(ab.BrowserError) as e:
        b.click("#missing")
    assert e.value.status == 404


def test_bad_inputs_refused(ab, tmp_path, fake):
    b = _make(ab, tmp_path, fake, [])
    b.start()
    for url in ("", "file:///C:/Windows/win.ini", "javascript:alert(1)", None):
        with pytest.raises(ab.BrowserError):
            b.open(url)
    with pytest.raises(ab.BrowserError):
        b.click("")
    with pytest.raises(ab.BrowserError) as e:
        b.click('"); alert(1); ("')  # JSON-encoded into the page: just not found
    assert e.value.code == "not_found"


def test_no_browser_is_503(ab, tmp_path):
    b = ab.AgentBrowser(data_dir=str(tmp_path), exe_finder=lambda: None)
    with pytest.raises(ab.BrowserError) as e:
        b.start()
    assert e.value.status == 503
    with pytest.raises(ab.BrowserError) as e:
        b.read()
    assert e.value.code == "not_running"


def test_debugger_url_off_loopback_refused(ab, tmp_path):
    f = FakeBrowser(ws_host="10.0.0.9")
    try:
        b = _make(ab, tmp_path, f, [])
        b.start()
        with pytest.raises(ab.BrowserError) as e:
            b.read()
        assert "loopback" in e.value.message
    finally:
        f.close()


# --------------------------------------------------------------- gateway

H = {"X-Relay-Token": "t"}
ROUTES = (("get", "/v1/agent-browser/status"), ("post", "/v1/agent-browser/start"),
          ("post", "/v1/agent-browser/stop"), ("post", "/v1/agent-browser/open"),
          ("post", "/v1/agent-browser/read"), ("post", "/v1/agent-browser/screenshot"),
          ("post", "/v1/agent-browser/click"), ("post", "/v1/agent-browser/type"))


def _gateway(monkeypatch, path=ROOT / "services/gateway/app.py"):
    monkeypatch.setenv("RELAY_GATEWAY_TOKEN", "t")
    monkeypatch.setenv("HANDS_SECRET", "s")
    mod = _load(path, "ab_gw")
    monkeypatch.setattr(mod, "_push_claim_locked", lambda claim, now: None)
    return mod


def test_off_every_route_is_404_like_unknown(monkeypatch):
    gw = _gateway(monkeypatch)
    assert gw.AGENT_BROWSER is False
    c = gw.app.test_client()
    unknown = c.get("/v1/nonexistent", headers=H)
    for method, path in ROUTES:
        r = getattr(c, method)(path, json={"url": "https://x.test"}, headers=H)
        assert r.status_code == 404 and r.get_data() == unknown.get_data(), path
    assert gw._agent_browser is None  # nothing created, nothing launched


def test_off_is_byte_for_byte_the_base_gateway(monkeypatch, tmp_path):
    old = subprocess.run(["git", "-C", str(ROOT), "show", f"{BASE}:services/gateway/app.py"],
                         capture_output=True, text=True)
    if old.returncode != 0:
        pytest.skip("base commit not available in this checkout")
    old_path = tmp_path / "app_old.py"
    old_path.write_text(old.stdout)
    import requests
    import time as _time

    def dead(*a, **k):
        raise requests.RequestException("no hands in tests")

    monkeypatch.setattr(requests, "get", dead)
    monkeypatch.setattr(requests, "post", dead)
    monkeypatch.setattr(requests.Session, "request", dead)
    monkeypatch.setattr(_time, "time", lambda: 1_800_000_000.0)
    script = [("get", "/health", None), ("get", "/v1/info", None), ("get", "/v1/hands/claim", None),
              ("post", "/v1/hands/claim", {"holder": "m", "purpose": "p"}),
              ("post", "/v1/act", {"op": "nope"}), ("post", "/v1/hands/release", {"holder": "m"}),
              ("get", "/v1/nonexistent", None)] + [(m, p, {}) for m, p in ROUTES]

    def run(gw):
        c = gw.app.test_client()
        return [(r.status_code, r.get_data(), sorted((k, v) for k, v in r.headers if k != "Date"))
                for r in (getattr(c, m)(p, json=b, headers=H) for m, p, b in script)]

    assert run(_gateway(monkeypatch)) == run(_gateway(monkeypatch, old_path))
    old_rules = {r.rule for r in _gateway(monkeypatch, old_path).app.url_map.iter_rules()}
    new_rules = {r.rule for r in _gateway(monkeypatch).app.url_map.iter_rules()}
    assert new_rules - old_rules == {p for _, p in ROUTES}


@pytest.fixture
def gw_on(monkeypatch, ab, tmp_path, fake):
    gw = _gateway(monkeypatch)
    gw._agent_browser = _make(ab, tmp_path, fake, [])
    gw.set_agent_browser(True)
    return gw


def test_on_full_flow_without_any_claim(gw_on):
    c = gw_on.app.test_client()
    assert c.post("/v1/agent-browser/start", headers=H).get_json()["running"] is True
    r = c.post("/v1/agent-browser/open", json={"url": "https://example.test/"}, headers=H)
    assert r.status_code == 200 and r.get_json()["title"] == "Example Title"
    assert c.post("/v1/agent-browser/read", json={}, headers=H).get_json()["text"].startswith("Hello")
    assert c.post("/v1/agent-browser/screenshot", headers=H).get_json()["png_base64"] == PNG
    assert c.post("/v1/agent-browser/click", json={"selector": "#go"}, headers=H).status_code == 200
    assert c.post("/v1/agent-browser/click", json={"selector": "#nope"}, headers=H).status_code == 404
    assert c.post("/v1/agent-browser/open", json={"url": "file:///etc"}, headers=H).status_code == 400


def test_pause_refuses_and_token_required(gw_on):
    c = gw_on.app.test_client()
    assert c.get("/v1/agent-browser/status").status_code in (401, 403)
    gw_on.PAUSED = True
    assert c.post("/v1/agent-browser/start", headers=H).status_code == 423


def test_toggle_off_kills_the_browser(gw_on):
    c = gw_on.app.test_client()
    c.post("/v1/agent-browser/start", headers=H)
    proc = gw_on._agent_browser.proc
    gw_on.set_agent_browser(False)
    assert proc.terminated and not gw_on._agent_browser.running()
    assert c.get("/v1/agent-browser/status", headers=H).status_code == 404
