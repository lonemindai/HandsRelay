"""Browser DOM adapter (partner ruling 2026-11-15: BUILD it, as a toggle).

Opt-in, default OFF (setting ``browser_dom_adapter``). Attaches to a
Chromium-family browser (Chrome, Edge, Brave, ...) that the USER has
started with remote debugging, e.g.

    msedge.exe --remote-debugging-port=9222 --user-data-dir=%TEMP%\\relay-edge

and talks the Chrome DevTools Protocol (CDP) to it over a websocket.

Hard rules, enforced here and tested:
  * loopback only: the CDP host is fixed to 127.0.0.1; a /json/list
    entry whose websocket URL points anywhere else is refused (no
    redirect off-box);
  * this module never launches, relaunches or kills a browser; with no
    debuggable browser it answers "no_browser" with how to start one;
  * snapshots are size-capped and REDACT password fields always and
    every other form-control value by default.

Stdlib only: http.client for /json/list, and a minimal RFC 6455 client
(text frames, masking, fragmentation, ping/pong, close) for CDP.
"""

import base64
import http.client
import json
import os
import socket
import struct
import threading
from urllib.parse import urlsplit

CDP_HOST = "127.0.0.1"  # never configurable: loopback only
CDP_PORT_DEFAULT = 9222
CONNECT_TIMEOUT = 3.0
CALL_TIMEOUT = 10.0
SNAPSHOT_MAX_CHARS_DEFAULT = 20000
SNAPSHOT_MAX_CHARS_LIMIT = 200000
SNAPSHOT_MAX_NODES = 5000
SELECTOR_MAX_CHARS = 1000
TYPE_MAX_CHARS = 10000
MAX_FRAME_BYTES = 64 * 1024 * 1024
_LOOPBACK_NAMES = {"127.0.0.1", "localhost", "[::1]", "::1"}

START_HINT = (
    "No debuggable browser on 127.0.0.1:{port}. The relay never starts your browser. "
    "To allow this, close it and start it yourself with remote debugging, e.g. "
    "msedge.exe --remote-debugging-port={port} --user-data-dir=%TEMP%\\relay-browser "
    "(a separate profile keeps your everyday signed-in sessions out of reach)."
)


class DomError(Exception):
    def __init__(self, code, message, status=502):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def cdp_port():
    raw = os.environ.get("RELAY_DOM_CDP_PORT", "").strip()
    try:
        port = int(raw) if raw else CDP_PORT_DEFAULT
    except ValueError:
        port = CDP_PORT_DEFAULT
    return port if 1 <= port <= 65535 else CDP_PORT_DEFAULT


# ---------------------------------------------------------------------------
# HTTP discovery
# ---------------------------------------------------------------------------
def _http_json(path, port):
    conn = http.client.HTTPConnection(CDP_HOST, port, timeout=CONNECT_TIMEOUT)
    try:
        conn.request("GET", path, headers={"Host": f"{CDP_HOST}:{port}"})
        resp = conn.getresponse()
        body = resp.read(8 * 1024 * 1024)
    except (OSError, http.client.HTTPException):
        raise DomError("no_browser", START_HINT.format(port=port), 503)
    finally:
        conn.close()
    if resp.status != 200:
        raise DomError("cdp_error", f"browser debug endpoint answered HTTP {resp.status}")
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise DomError("cdp_error", "browser debug endpoint did not return JSON")


def _ws_url_ok(ws_url, port):
    try:
        parts = urlsplit(ws_url)
    except ValueError:
        return False
    return (
        parts.scheme == "ws"
        and (parts.hostname or "") in _LOOPBACK_NAMES
        and parts.port == port
    )


def list_tabs(port=None):
    """Page targets only: [{"id", "title", "url"}]."""
    port = port or cdp_port()
    data = _http_json("/json/list", port)
    if not isinstance(data, list):
        raise DomError("cdp_error", "unexpected /json/list shape")
    tabs = []
    for entry in data:
        if not isinstance(entry, dict) or entry.get("type") != "page":
            continue
        tabs.append({
            "id": str(entry.get("id", "")),
            "title": str(entry.get("title", ""))[:300],
            "url": str(entry.get("url", ""))[:2000],
        })
    return tabs


def _tab_ws_url(tab_id, port):
    if not isinstance(tab_id, str) or not tab_id or len(tab_id) > 200:
        raise DomError("bad_request", "tab_id (string from /v1/dom/tabs) is required", 400)
    data = _http_json("/json/list", port)
    for entry in data if isinstance(data, list) else ():
        if isinstance(entry, dict) and entry.get("id") == tab_id and entry.get("type") == "page":
            ws_url = str(entry.get("webSocketDebuggerUrl") or "")
            if not ws_url:
                raise DomError("tab_busy", "tab has no debugger URL (DevTools already attached?)", 409)
            if not _ws_url_ok(ws_url, port):
                raise DomError("not_loopback", "refused: the tab's debugger URL is not on 127.0.0.1", 502)
            return ws_url
    raise DomError("no_tab", f"no page tab with id {tab_id!r}", 404)


# ---------------------------------------------------------------------------
# Minimal websocket client (RFC 6455)
# ---------------------------------------------------------------------------
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
                raise DomError("cdp_error", "websocket handshake closed")
            head += chunk
            if len(head) > 65536:
                raise DomError("cdp_error", "websocket handshake too large")
        head, self._buf = head.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status + b" ":
            raise DomError("cdp_error", f"websocket handshake refused: {status[:80]!r}")
        import hashlib
        expect = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        )
        if expect not in head:
            raise DomError("cdp_error", "websocket handshake accept key mismatch")

    def _recv_exact(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self._buf)))
            if not chunk:
                raise DomError("cdp_error", "browser closed the connection")
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
                raise DomError("cdp_error", "browser frame too large")
            mask = self._recv_exact(4) if b2 & 0x80 else None
            payload = self._recv_exact(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                raise DomError("cdp_error", "browser closed the websocket")
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
            raise DomError("cdp_error", f"could not attach to the tab: {exc}")
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
            raise DomError("cdp_timeout", f"browser did not answer {method} in time", 504)
        except (OSError, ValueError) as exc:
            raise DomError("cdp_error", f"CDP {method} failed: {exc}")
        if "error" in msg:
            err = msg["error"]
            raise DomError("cdp_error", f"CDP {method}: {err.get('message', err)}")
        return msg.get("result", {})

    def evaluate(self, expression):
        res = self.call("Runtime.evaluate", {
            "expression": expression, "returnByValue": True, "awaitPromise": False,
        })
        if res.get("exceptionDetails"):
            text = res["exceptionDetails"].get("text", "script error")
            raise DomError("page_error", f"page script failed: {text}")
        return (res.get("result") or {}).get("value")

    def close(self):
        self.ws.close()


_attach_lock = threading.Lock()  # one CDP conversation at a time


def _with_tab(tab_id, fn, port=None):
    port = port or cdp_port()
    with _attach_lock:
        session = CdpSession(_tab_ws_url(tab_id, port))
        try:
            return fn(session)
        finally:
            session.close()


# ---------------------------------------------------------------------------
# Page scripts (run in the page via Runtime.evaluate; inputs JSON-encoded)
# ---------------------------------------------------------------------------
_SNAPSHOT_JS = r"""
(function (opts) {
  var out = [], count = 0, truncated = false;
  var SKIP = {SCRIPT:1, STYLE:1, NOSCRIPT:1, TEMPLATE:1, SVG:1, HEAD:1, META:1, LINK:1};
  function sel(el) {
    if (el.id) return '#' + CSS.escape(el.id);
    var parts = [];
    while (el && el.nodeType === 1 && parts.length < 8) {
      var p = el.tagName.toLowerCase();
      if (el.id) { parts.unshift('#' + CSS.escape(el.id)); break; }
      var par = el.parentElement;
      if (par) {
        var same = Array.prototype.filter.call(par.children, function (c) { return c.tagName === el.tagName; });
        if (same.length > 1) p += ':nth-of-type(' + (same.indexOf(el) + 1) + ')';
      }
      parts.unshift(p); el = par;
    }
    return parts.join(' > ');
  }
  function visible(el) {
    var r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }
  function walk(el, depth) {
    if (count >= opts.maxNodes) { truncated = true; return; }
    if (!el || el.nodeType !== 1 || SKIP[el.tagName]) return;
    var tag = el.tagName.toLowerCase();
    var role = el.getAttribute('role') || '';
    var name = el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('alt') || '';
    var ownText = '';
    for (var i = 0; i < el.childNodes.length; i++) {
      var n = el.childNodes[i];
      if (n.nodeType === 3) ownText += n.nodeValue;
    }
    ownText = ownText.replace(/\s+/g, ' ').trim().slice(0, 200);
    var interactive = /^(a|button|input|select|textarea|option|summary|label)$/.test(tag) ||
      role || el.hasAttribute('onclick') || el.isContentEditable;
    if (interactive || ownText || name) {
      var node = {d: depth, tag: tag, sel: sel(el)};
      if (role) node.role = role;
      if (name) node.name = name.slice(0, 200);
      if (ownText) node.text = ownText;
      if (tag === 'a' && el.href) node.href = String(el.href).slice(0, 500);
      if (tag === 'input' || tag === 'textarea' || tag === 'select') {
        var type = (el.getAttribute('type') || '').toLowerCase();
        node.type = type || tag;
        if (el.placeholder) node.placeholder = el.placeholder.slice(0, 200);
        var auto = (el.getAttribute('autocomplete') || '').toLowerCase();
        var secret = type === 'password' || /password|cc-|one-time-code/.test(auto);
        var hasValue = tag === 'select' ? el.selectedIndex >= 0 : !!el.value;
        if (type === 'checkbox' || type === 'radio') node.checked = !!el.checked;
        else if (secret) { if (hasValue) node.value = '[REDACTED:password]'; }
        else if (hasValue) node.value = opts.values ? String(el.value).slice(0, 200) : '[REDACTED]';
      }
      if (!visible(el)) node.hidden = true;
      out.push(node); count++;
    }
    for (var c = el.firstElementChild; c; c = c.nextElementSibling) walk(c, depth + 1);
  }
  walk(document.body || document.documentElement, 0);
  return {url: location.href, title: document.title, nodes: out, truncated: truncated};
})(%s)
"""

_LOCATE_JS = r"""
(function (q) {
  var el = document.querySelector(q.selector);
  if (!el) return {found: false};
  el.scrollIntoView({block: 'center', inline: 'center'});
  var r = el.getBoundingClientRect();
  var tag = el.tagName.toLowerCase();
  if (q.focus) el.focus();
  return {found: true, tag: tag, x: r.left + r.width / 2, y: r.top + r.height / 2,
          w: r.width, h: r.height,
          editable: /^(input|textarea)$/.test(tag) || el.isContentEditable,
          disabled: !!el.disabled};
})(%s)
"""


def _clamp_chars(value):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return SNAPSHOT_MAX_CHARS_DEFAULT
    return max(1000, min(SNAPSHOT_MAX_CHARS_LIMIT, n))


def snapshot(tab_id, max_chars=None, include_values=False, port=None):
    """Compact DOM snapshot, size-capped. Password values are ALWAYS
    redacted; other control values unless include_values is true."""
    limit = _clamp_chars(max_chars) if max_chars is not None else SNAPSHOT_MAX_CHARS_DEFAULT
    opts = json.dumps({"values": bool(include_values), "maxNodes": SNAPSHOT_MAX_NODES})
    data = _with_tab(tab_id, lambda s: s.evaluate(_SNAPSHOT_JS % opts), port)
    if not isinstance(data, dict):
        raise DomError("page_error", "snapshot script returned nothing")
    nodes = data.get("nodes") if isinstance(data.get("nodes"), list) else []
    redact_secrets(nodes)  # belt and braces: never trust the page script alone
    if not include_values:
        for n in nodes:
            if isinstance(n, dict) and "value" in n and n["value"] != "[REDACTED:password]":
                n["value"] = "[REDACTED]"
    truncated = bool(data.get("truncated"))
    kept, used = [], 0
    for node in nodes:
        size = len(json.dumps(node, ensure_ascii=False)) + 1
        if used + size > limit:
            truncated = True
            break
        kept.append(node)
        used += size
    return {
        "url": str(data.get("url", ""))[:2000],
        "title": str(data.get("title", ""))[:300],
        "nodes": kept,
        "node_count": len(kept),
        "truncated": truncated,
        "max_chars": limit,
        "values_redacted": not include_values,
    }


def redact_secrets(nodes):
    for n in nodes:
        if isinstance(n, dict) and str(n.get("type", "")).lower() == "password" and "value" in n:
            n["value"] = "[REDACTED:password]"


def check_selector(selector):
    if not isinstance(selector, str) or not selector.strip():
        raise DomError("bad_request", "selector (CSS) is required", 400)
    if len(selector) > SELECTOR_MAX_CHARS:
        raise DomError("bad_request", f"selector longer than {SELECTOR_MAX_CHARS} chars", 400)
    return selector


def _locate(session, selector, focus=False):
    found = session.evaluate(_LOCATE_JS % json.dumps({"selector": selector, "focus": focus}))
    if not isinstance(found, dict) or not found.get("found"):
        raise DomError("no_element", f"no element matches {selector[:120]!r}", 404)
    if found.get("disabled"):
        raise DomError("element_disabled", "the element is disabled", 409)
    if not found.get("w") or not found.get("h"):
        raise DomError("element_hidden", "the element has no visible box", 409)
    return found


def click(tab_id, selector, port=None):
    """Real mouse press/release at the element's centre (CDP Input)."""
    check_selector(selector)

    def run(s):
        el = _locate(s, selector)
        x, y = float(el["x"]), float(el["y"])
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            params = {"type": kind, "x": x, "y": y}
            if kind != "mouseMoved":
                params.update({"button": "left", "clickCount": 1})
            s.call("Input.dispatchMouseEvent", params)
        return {"tag": el["tag"], "x": round(x), "y": round(y)}

    return _with_tab(tab_id, run, port)


def type_text(tab_id, selector, text, port=None):
    """Focus the element and insert text (CDP Input.insertText)."""
    check_selector(selector)
    if not isinstance(text, str):
        raise DomError("bad_request", "text (string) is required", 400)
    if len(text) > TYPE_MAX_CHARS:
        raise DomError("bad_request", f"text longer than {TYPE_MAX_CHARS} chars", 400)

    def run(s):
        el = _locate(s, selector, focus=True)
        if not el.get("editable"):
            raise DomError("not_editable", f"<{el['tag']}> is not a text field", 409)
        s.call("Input.insertText", {"text": text})
        return {"tag": el["tag"], "chars": len(text)}

    return _with_tab(tab_id, run, port)
