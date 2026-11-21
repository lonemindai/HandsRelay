"""R13 contract deltas for the v4 'toggle off == the old gateway' tests.

Those tests compare against gateways from before the v3 R13 merge. R13
deliberately changed two answers on every gateway, toggles or not:
/v1/info gained fields (claim_sync, release, watcher freshness), and
/v1/act (+ /queued) answer with bounded diagnostics instead of echoing
the hands body. For those paths the comparison is the status code plus
"every old /v1/info key is still there"; every other route stays
byte-for-byte.
"""
import json

R13_CHANGED = ("/v1/info", "/v1/act", "/v1/act/queued")


def _keys(data):
    try:
        value = json.loads(data)
    except (TypeError, ValueError):
        return None
    return set(value) if isinstance(value, dict) else None


def _drop_claim_id(body):
    """R13 gives every claim a random id; the rest of the body is unchanged."""
    try:
        value = json.loads(body)
    except (TypeError, ValueError):
        return body
    if isinstance(value, dict) and isinstance(value.get("claim"), dict):
        value["claim"].pop("id", None)
        return json.dumps(value, sort_keys=True)
    return body


def compare(script_paths, new, old):
    """new/old: lists of (status, body_bytes, headers) in script order."""
    assert len(new) == len(old)
    for path, n, o in zip(script_paths, new, old):
        if path in R13_CHANGED:
            assert n[0] == o[0], (path, n[0], o[0])
            if path == "/v1/info" and o[0] == 200:
                assert _keys(o[1]) <= _keys(n[1]), path
        elif path.startswith("/v1/hands/claim"):
            assert n[0] == o[0], (path, n[0], o[0])
            assert _drop_claim_id(n[1]) == _drop_claim_id(o[1]), path
        else:
            assert n == o, path
