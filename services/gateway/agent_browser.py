r"""Agent browser, invisible (setting ``agent_browser_invisible``; replaces the
former ``nested_desktop`` entry under its real name. Partner ruling
2026-11-15 22:32 / order 2026-11-16).

BROWSER TASKS ONLY - not a full desktop. When the owner turns it on, an agent
may start ITS OWN Chromium-family browser (Edge, else Chrome) that never shows
on the owner's screen:

  * launched with --headless=new: no window on any desktop, no taskbar entry;
  * a dedicated profile under %USERPROFILE%\.HandsRelayV0.1\agent-browser\profile
    (never the owner's profile, cookies or sign-ins);
  * --remote-debugging-address=127.0.0.1 and --remote-debugging-port=0, so the
    browser picks a random free port, read back from DevToolsActivePort;
  * driven only over CDP (Page.navigate, Runtime.evaluate, Input.dispatch*,
    Page.captureScreenshot), never through SendInput / pyautogui, so the
    owner's mouse, keyboard and focus are untouched;
  * one instance at a time; stopped (process killed) when the toggle goes off.

Stdlib only.
"""

import base64
import glob
import http.client
import json
import os
import shutil
import socket
import struct
import subprocess
import threading
import time
from urllib.parse import urlsplit

CDP_HOST = "127.0.0.1"  # never configurable
CONNECT_TIMEOUT = 3.0
CALL_TIMEOUT = 20.0
MAX_FRAME_BYTES = 64 * 1024 * 1024
START_TIMEOUT = 20.0
TEXT_MAX_DEFAULT = 20000
TEXT_MAX_LIMIT = 200000
URL_MAX = 4000
SELECTOR_MAX = 1000
TYPE_MAX = 10000
VIEW_W, VIEW_H = 1280, 800

DATA_DIR = os.environ.get("HANDSRELAY_AGENT_BROWSER_DIR", "").strip() or os.path.join(
    os.path.expanduser("~"), ".HandsRelayV0.1", "agent-browser")

BROWSER_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)


class BrowserError(Exception):
    def __init__(self, code, message, status=502):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def find_browser():
    env = os.environ.get("HANDSRELAY_AGENT_BROWSER_EXE", "").strip()
    if env:
        return env if os.path.isfile(env) else None
    for path in BROWSER_CANDIDATES:
        if os.path.isfile(path):
            return path
    for name in ("msedge", "chrome", "chromium", "chromium-browser", "google-chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def build_argv(exe, profile_dir):
    return [
        exe, "--headless=new",
        "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
        f"--user-data-dir={profile_dir}",
        "--no-first-run", "--no-default-browser-check", "--disable-sync",
        "--disable-extensions", "--disable-background-networking",
        f"--window-size={VIEW_W},{VIEW_H}", "about:blank",
    ]


class WebSocket:
    def __init__(self, url, timeout=CALL_TIMEOUT):
        parts = urlsplit(url)
        self.sock = socket.create_connection((CDP_HOST, parts.port), timeout=CONNECT_TIMEOUT)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        path = parts.path + (("?" + parts.query) if parts.query else "")
        req = (
            f"GET {path or '/'} HTTP/1.1\r\nHost: {CDP_HOST}:{parts.port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode("ascii"))
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise BrowserError("cdp_error", "websocket handshake closed")
            head += chunk
            if len(head) > 65536:
                raise BrowserError("cdp_error", "websocket handshake too large")
        head, self._buf = head.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status + b" ":
            raise BrowserError("cdp_error", f"websocket handshake refused: {status[:80]!r}")
        import hashlib
        expect = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        )
        if expect not in head:
            raise BrowserError("cdp_error", "websocket handshake accept key mismatch")

    def _recv_exact(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self._buf)))
            if not chunk:
                raise BrowserError("cdp_error", "browser closed the connection")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_frame(self, opcode, payload):
        header = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def send_text(self, text):
        self._send_frame(0x1, text.encode("utf-8"))

    def recv_text(self):
        parts = []
        while True:
            b1, b2 = self._recv_exact(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._recv_exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._recv_exact(8))[0]
            if n > MAX_FRAME_BYTES:
                raise BrowserError("cdp_error", "browser frame too large")
            mask = self._recv_exact(4) if b2 & 0x80 else None
            payload = self._recv_exact(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                raise BrowserError("cdp_error", "browser closed the websocket")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            parts.append(payload)
            if fin:
                return b"".join(parts).decode("utf-8")

    def close(self):
        try:
            self._send_frame(0x8, struct.pack(">H", 1000))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class CdpSession:
    """One short-lived websocket to one tab; ``call`` waits for its id."""

    def __init__(self, ws_url):
        try:
            self.ws = WebSocket(ws_url)
        except OSError as exc:
            raise BrowserError("cdp_error", f"could not attach to the tab: {exc}")
        self._next = 0

    def call(self, method, params=None):
        self._next += 1
        mid = self._next
        try:
            self.ws.send_text(json.dumps({"id": mid, "method": method, "params": params or {}}))
            while True:
                msg = json.loads(self.ws.recv_text())
                if msg.get("id") == mid:
                    break  # events (no id) are skipped
        except socket.timeout:
            raise BrowserError("cdp_timeout", f"browser did not answer {method} in time", 504)
        except (OSError, ValueError) as exc:
            raise BrowserError("cdp_error", f"CDP {method} failed: {exc}")
        if "error" in msg:
            err = msg["error"]
            raise BrowserError("cdp_error", f"CDP {method}: {err.get('message', err)}")
        return msg.get("result", {})

    def evaluate(self, expression):
        res = self.call("Runtime.evaluate", {
            "expression": expression, "returnByValue": True, "awaitPromise": False,
        })
        if res.get("exceptionDetails"):
            text = res["exceptionDetails"].get("text", "script error")
            raise BrowserError("page_error", f"page script failed: {text}")
        return (res.get("result") or {}).get("value")

    def close(self):
        self.ws.close()



class AgentBrowser:
    """One headless browser owned by the relay."""

    def __init__(self, data_dir=None, popen=subprocess.Popen, exe_finder=find_browser):
        self.data_dir = data_dir or DATA_DIR
        self._popen, self._find = popen, exe_finder
        self.proc = None
        self.port = None
        self.exe = None
        self.lock = threading.RLock()

    @property
    def profile_dir(self):
        return os.path.join(self.data_dir, "profile")

    @property
    def shots_dir(self):
        return os.path.join(self.data_dir, "screenshots")

    def running(self):
        return self.proc is not None and self.proc.poll() is None and self.port is not None

    def status(self):
        with self.lock:
            return {"running": self.running(), "port": self.port if self.running() else None,
                    "browser": os.path.basename(self.exe) if self.exe else None,
                    "headless": True, "scope": "browser tasks only, not a full desktop",
                    "profile_dir": self.profile_dir}

    def start(self):
        with self.lock:
            if self.running():
                return self.status()
            self.stop()
            exe = self._find()
            if not exe:
                raise BrowserError("no_browser", "no Edge or Chrome found on this PC", 503)
            os.makedirs(self.profile_dir, exist_ok=True)
            port_file = os.path.join(self.profile_dir, "DevToolsActivePort")
            try:
                os.remove(port_file)
            except OSError:
                pass
            flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
            self.proc = self._popen(build_argv(exe, self.profile_dir), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    creationflags=flags)
            self.exe = exe
            deadline = time.monotonic() + START_TIMEOUT
            while time.monotonic() < deadline:
                if self.proc.poll() is not None:
                    self.proc = None
                    raise BrowserError("start_failed", "the browser exited during start", 502)
                try:
                    with open(port_file, "r", encoding="ascii") as fh:
                        first = fh.readline().strip()
                    if first.isdigit():
                        self.port = int(first)
                        return self.status()
                except OSError:
                    pass
                time.sleep(0.1)
            self.stop()
            raise BrowserError("start_failed", "the browser did not report its debugging port", 504)

    def stop(self):
        with self.lock:
            proc, self.proc, self.port = self.proc, None, None
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
            return {"running": False}

    # ---------------------------------------------------------- CDP
    def _page_ws(self):
        if not self.running():
            raise BrowserError("not_running", "the agent browser is not started (POST /v1/agent-browser/start)", 409)
        conn = http.client.HTTPConnection(CDP_HOST, self.port, timeout=CONNECT_TIMEOUT)
        try:
            conn.request("GET", "/json/list")
            tabs = json.loads(conn.getresponse().read().decode("utf-8"))
        except (OSError, ValueError) as exc:
            raise BrowserError("cdp_error", f"could not list pages: {exc}")
        finally:
            conn.close()
        for t in tabs:
            if t.get("type") == "page":
                url = t.get("webSocketDebuggerUrl", "")
                if urlsplit(url).hostname != CDP_HOST:
                    raise BrowserError("cdp_error", "debugger URL is not loopback; refused")
                return url
        raise BrowserError("cdp_error", "the agent browser has no page")

    def _session(self, fn):
        with self.lock:
            s = CdpSession(self._page_ws())
            try:
                return fn(s)
            finally:
                s.close()

    def open(self, url):
        if not isinstance(url, str) or not url or len(url) > URL_MAX:
            raise BrowserError("bad_request", "url must be a non-empty string", 400)
        if urlsplit(url).scheme not in ("http", "https", "about", "data"):
            raise BrowserError("bad_request", "url scheme must be http, https, about or data", 400)

        def go(s):
            s.call("Page.enable")
            res = s.call("Page.navigate", {"url": url})
            if res.get("errorText"):
                raise BrowserError("navigate_failed", res["errorText"])
            deadline = time.monotonic() + CALL_TIMEOUT
            while time.monotonic() < deadline:
                if s.evaluate("document.readyState") == "complete":
                    break
                time.sleep(0.1)
            return {"url": s.evaluate("location.href"), "title": s.evaluate("document.title")}
        return self._session(go)

    def read(self, max_chars=None, html=False):
        try:
            limit = int(max_chars) if max_chars is not None else TEXT_MAX_DEFAULT
        except (TypeError, ValueError):
            limit = TEXT_MAX_DEFAULT
        limit = max(100, min(limit, TEXT_MAX_LIMIT))
        expr = "document.documentElement.outerHTML" if html else "document.body ? document.body.innerText : ''"

        def rd(s):
            body = s.evaluate(expr) or ""
            return {"url": s.evaluate("location.href"), "title": s.evaluate("document.title"),
                    ("html" if html else "text"): body[:limit], "truncated": len(body) > limit}
        return self._session(rd)

    def screenshot(self):
        def shot(s):
            data = s.call("Page.captureScreenshot", {"format": "png"}).get("data", "")
            os.makedirs(self.shots_dir, exist_ok=True)
            path = os.path.join(self.shots_dir, time.strftime("shot-%Y%m%d-%H%M%S") + f"-{int(time.time()*1000)%1000:03d}.png")
            with open(path, "wb") as fh:
                fh.write(base64.b64decode(data))
            return {"png_base64": data, "path": path}
        return self._session(shot)

    def click(self, selector):
        sel = _check_selector(selector)

        def ck(s):
            box = s.evaluate("(()=>{const e=document.querySelector(%s);if(!e)return null;e.scrollIntoView({block:'center'});"
                             "const r=e.getBoundingClientRect();return [r.x+r.width/2,r.y+r.height/2,e.tagName.toLowerCase()]})()" % sel)
            if not box:
                raise BrowserError("not_found", "no element matches the selector", 404)
            x, y, tag = box
            for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
                p = {"type": kind, "x": x, "y": y}
                if kind != "mouseMoved":
                    p.update(button="left", clickCount=1)
                s.call("Input.dispatchMouseEvent", p)
            return {"op": "click", "tag": tag, "x": x, "y": y}
        return self._session(ck)

    def type_text(self, selector, text):
        sel = _check_selector(selector)
        if not isinstance(text, str) or len(text) > TYPE_MAX:
            raise BrowserError("bad_request", f"text must be a string of at most {TYPE_MAX} chars", 400)

        def ty(s):
            tag = s.evaluate("(()=>{const e=document.querySelector(%s);if(!e)return null;e.focus();"
                             "return (e.isContentEditable||/^(input|textarea)$/i.test(e.tagName))?e.tagName.toLowerCase():'!'})()" % sel)
            if tag is None:
                raise BrowserError("not_found", "no element matches the selector", 404)
            if tag == "!":
                raise BrowserError("not_editable", "the element is not editable", 400)
            s.call("Input.insertText", {"text": text})
            return {"op": "type", "tag": tag, "chars": len(text)}
        return self._session(ty)


def _check_selector(selector):
    if not isinstance(selector, str) or not selector or len(selector) > SELECTOR_MAX:
        raise BrowserError("bad_request", f"selector must be a CSS string of 1-{SELECTOR_MAX} chars", 400)
    return json.dumps(selector)  # JSON-encoded into the page script: no injection
