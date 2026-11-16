"""Toggle: input-injecting smoke script (WP0, partner ruling 2026-11-15)."""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import relay_settings as rs  # noqa: E402

spec = importlib.util.spec_from_file_location("smoke_inject", ROOT / "tools" / "smoke_inject.py")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class _Resp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data

    def json(self):
        return self._data


class FakeHttp:
    def __init__(self, info=None, fail_path=None):
        self.calls = []
        self.info = info or {"ok": True, "hands_reachable": True, "paused": False}
        self.fail_path = fail_path

    def _answer(self, method, url, json=None, headers=None, timeout=None):
        path = url.split("5010", 1)[1]
        self.calls.append((method, path, json, headers))
        if self.fail_path and path == self.fail_path and (json or {}).get("op") != "move":
            return _Resp(409, {"ok": False, "error": "focus guard"})
        if path == "/v1/info":
            return _Resp(200, self.info)
        return _Resp(200, {"ok": True})

    def get(self, url, **kw):
        return self._answer("get", url, **kw)

    def post(self, url, **kw):
        return self._answer("post", url, **kw)


def _settings(tmp_path, on):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"version": 1, "settings": {"input_injecting_smoke_script": on}}))
    return str(path)


def test_registered_off_and_available():
    s = rs.BY_KEY["input_injecting_smoke_script"]
    assert s.default is False and s.available and s.risk


def test_refuses_when_toggle_off_and_sends_nothing(tmp_path):
    http, out = FakeHttp(), []
    for path in (_settings(tmp_path, False), str(tmp_path / "missing.json")):
        rc = smoke.main(["--settings", path, "--token", "t"], http=http, out=out.append)
        assert rc == 2
    assert http.calls == [] and "REFUSED" in out[-1]


def test_pointer_only_run(tmp_path):
    http, out = FakeHttp(), []
    rc = smoke.main(["--settings", _settings(tmp_path, True), "--token", "t", "--point", "10,20"],
                    http=http, out=out.append)
    assert rc == 0
    posted = [(p, b) for m, p, b, _ in http.calls if m == "post"]
    assert posted[0][0] == "/v1/hands/claim"
    assert [b for p, b in posted if p == "/v1/act"] == [{"op": "move", "x": 10, "y": 20}, {"op": "move", "x": 50, "y": 20}]
    assert posted[-1] == ("/v1/hands/release", {"holder": "smoke-test"})
    assert all(h == {"X-Relay-Token": "t"} for _, _, _, h in http.calls)
    assert not any(b and b.get("op") == "type" for _, b in posted)


def test_type_into_pins_focus(tmp_path):
    http = FakeHttp()
    rc = smoke.main(["--settings", _settings(tmp_path, True), "--token", "t", "--type-into", "Notepad"],
                    http=http, out=lambda s: None)
    assert rc == 0
    typed = [b for _, p, b, _ in http.calls if b and b.get("op") == "type"]
    assert typed == [{"op": "type", "text": smoke.DEFAULT_TEXT, "expect_focus": {"title": "Notepad"}}]


def test_failure_still_releases(tmp_path):
    http = FakeHttp(fail_path="/v1/act")
    rc = smoke.main(["--settings", _settings(tmp_path, True), "--token", "t", "--type-into", "Notepad"],
                    http=http, out=lambda s: None)
    assert rc == 1
    assert http.calls[-1][1] == "/v1/hands/release"


def test_paused_or_unreachable_stops_before_claim(tmp_path):
    for info in ({"ok": True, "hands_reachable": True, "paused": True},
                 {"ok": True, "hands_reachable": False}):
        http = FakeHttp(info=info)
        assert smoke.main(["--settings", _settings(tmp_path, True), "--token", "t"],
                          http=http, out=lambda s: None) == 1
        assert not any(p == "/v1/hands/claim" for _, p, _, _ in http.calls)
