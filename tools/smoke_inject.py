#!/usr/bin/env python3
"""HandsRelay end-to-end smoke test that REALLY injects input (WP0).

Partner ruling 2026-11-15 18:57: a toggle, off by default. This script
refuses to run unless Settings -> "Allow the self-test that moves the
mouse and types" (input_injecting_smoke_script) is on in
%USERPROFILE%\\.HandsRelayV0.1\\settings.json.

RISK: it moves the pointer and, with --type-into, types into the window
whose title you name. Run it with the PC idle (the hands give way to your
input) and a harmless target such as an empty Notepad.

Steps (it stops at the first failure and always releases its claim):
  1. GET  /health and /v1/info   (gateway up, hands reachable, not paused)
  2. POST /v1/hands/claim        (holder "smoke-test", 120 s)
  3. POST /v1/act move x2        (to --point and 40 px right of it)
  4. with --type-into TITLE:
     POST /v1/act focus {title}, then type --text with
     expect_focus {title} so nothing lands in another window
  5. POST /v1/hands/release

    python tools/smoke_inject.py                      # pointer moves only
    python tools/smoke_inject.py --type-into Notepad  # plus a typed marker

Token: --token, else env RELAY_GATEWAY_TOKEN, else the app's
%USERPROFILE%\\.HandsRelayV0.1\\gateway-token.txt. Exit 0 = pass,
1 = a step failed, 2 = refused (toggle off / bad setup).
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import relay_settings  # noqa: E402

TOGGLE = "input_injecting_smoke_script"
HOLDER = "smoke-test"
DEFAULT_TEXT = "HandsRelay smoke test"


def toggle_enabled(settings_path=None):
    store = relay_settings.SettingsStore(settings_path)
    store.load()
    return bool(store.get(TOGGLE)), store.warnings


def read_token(explicit=None):
    if explicit:
        return explicit
    if os.environ.get("RELAY_GATEWAY_TOKEN"):
        return os.environ["RELAY_GATEWAY_TOKEN"]
    path = os.path.join(os.path.expanduser("~"), ".HandsRelayV0.1", "gateway-token.txt")
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


class Smoke:
    def __init__(self, url, token, http, out=print):
        self.url = url.rstrip("/")
        self.headers = {"X-Relay-Token": token}
        self.http = http
        self.out = out

    def _call(self, method, path, body=None):
        fn = getattr(self.http, method)
        kwargs = {"headers": self.headers, "timeout": 30}
        if body is not None:
            kwargs["json"] = body
        r = fn(self.url + path, **kwargs)
        try:
            data = r.json()
        except ValueError:
            data = {}
        return r.status_code, data

    def step(self, name, method, path, body=None):
        status, data = self._call(method, path, body)
        ok = 200 <= status < 300 and data.get("ok", True) is not False
        self.out(f"{'PASS' if ok else 'FAIL'}  {name}  ({status})" + ("" if ok else f"  {data.get('error', '')}"))
        return ok, data

    def run(self, point=(200, 200), type_into=None, text=DEFAULT_TEXT):
        ok, _ = self.step("gateway health", "get", "/health")
        if not ok:
            return 1
        ok, info = self.step("gateway info", "get", "/v1/info")
        if not ok:
            return 1
        if not info.get("hands_reachable"):
            self.out("FAIL  hands not reachable from the gateway")
            return 1
        if info.get("paused"):
            self.out("FAIL  the relay is paused by the owner; resume it first")
            return 1
        ok, _ = self.step("claim the hands", "post", "/v1/hands/claim",
                          {"holder": HOLDER, "purpose": "input-injecting smoke test", "ttl_seconds": 120})
        if not ok:
            return 1
        try:
            x, y = point
            for px in (x, x + 40):
                ok, _ = self.step(f"move pointer to {px},{y}", "post", "/v1/act", {"op": "move", "x": px, "y": y})
                if not ok:
                    return 1
            if type_into:
                ok, _ = self.step(f"focus '{type_into}'", "post", "/v1/act", {"op": "focus", "title": type_into})
                if not ok:
                    return 1
                ok, _ = self.step(f"type {len(text)} chars into '{type_into}'", "post", "/v1/act",
                                  {"op": "type", "text": text, "expect_focus": {"title": type_into}})
                if not ok:
                    return 1
            return 0
        finally:
            self.step("release the hands", "post", "/v1/hands/release", {"holder": HOLDER})


def main(argv=None, http=None, out=print):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--url", default="http://127.0.0.1:5010")
    p.add_argument("--token")
    p.add_argument("--settings", help="settings.json path (default: the app's)")
    p.add_argument("--point", default="200,200", help="x,y for the pointer moves")
    p.add_argument("--type-into", help="window title substring to focus and type into")
    p.add_argument("--text", default=DEFAULT_TEXT)
    args = p.parse_args(argv)
    enabled, warnings = toggle_enabled(args.settings)
    for w in warnings:
        out(f"WARN  {w}")
    if not enabled:
        out("REFUSED  the input-injecting smoke test is off. Turn on Settings -> "
            "'Allow the self-test that moves the mouse and types' to run it.")
        return 2
    token = read_token(args.token)
    if not token:
        out("REFUSED  no gateway token (use --token or RELAY_GATEWAY_TOKEN)")
        return 2
    try:
        x, y = (int(v) for v in args.point.split(","))
    except ValueError:
        out("REFUSED  --point must be x,y")
        return 2
    if http is None:
        import requests as http
    out("RISK  this test moves the pointer" + (f" and types into '{args.type_into}'" if args.type_into else ""))
    return Smoke(args.url, token, http, out).run((x, y), args.type_into, args.text)


if __name__ == "__main__":
    sys.exit(main())
