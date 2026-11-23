"""UI Automation tree toggle (ticket 20261115-muse-handsrelay-toggles, uia).

Covers, with a fake UIA provider (no Windows, no COM):
  * the setting: available, default OFF, risk note, applies live;
  * OFF: hands and gateway refuse /uia routes (403 uia_disabled), and
    every pre-existing route answers byte-for-byte what the base commit
    (5715a98) answers;
  * ON: tree shape, depth limit, node cap, name truncation, snapshot ids;
    click/focus by id; stale snapshots; owner yield, focus guard and
    owner pause still apply; no provider -> 501 uia_unavailable;
  * the window app pushes the toggle into both modules live.

The real Windows provider (comtypes -> IUIAutomation) needs a human on
Windows; see the ticket REPORT.

    python -m pytest -q tests/test_uia_tree.py
"""
import importlib.util
import itertools
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASE = "178a719"  # was 5715a98 before the history rewrite (git-filter-repo commit-map)
sys.path.insert(0, str(ROOT))
import relay_settings as rs  # noqa: E402

_seq = itertools.count()

# ----------------------------------------------------------- fakes for hands
_calls = []


def _rec(name):
    def fn(*a, **k):
        _calls.append((name, a, k))
    return fn


fake_pag = types.ModuleType("pyautogui")
fake_pag.FAILSAFE = False
fake_pag.PAUSE = 0
for _n in ("moveTo", "click", "write", "press", "scroll", "hotkey", "mouseDown", "mouseUp"):
    setattr(fake_pag, _n, _rec(_n))
fake_pag.position = lambda: (0, 0)
fake_pag.screenshot = lambda: None
fake_pag.getAllWindows = lambda: []
sys.modules["pyautogui"] = fake_pag
fake_tk = types.ModuleType("tkinter")


class _Dummy:
    def __init__(self, *a, **k):
        pass


for _n in ("Tk", "StringVar", "Label", "Entry", "Button", "Text"):
    setattr(fake_tk, _n, _Dummy)
sys.modules["tkinter"] = fake_tk


def _load_path(path, tag):
    spec = importlib.util.spec_from_file_location(f"{tag}_{next(_seq)}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git_base(rel, tmp_path):
    """The file as it was at the base commit, written beside its siblings
    (hands loads motion.py by path, so copy the folder)."""
    out = tmp_path / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    shown = subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{BASE}:{rel}"], capture_output=True
    )
    if shown.returncode != 0:
        pytest.skip("base commit not available in this checkout")
    data = shown.stdout
    out.write_bytes(data)
    return out


class FakeEl:
    def __init__(self, name, ct=50000, rect=(10, 20, 110, 60), kids=(), enabled=True, focused=False):
        self.name, self.ct, self.rect = name, ct, rect
        self.kids, self.enabled, self.focused = list(kids), enabled, focused
        self.alive = True


class FakeProvider:
    def __init__(self, root):
        self.root = root
        self.focused = []

    def foreground_root(self):
        return self.root

    def children(self, el):
        return list(el.kids)

    def props(self, el):
        return {"name": el.name, "control_type": el.ct, "rect": el.rect,
                "enabled": el.enabled, "focused": el.focused, "automation_id": "aid-" + el.name[:5]}

    def set_focus(self, el):
        self.focused.append(el)

    def alive_rect(self, el):
        return el.rect if el.alive else None


def _tree():
    ok = FakeEl("OK", 50000, (100, 200, 180, 230))
    edit = FakeEl("Search", 50004, (10, 10, 310, 40), focused=True)
    deep = FakeEl("leaf", 50020, (0, 0, 5, 5))
    pane = FakeEl("pane", 50033, (0, 0, 400, 300), kids=[edit, ok, FakeEl("g", 50026, kids=[deep])])
    return FakeEl("Notepad", 50032, (0, 0, 800, 600), kids=[pane]), ok, edit


def _hands(path=None):
    mod = _load_path(path or ROOT / "hands" / "hands_relay_hands.py", "uia_hands")
    mod._probe_lock_state = lambda: (False, 999.0)
    mod._enum_windows = lambda: [
        {"title": "Notepad", "focused": True, "bounds": {"x": 0, "y": 0, "w": 800, "h": 600}}
    ]
    mod._foreground_window_native = lambda: None
    return mod


@pytest.fixture
def hands():
    mod = _hands()
    mod.uia.clear()
    root, ok, edit = _tree()
    prov = FakeProvider(root)
    mod.uia.set_provider(prov)
    mod.fake = types.SimpleNamespace(provider=prov, ok=ok, edit=edit, root=root)
    yield mod
    mod.uia.set_provider(None)


def _auth(mod):
    return {"X-Secret": mod.SHARED_SECRET}


# ------------------------------------------------------------ the setting

def test_setting_is_available_default_off_with_a_risk_note():
    s = rs.BY_KEY["ui_automation_tree"]
    assert s.available and s.default is False and s.kind == "bool" and s.applies == "live"
    assert "window" in s.risk and "click" in s.risk
    assert "ui_automation_tree" in {x["key"] for x in rs.schema()}
    store = rs.SettingsStore("/nonexistent/never-written.json")
    assert store.values["ui_automation_tree"] is False


# ------------------------------------------------------------ OFF

def test_hands_off_refuses(hands, monkeypatch):
    hands.UIA_TREE_ENABLED = False
    c = hands.app.test_client()
    for r in (c.get("/uia/tree", headers=_auth(hands)),
              c.post("/uia/act", json={"snapshot": "x", "id": "0"}, headers=_auth(hands))):
        assert r.status_code == 403 and r.get_json()["error_code"] == "uia_disabled"
    assert hands.fake.provider.focused == [] and not _calls
    assert c.get("/uia/tree").status_code == 401  # still behind the secret


def test_hands_default_is_off(monkeypatch):
    monkeypatch.delenv("HANDS_UIA_TREE", raising=False)
    assert _hands().UIA_TREE_ENABLED is False


def _hands_requests(mod):
    c = mod.app.test_client()
    a = {"X-Secret": "fixed"}
    out = []
    for method, path, body in (
        ("get", "/windows", None),
        ("post", "/click", {"x": 5, "y": 6, "human": False}),
        ("post", "/click", {"x": "bad"}),
        ("post", "/focus", {"title": "nothing-matches"}),
        ("post", "/focus", {"title": "Notepad"}),
        ("get", "/nope", None),
        ("get", "/windows", "noauth"),
    ):
        hdr = {} if body == "noauth" else a
        r = getattr(c, method)(path, json=None if body in (None, "noauth") else body, headers=hdr)
        out.append((path, r.status_code, r.get_data()))
    return out


def test_hands_off_is_byte_for_byte_the_base(tmp_path, monkeypatch):
    monkeypatch.delenv("HANDS_UIA_TREE", raising=False)
    for f in ("hands/hands_relay_hands.py", "hands/motion.py"):
        _git_base(f, tmp_path)
    results = []
    for path in (tmp_path / "hands" / "hands_relay_hands.py", None):
        mod = _hands(path)
        mod.SHARED_SECRET = "fixed"
        mod._activate_window = lambda win: True
        _calls.clear()
        res = _hands_requests(mod)
        results.append((res, list(_calls)))
    assert results[0] == results[1]


def _gw(monkeypatch, path=None):
    monkeypatch.setenv("RELAY_GATEWAY_TOKEN", "t")
    monkeypatch.setenv("HANDS_SECRET", "s")
    monkeypatch.delenv("RELAY_UIA_TREE", raising=False)
    mod = _load_path(path or ROOT / "services" / "gateway" / "app.py", "uia_gw")
    mod._push_claim_locked = lambda claim, now: None
    return mod


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.ok = status < 400

    def json(self):
        return self._body


def _fake_requests(mod, log):
    # Speaks the R13 execution lease like a current hands; older
    # gateways never ask, so the base comparison is unaffected.
    def post(url, json=None, headers=None, timeout=None):
        log.append(("POST", url, json))
        if url.endswith("/execution/lease"):
            return _Resp(200, {"ok": True, "lease": {"version": 1, "boot": "b", "token": "t"}})
        return _Resp(200, {"ok": True, "echo": json})

    def get(url, headers=None, timeout=None):
        log.append(("GET", url))
        if url.endswith("/execution/capabilities"):
            return _Resp(200, {"ok": True, "execution_lease_version": 1, "boot": "b"})
        return _Resp(200, {"ok": True, "windows": []})

    mod.requests.post = post
    mod.requests.get = get


def test_gateway_off_refuses_without_reaching_hands(monkeypatch):
    gw = _gw(monkeypatch)
    assert gw.UIA_TREE_ENABLED is False
    log = []
    real_post, real_get = gw.requests.post, gw.requests.get
    try:
        _fake_requests(gw, log)
        c = gw.app.test_client()
        h = {"X-Relay-Token": "t"}
        for r in (c.get("/v1/uia/tree", headers=h),
                  c.post("/v1/uia/act", json={"snapshot": "x", "id": "0"}, headers=h)):
            assert r.status_code == 403 and r.get_json()["error_code"] == "uia_disabled"
        assert log == []
        assert c.get("/v1/uia/tree").status_code == 401  # token gate first
    finally:
        gw.requests.post, gw.requests.get = real_post, real_get


def test_gateway_off_is_byte_for_byte_the_base(tmp_path, monkeypatch):
    old_path = _git_base("services/gateway/app.py", tmp_path)
    outs = []
    for path in (old_path, None):
        gw = _gw(monkeypatch, path)
        log = []
        real_post, real_get = gw.requests.post, gw.requests.get
        try:
            _fake_requests(gw, log)
            c = gw.app.test_client()
            h = {"X-Relay-Token": "t"}
            res = []
            for method, p, body in (
                ("post", "/v1/act", {"op": "click", "x": 1, "y": 2}),
                ("post", "/v1/act", {"op": "uia_act", "id": "0"}),
                ("post", "/v1/act", {"op": "focus", "title": "x"}),
                ("get", "/v1/windows", None),
                ("get", "/v1/hands/claim", None),
                ("post", "/v1/hands/claim", {"holder": "muse"}),
                ("post", "/v1/act/queued", {"op": "key", "key": "a"}),
            ):
                r = getattr(c, method)(p, json=body, headers=h)
                data = r.get_json() or {}
                for volatile in ("since", "expires_at", "receipt", "id", "submitted_at", "expires_in"):
                    data.pop(volatile, None)
                if isinstance(data.get("claim"), dict):
                    data["claim"] = {k: v for k, v in data["claim"].items()
                                     if k not in ("since", "expires_at", "expires_in", "id")}  # R13 claim id
                if p in ("/v1/act", "/v1/act/queued"):
                    # R13: bounded diagnostics, not the hands echo
                    data = {k: v for k, v in data.items() if k == "ok"}
                res.append((p, r.status_code, sorted(data.items(), key=str)))
            # R13: queued acts negotiate a boot-bound lease first
            log = [entry for entry in log if "/execution/" not in str(entry)]
            log = [(e[0], e[1], {k: v for k, v in e[2].items() if k != "_execution_lease"})
                   if len(e) == 3 and isinstance(e[2], dict) else e for e in log]
            outs.append((res, log))
            gw.stop_background()
        finally:
            gw.requests.post, gw.requests.get = real_post, real_get
    assert outs[0] == outs[1]


# ------------------------------------------------------------ ON: tree

def _tree_json(hands, **q):
    hands.UIA_TREE_ENABLED = True
    c = hands.app.test_client()
    r = c.get("/uia/tree", query_string=q, headers=_auth(hands))
    return r.status_code, r.get_json()


def test_tree_shape_and_ids(hands):
    status, d = _tree_json(hands)
    assert status == 200 and d["ok"] and d["snapshot"]
    ids = {n["id"]: n for n in d["nodes"]}
    assert ids["0"]["control_type"] == "Window" and ids["0"]["name"] == "Notepad"
    ok = ids["0.0.1"]
    assert ok["name"] == "OK" and ok["control_type"] == "Button"
    assert ok["rect"] == {"x": 100, "y": 200, "w": 80, "h": 30}
    assert ids["0.0.0"]["focused"] is True and ok["enabled"] is True
    assert d["truncated"] is False and d["count"] == 6
    assert d["window"]["title"] == "Notepad"


def test_depth_limit_and_node_cap_and_name_truncation(hands):
    _, d = _tree_json(hands, max_depth=1)
    assert {n["id"] for n in d["nodes"]} == {"0", "0.0"} and d["truncated"] is True
    _, d = _tree_json(hands, max_nodes=3)
    assert d["count"] == 3 and d["truncated"] is True
    _, d = _tree_json(hands, max_depth="junk", max_nodes=10**9)
    assert d["max_depth"] == hands.uia.MAX_DEPTH_DEFAULT and d["max_nodes"] == hands.uia.MAX_NODES_LIMIT
    hands.fake.root.name = "x" * 5000
    _, d = _tree_json(hands)
    assert len(d["nodes"][0]["name"]) == hands.uia.NAME_MAX_CHARS and d["nodes"][0]["name_truncated"]


def test_no_foreground_window_is_409(hands):
    hands.fake.provider.root = None
    status, d = _tree_json(hands)
    assert status == 409 and d["error_code"] == "uia_error"


def test_no_provider_is_501(hands):
    hands.UIA_TREE_ENABLED = True
    hands.uia.set_provider(None)
    hands.uia._provider_probed = True  # auto-detect found nothing (Linux)
    r = hands.app.test_client().get("/uia/tree", headers=_auth(hands))
    assert r.status_code == 501 and r.get_json()["error_code"] == "uia_unavailable"


# ------------------------------------------------------------ ON: act

def _act(hands, **body):
    hands.UIA_TREE_ENABLED = True
    r = hands.app.test_client().post("/uia/act", json=body, headers=_auth(hands))
    return r.status_code, r.get_json()


def test_click_by_id_hits_the_element_centre(hands):
    _, d = _tree_json(hands)
    _calls.clear()
    status, out = _act(hands, snapshot=d["snapshot"], id="0.0.1", action="click", human=False)
    assert status == 200 and out["ok"] and out["action"] == "click"
    assert ("click", (), {"x": 140, "y": 215, "button": "left"}) in _calls


def test_human_click_lands_inside_the_element(hands):
    _, d = _tree_json(hands)
    _calls.clear()
    status, out = _act(hands, snapshot=d["snapshot"], id="0.0.1", human=True)
    assert status == 200 and 100 <= out["x"] <= 180 and 200 <= out["y"] <= 230


def test_focus_by_id(hands):
    _, d = _tree_json(hands)
    status, out = _act(hands, snapshot=d["snapshot"], id="0.0.0", action="focus")
    assert status == 200 and hands.fake.provider.focused == [hands.fake.edit]


def test_stale_and_bad_requests(hands):
    _, d = _tree_json(hands)
    assert _act(hands, snapshot="nope", id="0")[1]["error_code"] == "uia_stale"
    assert _act(hands, snapshot=d["snapshot"], id="9.9")[1]["error_code"] == "uia_stale"
    assert _act(hands, snapshot=d["snapshot"], id="0", action="type")[0] == 400
    hands.fake.ok.alive = False
    status, out = _act(hands, snapshot=d["snapshot"], id="0.0.1")
    assert status == 409 and out["error_code"] == "uia_stale"
    hands.fake.edit.rect = (5, 5, 5, 5)
    assert _act(hands, snapshot=d["snapshot"], id="0.0.0")[1]["error_code"] == "uia_offscreen"


def test_snapshot_ttl_expires(hands):
    t = [1000.0]
    d = hands.uia.snapshot(hands.fake.provider, clock=lambda: t[0])
    assert hands.uia.resolve(d["snapshot"], "0", clock=lambda: t[0]) is hands.fake.root
    t[0] += hands.uia.SNAPSHOT_TTL_SECONDS + 1
    with pytest.raises(hands.uia.StaleSnapshot):
        hands.uia.resolve(d["snapshot"], "0", clock=lambda: t[0])


def test_owner_active_yields_before_acting(hands):
    _, d = _tree_json(hands)
    hands._probe_lock_state = lambda: (False, 0.5)
    _calls.clear()
    status, out = _act(hands, snapshot=d["snapshot"], id="0.0.1", human=False)
    assert status == 409 and "yielded" in out["error"]
    assert not _calls and hands.fake.provider.focused == []
    hands._probe_lock_state = lambda: (True, 999.0)
    assert _act(hands, snapshot=d["snapshot"], id="0.0.0", action="focus")[1]["error_code"] == "locked"
    assert hands.fake.provider.focused == []


def test_focus_guard_applies(hands):
    _, d = _tree_json(hands)
    status, out = _act(hands, snapshot=d["snapshot"], id="0.0.1", human=False,
                       expect_focus={"title": "Some Other Window"})
    assert status == 409 and out["error_code"] == "foreground_changed"


def test_tree_read_does_not_yield(hands):
    """Reading is not input: the owner being active does not block it."""
    hands._probe_lock_state = lambda: (False, 0.1)
    assert _tree_json(hands)[0] == 200


# ------------------------------------------------------------ gateway ON

def test_gateway_on_proxies_and_respects_pause(monkeypatch):
    gw = _gw(monkeypatch)
    gw.UIA_TREE_ENABLED = True
    log = []
    real_post, real_get = gw.requests.post, gw.requests.get
    try:
        _fake_requests(gw, log)
        c = gw.app.test_client()
        h = {"X-Relay-Token": "t"}
        assert c.get("/v1/uia/tree?max_depth=3&max_nodes=50&evil=1", headers=h).status_code == 200
        assert log[-1] == ("GET", "http://host.docker.internal:5000/uia/tree?max_depth=3&max_nodes=50")
        r = c.post("/v1/uia/act", json={"snapshot": "s", "id": "0.1", "action": "click", "x": 9}, headers=h)
        assert r.status_code == 200
        assert log[-1] == ("POST", "http://host.docker.internal:5000/uia/act",
                           {"snapshot": "s", "id": "0.1", "action": "click"})
        gw.set_paused(True)
        n = len(log)
        for r in (c.get("/v1/uia/tree", headers=h), c.post("/v1/uia/act", json={}, headers=h)):
            assert r.status_code == 423 and r.get_json()["error_code"] == "paused"
        assert len(log) == n
        gw.set_paused(False)
    finally:
        gw.requests.post, gw.requests.get = real_post, real_get


# ------------------------------------------------------------ app wiring

class _Root:
    def after(self, ms, fn, *args):
        pass


def test_app_pushes_the_toggle_live(monkeypatch, tmp_path):
    appmod = _load_path(ROOT / "hands_relay_v4_app.py", "uia_app")
    monkeypatch.setattr(appmod, "_autostart_enabled", lambda: False)
    monkeypatch.setattr(appmod.relay_settings, "settings_path", lambda: str(tmp_path / "s.json"))
    monkeypatch.delenv("RELAY_UIA_TREE", raising=False)
    monkeypatch.delenv("HANDS_UIA_TREE", raising=False)
    relay = appmod.RelayApp(_Root())
    gw_mod = types.SimpleNamespace(UIA_TREE_ENABLED=False)
    hands_mod = types.SimpleNamespace(UIA_TREE_ENABLED=False)
    relay._gateway_module, relay._hands_module = gw_mod, hands_mod
    relay._apply_settings({"ui_automation_tree": True})
    assert gw_mod.UIA_TREE_ENABLED and hands_mod.UIA_TREE_ENABLED
    relay._apply_settings({"ui_automation_tree": False})
    assert not gw_mod.UIA_TREE_ENABLED and not hands_mod.UIA_TREE_ENABLED
    # untouched store: nothing pushed at Start
    assert "ui_automation_tree" not in rs.changed_from_defaults(rs.defaults())
