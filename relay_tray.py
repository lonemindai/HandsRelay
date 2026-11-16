"""HandsRelay v4 tray icon (tray + settings ticket, 2026-11-15).

Two halves:
  * tray_state(view, yield_idle_s): PURE. The RelayApp.view() snapshot
    (the same one the dashboard renders) -> variant, owner flag and the
    tooltip text. Unit-tested headless.
  * TrayIcon: the Windows notification-area icon, plain ctypes
    (Shell_NotifyIconW) so the relay gains no new dependency. Runs its
    own hidden window and message loop on a daemon thread. Not testable
    headless; Windows-only, a no-op elsewhere.

State sources (nothing new is invented): view["started"],
view["paused"], view["model"]["services"], view["model"]["claim"]
(the gateway's _claim_snapshot), view["model"]["mode"]/["watchers"],
and view["model"]["pc"]["owner_idle_seconds"] (the hands' yield probe,
as reported in /v1/info pc_state).
"""

import os
import sys
import tempfile
import threading

TOOLTIP_MAX = 127  # NOTIFYICONDATAW.szTip is 128 WCHARs including the NUL

# variant -> (tint RGB, badge RGB or None)
VARIANTS = {
    "stopped": ((110, 116, 130), None),
    "starting": ((150, 160, 180), (150, 160, 180)),
    "fault": ((235, 92, 108), (255, 92, 108)),
    "paused": ((255, 181, 71), (255, 181, 71)),
    "claimed": ((80, 160, 255), (80, 160, 255)),
    "watching": ((61, 220, 151), (160, 120, 255)),
    "free": ((61, 220, 151), None),
}
OWNER_BADGE = (255, 255, 255)


def _clip(text, limit=TOOLTIP_MAX):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def tray_state(view, yield_idle_s=5):
    """view: RelayApp.view() dict (or None). Returns
    {"variant", "owner_active", "tooltip", "paused", "running"}."""
    view = view if isinstance(view, dict) else {}
    model = view.get("model") if isinstance(view.get("model"), dict) else None
    started = bool(view.get("started"))
    paused = bool(view.get("paused"))
    owner_active = False
    holder = None
    if model:
        pc = model.get("pc") if isinstance(model.get("pc"), dict) else {}
        idle = pc.get("owner_idle_seconds")
        if isinstance(idle, (int, float)) and not isinstance(idle, bool):
            owner_active = idle < yield_idle_s
        claim = model.get("claim")
        if isinstance(claim, dict) and claim.get("holder"):
            holder = str(claim["holder"])
    if not started:
        variant, line = "stopped", "stopped"
        owner_active = False
    elif model is None:
        variant, line = "starting", "starting"
    else:
        services = model.get("services") if isinstance(model.get("services"), dict) else {}
        down = [n for n in ("hands", "gateway", "parser") if services.get(n) != "ok"]
        if paused:
            variant, line = "paused", "PAUSED by you - no agent can act"
        elif "gateway" in down or "hands" in down:
            variant, line = "fault", "running, " + " and ".join(d for d in down if d != "parser") + " down"
        elif holder:
            variant, line = "claimed", f"claimed by {holder}"
        elif model.get("mode") == "watcher":
            variant, line = "watching", "free (agent watching)"
        else:
            variant, line = "free", "running, free"
        if variant != "fault" and "parser" in down:
            line += "; parser down"
    tip = f"HandsRelay: {line}"
    if owner_active:
        tip += " · you are active, agent waits"
    return {
        "variant": variant,
        "owner_active": owner_active,
        "tooltip": _clip(tip),
        "paused": paused,
        "running": started,
    }


def icon_key(state):
    return state["variant"] + ("-owner" if state["owner_active"] else "")


def render_icon(base_ico, variant, owner_active, out_dir):
    """Write a tinted/badged copy of relay.ico; returns its path. Pillow
    is already an app dependency."""
    from PIL import Image, ImageDraw, ImageOps

    tint, badge = VARIANTS[variant]
    path = os.path.join(out_dir, f"tray-{variant}{'-owner' if owner_active else ''}.ico")
    if os.path.isfile(path):
        return path
    src = Image.open(base_ico)
    try:
        src.size = max(src.info.get("sizes", {src.size}))
    except Exception:
        pass
    img = src.convert("RGBA").resize((64, 64), Image.LANCZOS)
    alpha = img.getchannel("A")
    gray = ImageOps.grayscale(img)
    if variant == "stopped":
        colored = ImageOps.colorize(gray, (40, 42, 48), tint)
    else:
        colored = ImageOps.colorize(gray, (10, 12, 18), tint)
    out = colored.convert("RGBA")
    out.putalpha(alpha)
    draw = ImageDraw.Draw(out)
    if badge:
        draw.ellipse((38, 38, 63, 63), fill=badge + (255,), outline=(11, 13, 18, 255), width=3)
    if owner_active:
        draw.ellipse((0, 0, 22, 22), fill=OWNER_BADGE + (255,), outline=(11, 13, 18, 255), width=3)
    out.save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64)])
    return path


# ---------------------------------------------------------------- Windows
MENU_OPEN, MENU_SETTINGS, MENU_PAUSE, MENU_QUIT = 1001, 1002, 1003, 1004


class TrayIcon:
    """Windows tray icon. callbacks: on_open, on_settings,
    on_pause(paused_now_wanted: bool), on_quit. update(state) is safe from
    any thread."""

    def __init__(self, base_ico, on_open, on_settings, on_pause, on_quit, log=None):
        self.base_ico = base_ico
        self.on_open, self.on_settings = on_open, on_settings
        self.on_pause, self.on_quit = on_pause, on_quit
        self.log = log or (lambda msg: None)
        self._state = tray_state(None)
        self._hwnd = None
        self._icons = {}
        self._dir = tempfile.mkdtemp(prefix="handsrelay-tray-")
        self._ready = threading.Event()

    @staticmethod
    def supported():
        return sys.platform == "win32"

    def start(self):
        if not self.supported():
            return False
        threading.Thread(target=self._run, name="tray", daemon=True).start()
        return self._ready.wait(5)

    def update(self, state):
        self._state = state
        if self._hwnd:
            self._u.PostMessageW(self._hwnd, self.WM_REFRESH, 0, 0)

    def stop(self):
        if self._hwnd:
            self._u.PostMessageW(self._hwnd, 0x0010, 0, 0)  # WM_CLOSE

    # -- the rest runs on the tray thread --------------------------------
    def _run(self):
        import ctypes
        from ctypes import wintypes as wt

        u = self._u = ctypes.WinDLL("user32", use_last_error=True)
        sh = ctypes.WinDLL("shell32", use_last_error=True)
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
        WM_APP = 0x8000
        self.WM_TRAY, self.WM_REFRESH = WM_APP + 1, WM_APP + 2
        WM_DESTROY, WM_CLOSE, WM_COMMAND = 0x0002, 0x0010, 0x0111
        WM_LBUTTONUP, WM_RBUTTONUP, WM_CONTEXTMENU = 0x0202, 0x0205, 0x007B
        NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
        NIF_MESSAGE, NIF_ICON, NIF_TIP = 1, 2, 4

        class GUID(ctypes.Structure):
            _fields_ = [("a", wt.DWORD), ("b", wt.WORD), ("c", wt.WORD), ("d", ctypes.c_ubyte * 8)]

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT),
                ("uFlags", wt.UINT), ("uCallbackMessage", wt.UINT), ("hIcon", wt.HICON),
                ("szTip", wt.WCHAR * 128), ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD),
                ("szInfo", wt.WCHAR * 256), ("uVersion", wt.UINT),
                ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
                ("guidItem", GUID), ("hBalloonIcon", wt.HICON),
            ]

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR),
            ]

        u.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        u.DefWindowProcW.restype = LRESULT
        u.CreateWindowExW.restype = wt.HWND
        u.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND,
                                      wt.HMENU, wt.HINSTANCE, wt.LPVOID]
        u.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        u.LoadImageW.restype = wt.HANDLE
        u.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ctypes.c_int, ctypes.c_int, wt.UINT]
        u.CreatePopupMenu.restype = wt.HMENU
        u.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR]
        u.TrackPopupMenu.argtypes = [wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                     wt.HWND, wt.LPVOID]
        u.DestroyMenu.argtypes = [wt.HMENU]
        u.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
        u.RegisterWindowMessageW.argtypes = [wt.LPCWSTR]
        u.RegisterWindowMessageW.restype = wt.UINT
        sh.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
        k.GetModuleHandleW.restype = wt.HMODULE
        k.GetModuleHandleW.argtypes = [wt.LPCWSTR]

        wm_taskbar_created = u.RegisterWindowMessageW("TaskbarCreated")
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.uID = 1
        nid.uCallbackMessage = self.WM_TRAY

        def load_icon(state):
            key = icon_key(state)
            if key not in self._icons:
                try:
                    path = render_icon(self.base_ico, state["variant"], state["owner_active"], self._dir)
                except Exception as exc:
                    self.log(f"Tray: icon variant {key} failed ({exc}); using relay.ico.")
                    path = self.base_ico
                self._icons[key] = u.LoadImageW(None, path, 1, 0, 0, 0x0010 | 0x0040)  # IMAGE_ICON; LOADFROMFILE|DEFAULTSIZE
            return self._icons[key]

        def apply(msg):
            st = self._state
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.hIcon = load_icon(st)
            nid.szTip = st["tooltip"][:TOOLTIP_MAX]
            sh.Shell_NotifyIconW(msg, ctypes.byref(nid))

        def menu():
            st = self._state
            h = u.CreatePopupMenu()
            u.AppendMenuW(h, 0, MENU_OPEN, "Open dashboard")
            u.AppendMenuW(h, 0, MENU_SETTINGS, "Settings")
            u.AppendMenuW(h, 0x0800, 0, None)  # MF_SEPARATOR
            pause_flags = 0 if st["running"] else 0x0001  # MF_GRAYED until Start
            u.AppendMenuW(h, pause_flags, MENU_PAUSE,
                          "Resume relay" if st["paused"] else "Pause relay (release claim, stop acts)")
            u.AppendMenuW(h, 0x0800, 0, None)
            u.AppendMenuW(h, 0, MENU_QUIT, "Quit (stop the relay)")
            pt = wt.POINT()
            u.GetCursorPos(ctypes.byref(pt))
            u.SetForegroundWindow(self._hwnd)
            cmd = u.TrackPopupMenu(h, 0x0100 | 0x0020, pt.x, pt.y, 0, self._hwnd, None)  # RETURNCMD|RIGHTBUTTON
            u.PostMessageW(self._hwnd, 0, 0, 0)  # documented fix: menu closes on click-away
            u.DestroyMenu(h)
            return cmd

        def dispatch(cmd):
            try:
                if cmd == MENU_OPEN:
                    self.on_open()
                elif cmd == MENU_SETTINGS:
                    self.on_settings()
                elif cmd == MENU_PAUSE:
                    self.on_pause(not self._state["paused"])
                elif cmd == MENU_QUIT:
                    self.on_quit()
            except Exception as exc:
                self.log(f"Tray: menu action failed ({exc}).")

        def wndproc(hwnd, msg, wparam, lparam):
            if msg == self.WM_TRAY:
                event = lparam & 0xFFFF
                if event == WM_LBUTTONUP:
                    dispatch(MENU_OPEN)
                elif event in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    dispatch(menu())
                return 0
            if msg == self.WM_REFRESH:
                apply(NIM_MODIFY)
                return 0
            if msg == wm_taskbar_created:  # Explorer restarted: re-add
                apply(NIM_ADD)
                return 0
            if msg == WM_CLOSE:
                sh.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
                u.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                u.PostQuitMessage(0)
                return 0
            return u.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc = WNDPROC(wndproc)  # keep a reference alive
        hinst = k.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.lpszClassName = "HandsRelayV4Tray"
        u.RegisterClassW(ctypes.byref(wc))
        self._hwnd = u.CreateWindowExW(0, wc.lpszClassName, "HandsRelay tray", 0,
                                       0, 0, 0, 0, None, None, hinst, None)
        nid.hWnd = self._hwnd
        apply(NIM_ADD)
        self._ready.set()
        msg = wt.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))
