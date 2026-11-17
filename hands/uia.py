"""Opt-in Windows UI Automation (UIA) tree reader for the hands.

Partner ruling 2026-11-15 18:57 CST (WP1, was declined): TOGGLE, default
off. Settings key: ``ui_automation_tree``. While the toggle is off the
hands and the gateway refuse every UIA route with
``error_code: "uia_disabled"`` and nothing in this module touches COM.

What it does
  * ``snapshot(...)`` reads the accessibility tree of the FOREGROUND
    window only: name, control type, bounding rect, enabled, focused,
    automation id. Depth-limited and node-capped; names are truncated.
    Every node gets a short id (``"0"``, ``"0.3"``, ``"0.3.1"`` = child
    path) and the snapshot gets a random ``snapshot`` token. The live
    element objects are cached under that token for a short TTL, so an
    agent can then target an element by ``(snapshot, id)``.
  * ``resolve(snapshot, id)`` returns the cached element, or raises
    ``StaleSnapshot`` if the token expired or is unknown.

Provider seam: the hands call ``get_provider()``; tests install a fake
with ``set_provider()``. The Windows provider uses ``comtypes`` (MIT
licence, optional dependency) to reach ``IUIAutomation``. Off Windows,
or without comtypes, ``get_provider()`` returns None and the routes
answer 501 ``uia_unavailable`` (Linux CI imports this file safely).
"""
import secrets
import sys
import threading
import time

MAX_DEPTH_DEFAULT = 6
MAX_DEPTH_LIMIT = 20
MAX_NODES_DEFAULT = 400
MAX_NODES_LIMIT = 2000
NAME_MAX_CHARS = 200
SNAPSHOT_TTL_SECONDS = 120.0
SNAPSHOT_CACHE_MAX = 4

# UIA control type ids (UIAutomationClient.h) -> readable names.
CONTROL_TYPES = {
    50000: "Button", 50001: "Calendar", 50002: "CheckBox", 50003: "ComboBox",
    50004: "Edit", 50005: "Hyperlink", 50006: "Image", 50007: "ListItem",
    50008: "List", 50009: "Menu", 50010: "MenuBar", 50011: "MenuItem",
    50012: "ProgressBar", 50013: "RadioButton", 50014: "ScrollBar",
    50015: "Slider", 50016: "Spinner", 50017: "StatusBar", 50018: "Tab",
    50019: "TabItem", 50020: "Text", 50021: "ToolBar", 50022: "ToolTip",
    50023: "Tree", 50024: "TreeItem", 50025: "Custom", 50026: "Group",
    50027: "Thumb", 50028: "DataGrid", 50029: "DataItem", 50030: "Document",
    50031: "SplitButton", 50032: "Window", 50033: "Pane", 50034: "Header",
    50035: "HeaderItem", 50036: "Table", 50037: "TitleBar", 50038: "Separator",
    50039: "SemanticZoom", 50040: "AppBar",
}


class UIAError(Exception):
    """Provider failure (COM error, no foreground window, ...)."""


class StaleSnapshot(Exception):
    """The (snapshot, id) pair no longer names a cached element."""


# ------------------------------------------------------------ provider seam
#
# A provider exposes:
#   foreground_root()      -> element handle, or None if no foreground window
#   children(el)           -> list of element handles
#   props(el)              -> dict: name, control_type (int), rect
#                             (left, top, right, bottom), enabled, focused,
#                             automation_id
#   set_focus(el)          -> None (raises UIAError on failure)
#   alive_rect(el)         -> fresh rect, or None if the element is gone

_provider = None
_provider_probed = False
_provider_lock = threading.Lock()


def set_provider(provider):
    """Install a provider (tests); None resets to auto-detect."""
    global _provider, _provider_probed
    with _provider_lock:
        _provider = provider
        _provider_probed = provider is not None


def get_provider():
    global _provider, _provider_probed
    with _provider_lock:
        if not _provider_probed:
            _provider_probed = True
            _provider = _windows_provider()
        return _provider


def unavailable_reason():
    if not sys.platform.startswith("win"):
        return "UI Automation needs Windows"
    try:
        import comtypes  # noqa: F401
    except Exception:
        return "UI Automation needs the optional 'comtypes' package (pip install comtypes)"
    return "UI Automation could not be initialised on this PC"


def _windows_provider():
    if not sys.platform.startswith("win"):
        return None
    try:
        return _ComtypesProvider()
    except Exception:
        return None


class _ComtypesProvider:
    """IUIAutomation through comtypes. Windows only; never imported in CI."""

    def __init__(self):
        import ctypes
        import comtypes
        import comtypes.client

        comtypes.CoInitialize()
        self._uia_mod = comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as uiac

        self._uiac = uiac
        self._uia = comtypes.client.CreateObject(
            uiac.CUIAutomation, interface=uiac.IUIAutomation
        )
        self._walker = self._uia.ControlViewWalker
        self._user32 = ctypes.windll.user32
        self._lock = threading.Lock()  # COM calls from one thread at a time

    def _co(self):
        import comtypes

        try:
            comtypes.CoInitialize()  # per waitress worker thread; idempotent
        except OSError:
            pass

    def foreground_root(self):
        self._co()
        hwnd = self._user32.GetForegroundWindow()
        if not hwnd:
            return None
        with self._lock:
            try:
                return self._uia.ElementFromHandle(hwnd)
            except Exception as exc:
                raise UIAError(f"ElementFromHandle failed: {exc}") from exc

    def children(self, el):
        self._co()
        out = []
        with self._lock:
            try:
                child = self._walker.GetFirstChildElement(el)
                while child:
                    out.append(child)
                    child = self._walker.GetNextSiblingElement(child)
            except Exception:
                pass  # a vanishing subtree truncates, never fails the read
        return out

    def props(self, el):
        self._co()
        with self._lock:
            r = el.CurrentBoundingRectangle
            return {
                "name": el.CurrentName or "",
                "control_type": int(el.CurrentControlType),
                "rect": (int(r.left), int(r.top), int(r.right), int(r.bottom)),
                "enabled": bool(el.CurrentIsEnabled),
                "focused": bool(el.CurrentHasKeyboardFocus),
                "automation_id": el.CurrentAutomationId or "",
            }

    def set_focus(self, el):
        self._co()
        with self._lock:
            try:
                el.SetFocus()
            except Exception as exc:
                raise UIAError(f"SetFocus failed: {exc}") from exc

    def alive_rect(self, el):
        self._co()
        with self._lock:
            try:
                r = el.CurrentBoundingRectangle
                return (int(r.left), int(r.top), int(r.right), int(r.bottom))
            except Exception:
                return None


# ------------------------------------------------------------ snapshots

_cache = {}  # token -> {"at": t, "elements": {id: el}}
_cache_lock = threading.Lock()


def _clamp(value, default, low, high):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def _shape(node_id, depth, p):
    left, top, right, bottom = p["rect"]
    name = p.get("name") or ""
    truncated = len(name) > NAME_MAX_CHARS
    ct = p.get("control_type")
    return {
        "id": node_id,
        "depth": depth,
        "name": name[:NAME_MAX_CHARS],
        "name_truncated": truncated,
        "control_type": CONTROL_TYPES.get(ct, str(ct)),
        "rect": {"x": left, "y": top, "w": max(0, right - left), "h": max(0, bottom - top)},
        "enabled": bool(p.get("enabled")),
        "focused": bool(p.get("focused")),
        "automation_id": (p.get("automation_id") or "")[:NAME_MAX_CHARS],
    }


def snapshot(provider, max_depth=None, max_nodes=None, clock=time.monotonic):
    """Breadth-first read of the foreground window's tree. Returns a dict
    ready for JSON; raises UIAError if there is no foreground window."""
    max_depth = _clamp(max_depth, MAX_DEPTH_DEFAULT, 0, MAX_DEPTH_LIMIT)
    max_nodes = _clamp(max_nodes, MAX_NODES_DEFAULT, 1, MAX_NODES_LIMIT)
    root = provider.foreground_root()
    if root is None:
        raise UIAError("no foreground window")
    nodes, elements = [], {}
    truncated = False
    frontier = [("0", 0, root)]
    while frontier:
        node_id, depth, el = frontier.pop(0)
        if len(nodes) >= max_nodes:
            truncated = True
            break
        try:
            p = provider.props(el)
        except Exception:
            continue  # element vanished mid-read
        nodes.append(_shape(node_id, depth, p))
        elements[node_id] = el
        if depth < max_depth:
            for i, child in enumerate(provider.children(el)):
                frontier.append((f"{node_id}.{i}", depth + 1, child))
        elif provider.children(el):
            truncated = True
    token = secrets.token_hex(8)
    now = clock()
    with _cache_lock:
        _expire_locked(now)
        _cache[token] = {"at": now, "elements": elements}
        while len(_cache) > SNAPSHOT_CACHE_MAX:
            oldest = min(_cache, key=lambda k: _cache[k]["at"])
            del _cache[oldest]
    return {
        "ok": True,
        "snapshot": token,
        "ttl_seconds": SNAPSHOT_TTL_SECONDS,
        "max_depth": max_depth,
        "max_nodes": max_nodes,
        "truncated": truncated,
        "count": len(nodes),
        "nodes": nodes,
    }


def _expire_locked(now):
    for k in [k for k, v in _cache.items() if now - v["at"] > SNAPSHOT_TTL_SECONDS]:
        del _cache[k]


def resolve(token, node_id, clock=time.monotonic):
    with _cache_lock:
        _expire_locked(clock())
        entry = _cache.get(token) if isinstance(token, str) else None
        if entry is None:
            raise StaleSnapshot("snapshot expired or unknown; read the tree again")
        el = entry["elements"].get(node_id) if isinstance(node_id, str) else None
    if el is None:
        raise StaleSnapshot(f"no element {node_id!r} in that snapshot")
    return el


def clear():
    with _cache_lock:
        _cache.clear()
