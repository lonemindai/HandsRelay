"""HandsRelay v4 — gateway service.

The agent-facing API. Owns authentication (per-deployment token, constant-time
compare, failed-guess lockout) and routes control requests to the Windows
hands process and parse requests to the parser service.

The hands link: the gateway holds the hands base URL (HANDS_URL) and the
hands' current session secret (HANDS_SECRET — the packaged hands app
generates a fresh secret every launch and shows it in its window; copy it
into the gateway's environment). Agent calls are translated:

  GET  /v1/screen            -> GET  {HANDS_URL}/screenshot  (PNG bytes back)
  GET  /v1/screen/hash       -> screenshot hash (see screen_hash below)
  GET  /v1/monitors          -> GET  {HANDS_URL}/monitors
  GET  /v1/windows           -> GET  {HANDS_URL}/windows
  GET  /v1/clipboard         -> GET  {HANDS_URL}/clipboard/get
  POST /v1/act {"op": ...}   -> POST {HANDS_URL}/<op>        (move/click/
                                                              type/key/scroll/
                                                              hotkey/break/
                                                              drag/focus;
                                                              clipboard_set ->
                                                              /clipboard/set)
"""

import base64
import contextvars
import math
import binascii
import hashlib
import hmac
import importlib.util
import json
import os
import queue as _queue_mod
import subprocess
import threading
import time
import uuid
from collections import OrderedDict
from io import BytesIO

import requests
from flask import Flask, Response, abort, jsonify, request

RELEASE = "v4.0"  # v4 on the v3 R14 lineage; this build's release identity, reported on /v1/info

# Stripped (review E9): cmd's `set NAME=value && ...` keeps the trailing
# space, which made the token and secret silently never match.
TOKEN = os.environ.get("RELAY_GATEWAY_TOKEN", "").strip()
HANDS_URL = os.environ.get("HANDS_URL", "http://host.docker.internal:5000").strip().rstrip("/")
HANDS_SECRET = os.environ.get("HANDS_SECRET", "").strip()
PARSER_URL = os.environ.get("PARSER_URL", "http://parser:5011").strip().rstrip("/")

HANDS_OPS = {
    "move", "click", "type", "key", "scroll",
    # Window, clipboard and input operations:
    "hotkey", "break", "drag", "focus", "clipboard_set",
}
# Op -> hands path, for ops whose hands route is not "/<op>".
HANDS_OP_PATHS = {"clipboard_set": "clipboard/set"}
HANDS_TIMEOUT = 30

# Screen hashing (wishlist #14): the bottom strip of the frame is the
# taskbar — clock, tray icons, notification churn — and must not change
# the hash. 80px matches the watcher convention the live walkthrough
# loop already uses on the client side.
DEFAULT_MASK_BOTTOM = 80
# Stable mode samples two frames this far apart and keeps only the
# pixels that agree, so a blinking caret or spinner phase cannot flip
# the hash (wishlist #14 addendum, 2026-10-19).
STABLE_SAMPLE_GAP = 0.65

MAX_FAILS = 10
FAIL_WINDOW = 600
LOCKOUT_SECONDS = 300

_fail_lock = threading.Lock()

_probe_lock = threading.Lock()
_probe_cache = {"hands": None, "parser": None, "pc_state": None, "at": 0.0}

# ---------------------------------------------------------------------------
# Hands claim coordination (wishlist #15).
#
# Advisory only: sessions read the claim before driving the desktop, but
# /v1/act is deliberately NOT gated on it — the claim coordinates polite
# agents, it is not an access control (the gateway token is that).
# Exception (wishlist claim-yield, 2026-11-16): a caller that NAMES itself
# ("holder" in the act body or X-Hands-Holder) is refused with an explicit
# claim_held 409 when someone else holds the claim; unnamed callers keep
# the advisory behaviour but any yield-409 carries the holder's details.
# GET /v1/hands/claim[/status] is read-only and reports age/expiry/stale;
# a stale claim (unrefreshed for CLAIM_TTL_DEFAULT s) may be force-released.
# State is in-memory and lock-guarded, with LAZY expiry: an expired claim
# is treated as absent the next time anything looks, via _current_claim.
# ---------------------------------------------------------------------------
CLAIM_TTL_DEFAULT = 900
CLAIM_TTL_MIN = 30
CLAIM_TTL_MAX = 3600
RESERVATION_GRACE = 30  # seconds the queue head alone may claim after a free-up
# Review L2: the queue is bounded and self-cleaning. A queued holder keeps
# its slot by re-POSTing {"queue": true} (which refreshes the entry);
# entries not refreshed within QUEUE_ENTRY_TTL are dropped, and a head
# whose reservation lapses unused forfeits its slot.
CLAIM_QUEUE_MAX = 32
QUEUE_ENTRY_TTL = 3600
HOLDER_MAX_CHARS = 128
PURPOSE_MAX_CHARS = 500

# Owner pause (tray "Pause relay", 2026-11-15): set only in-process by the
# packaged app via set_paused(). While paused the claim is released, no
# new claim is granted and no act is forwarded to the hands; /v1/info
# reports "paused" so a driver can tell this from an outage.
PAUSED = False

# Opt-in UI Automation tree (settings key ui_automation_tree; partner
# ruling 2026-11-15: TOGGLE, default OFF). Set in-process by the packaged
# app, or RELAY_UIA_TREE=1. While off, /v1/uia/* answer 403
# error_code "uia_disabled" and never reach the hands; every other route
# is unchanged.
UIA_TREE_ENABLED = os.environ.get("RELAY_UIA_TREE", "").strip() == "1"

_claim_lock = threading.RLock()
_claim = None  # {"holder", "purpose", "since", "expires_at"} or None
_claim_queue = []  # FIFO of {"holder", "purpose", "enqueued_at", "refreshed_at"}
_reserved_for = None  # queue head holder with an exclusive claim window
_reserved_until = 0.0
# Last grant/refresh time of the live claim, kept OUT of the claim dict so
# every existing response stays byte-identical (staleness, claim-yield).
_claim_refreshed_at = None


# R11: the hands' resting-hand float runs only while a claim is held, and
# only the gateway knows claims — so every grant/refresh/release is pushed
# to the hands (/resting/claim) with the remaining lease and a sequence
# number (pushes run on a thread; the hands drops out-of-order ones). This
# is a notification only: lease duration and renewal are unchanged.
_claim_push_seq = 0
_claim_push_session = requests.Session()


_sync_latest = None
_sync_running = False
_sync_health = {"ok": True, "error": None, "epoch": None, "seq": None, "attempts": 0}
SYNC_RETRY_MAX_S = 0.5


def _post_claim_to_hands(payload):
    try:
        body = dict(payload)
        if "epoch" in body and "challenge" not in body:
            challenge = _claim_push_session.get(
                f"{HANDS_URL}/resting/challenge", headers=_hands_headers(), timeout=3
            )
            issued = challenge.json()
            body["challenge"] = issued["challenge"]
            body["boot"] = issued["boot"]
            payload["challenge"] = body["challenge"]
            payload["boot"] = body["boot"]
        return _claim_push_session.post(
            f"{HANDS_URL}/resting/claim", json=body, headers=_hands_headers(), timeout=3
        )
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return False  # retry the latest snapshot; do not drop it


def _ack_ok(response, payload):
    """None means a test replaced the sender. False means try again."""
    if response is None:
        return True
    if response is False:
        return False
    try:
        body = response.json()
    except Exception:
        return False
    if not isinstance(body, dict) or body.get("ok") is not True or body.get("applied") is not True:
        return False
    if "epoch" not in payload:
        return True
    return (
        body.get("boot") == payload.get("boot")
        and body.get("epoch") == payload.get("epoch")
        and body.get("seq") == payload.get("seq")
        and body.get("claim_id") == payload.get("claim_id")
    )


def _sync_loop():
    """One worker. A newer snapshot replaces one that has not been sent."""
    global _sync_running, _sync_latest, _claim_push_seq
    delay = 0.0
    try:
        while True:
            if delay:
                time.sleep(delay)
            with _claim_lock:
                payload = _sync_latest
                _sync_latest = None
            if payload is None:
                return
            response = _post_claim_to_hands(payload)
            if _ack_ok(response, payload):
                _sync_health.update({
                    "ok": True, "error": None, "attempts": 0,
                    "epoch": payload.get("epoch"), "seq": payload.get("seq"),
                })
                delay = 0.0
                if _sync_latest is None:
                    return
                continue
            _sync_health.update({
                "ok": False,
                "error": "unreachable" if response is False else "ack rejected",
                "attempts": int(_sync_health.get("attempts") or 0) + 1,
                "epoch": payload.get("epoch"),
                "seq": payload.get("seq"),
            })
            delay = SYNC_RETRY_MAX_S if delay >= SYNC_RETRY_MAX_S else max(0.05, (delay or 0.025) * 2)
            with _claim_lock:
                if _sync_latest is None:
                    _claim_push_seq += 1
                    retried = dict(payload)
                    retried["seq"] = _claim_push_seq
                    retried.pop("challenge", None)
                    retried.pop("boot", None)
                    _sync_latest = retried
    finally:
        _sync_running = False
        if _sync_latest is not None:
            _ensure_sync_worker()


def _ensure_sync_worker():
    global _sync_running
    if _sync_running:
        return
    _sync_running = True
    threading.Thread(target=_sync_loop, args=(), daemon=True).start()


def _push_claim_locked(claim, now):
    """Remember the latest claim snapshot and wake the one sync worker.

    Call with _claim_lock held. A bound epoch store adds pairing, epoch,
    and the stable claim id. The challenge is fetched when the worker
    sends, not when the snapshot is taken.
    """
    global _claim_push_seq, _sync_latest
    _claim_push_seq += 1
    payload = {
        "held": claim is not None,
        "expires_in": max(0.0, float(claim["expires_at"]) - now) if claim else 0.0,
        "seq": _claim_push_seq,
    }
    authority = _claim_authority
    if authority is not None and authority.serving:
        payload["epoch"] = authority.epoch
        payload["pairing"] = authority.pairing
        payload["claim_id"] = None if claim is None else claim.get("id")
    _sync_latest = payload
    _ensure_sync_worker()


def reconcile_claim_snapshot():
    """Startup: an absent claim is an explicit release, not silence."""
    now = time.time()
    with _claim_lock:
        _push_claim_locked(_current_claim_locked(now), now)


# ---------------------------------------------------------------------------
# R11 watchers (owner, 2026-10-20). A watcher is an agent in eyes-only mode:
# it polls the screen and never takes the claim, so without this it was
# invisible. POST /v1/watch (or ?watcher=<name> on /v1/screen,
# /v1/screen/hash, /v1/screen/wait) marks it watching; it lapses after its
# timeout without a refresh. Timeout: 30 s default, agent-settable up to
# 60 s per watcher (timeout_seconds) or gateway-wide (/v1/watch/config).
# Independent of the claim lease.
# ---------------------------------------------------------------------------
WATCHER_TIMEOUT_DEFAULT = 30
WATCHER_TIMEOUT_MIN = 30
WATCHER_TIMEOUT_MAX = 60

_watch_lock = threading.Lock()
_watchers = {}  # holder -> {"holder", "since", "last_seen", "timeout_seconds"}
WATCHERS_MAX = 64  # review L6: distinct watcher names cannot grow the table without bound


def _initial_watcher_timeout():
    return _clamp_number(
        os.environ.get("WATCHER_TIMEOUT_SECONDS", WATCHER_TIMEOUT_DEFAULT),
        WATCHER_TIMEOUT_DEFAULT,
        WATCHER_TIMEOUT_MIN,
        WATCHER_TIMEOUT_MAX,
    )


_watcher_timeout = None  # set lazily from _initial_watcher_timeout()


def _watcher_timeout_setting():
    global _watcher_timeout
    with _watch_lock:
        if _watcher_timeout is None:
            _watcher_timeout = _initial_watcher_timeout()
        return _watcher_timeout


# Item 32 (partner ruling 2026-11-15 19:02): an "I'm pausing" signal.
# POST /v1/watch/pause {holder, reason?, timeout_seconds?} marks the
# watcher paused: it stays listed (state "paused") under its own longer
# pause lease instead of lapsing after the 30-60 s poll timeout; the next
# touch (POST /v1/watch or ?watcher=) resumes it. A watcher that is
# released or times out is "stopped" and stays visible as such for
# STOPPED_KEEP_SECONDS so the dashboard can say so honestly. Clients that
# never pause see exactly the old behaviour plus a "state" field.
#
# v4-complete fold: the R13 observation model (B17/B18) rides on the same
# entries. Contact (last_seen) never advances observation; a capture
# attempt is recorded by _record_watcher_fetch. The R13 heartbeat flag
# POST /v1/watch {paused: bool} is kept as a separate, sticky flag; either
# kind of pause shows as state/observation "paused".
WATCHER_PAUSE_DEFAULT = 900
WATCHER_PAUSE_MIN = 30
WATCHER_PAUSE_MAX = 3600
STOPPED_KEEP_SECONDS = 300
_stopped_watchers = OrderedDict()  # holder -> {"holder","state","how","stopped_at"}


def _mark_stopped_locked(holder, how, now):
    _stopped_watchers.pop(holder, None)
    _stopped_watchers[holder] = {"holder": holder, "state": "stopped", "how": how, "stopped_at": now}
    while len(_stopped_watchers) > WATCHERS_MAX:
        _stopped_watchers.popitem(last=False)


def _watcher_deadline(entry):
    if entry.get("state") == "paused":
        return entry["paused_at"] + entry["pause_timeout_seconds"]
    return entry["last_seen"] + entry["timeout_seconds"]


def _watcher_stale_setting():
    """Explicit stale threshold S. Acceptance fixtures use 10 seconds.

    This is not the contact timeout. A heartbeat can refresh contact
    without making an old observation fresh.
    """
    raw = os.environ.get("WATCHER_STALE_SECONDS", "10")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 10.0
    if not math.isfinite(value) or value <= 0:
        return 10.0
    return value


def _blank_watcher(holder, now, timeout_seconds):
    return {
        "holder": holder,
        "since": now,
        "timeout_seconds": timeout_seconds,
        "last_seen": now,
        "last_success_at": None,
        "last_fetch_ok": None,
        "last_attempt_at": None,
        "last_published_at": None,
        "process_heartbeat_at": None,
        "consecutive_failures": 0,
        "last_error": None,
        "observation_generation": 0,
        "content_hash": None,
        "content_changed_at": None,
        "paused": False,
        "state": "watching",
    }


def _watcher_entry_locked(holder, now):
    """The live entry for holder, created fresh when absent or lapsed.
    Caller holds _watch_lock."""
    # Review L6: expire lapsed watchers on WRITE too, not only when
    # someone reads /v1/info or /v1/watch, and cap distinct names.
    _expire_watchers_locked(now)
    if holder not in _watchers and len(_watchers) >= WATCHERS_MAX:
        oldest = min(_watchers.values(), key=lambda e: e["last_seen"])
        del _watchers[oldest["holder"]]
    entry = _watchers.get(holder)
    if entry is None:
        entry = _blank_watcher(holder, now, _watcher_timeout)
        _watchers[holder] = entry
    _stopped_watchers.pop(holder, None)
    return entry


def _touch_watcher(holder, timeout_seconds=None, now=None, heartbeat=False, paused=None):
    """Refresh contact. Never advances a validated observation."""
    holder = str(holder or "").strip()[:80]
    if not holder:
        return None
    now = time.time() if now is None else now
    default = _watcher_timeout_setting()
    with _watch_lock:
        entry = _watcher_entry_locked(holder, now)
        if timeout_seconds is not None:
            entry["timeout_seconds"] = _clamp_number(
                timeout_seconds, default, WATCHER_TIMEOUT_MIN, WATCHER_TIMEOUT_MAX
            )
        # Item 32: any touch resumes a paused watcher.
        entry["state"] = "watching"
        for key in ("paused_at", "pause_timeout_seconds", "pause_reason"):
            entry.pop(key, None)
        entry["last_seen"] = now
        if heartbeat:
            entry["process_heartbeat_at"] = now
        if paused is not None:
            entry["paused"] = bool(paused)
        return _public_watcher(entry, now)


def _record_watcher_fetch(holder, ok, error=None, digest=None, now=None):
    """Record one capture attempt. Failure does not move last_success_at."""
    holder = str(holder or "").strip()[:80]
    if not holder:
        return None
    now = time.time() if now is None else now
    _watcher_timeout_setting()
    with _watch_lock:
        entry = _watcher_entry_locked(holder, now)
        # A capture is a touch (?watcher= resumes an item-32 pause).
        entry["state"] = "watching"
        for key in ("paused_at", "pause_timeout_seconds", "pause_reason"):
            entry.pop(key, None)
        entry["last_seen"] = now
        entry["last_attempt_at"] = now
        entry["last_fetch_ok"] = bool(ok)
        if ok:
            entry["last_success_at"] = now
            entry["consecutive_failures"] = 0
            entry["last_error"] = None
            entry["observation_generation"] = int(entry.get("observation_generation") or 0) + 1
            if digest is not None and digest != entry.get("content_hash"):
                entry["content_hash"] = digest
                entry["content_changed_at"] = now
        else:
            entry["consecutive_failures"] = int(entry.get("consecutive_failures") or 0) + 1
            entry["last_error"] = error or "fetch_failed"
        return _public_watcher(entry, now)


def _watcher_success_age(entry, now):
    success = entry.get("last_success_at")
    if isinstance(success, bool) or not isinstance(success, (int, float)):
        return None
    if not math.isfinite(float(success)):
        return None
    return now - float(success)


def _public_watcher(entry, now):
    """Copy plus the label a client can show. Contact is not observation."""
    out = dict(entry)
    age = _watcher_success_age(entry, now)
    stale = age is not None and age >= _watcher_stale_setting()
    if entry.get("paused") or entry.get("state") == "paused":
        label = "paused"
    elif entry.get("last_fetch_ok") is False:
        label = "error"
    elif age is None:
        label = "unknown"
    elif stale:
        label = "stale"
    else:
        label = "fresh"
    out["state"] = "paused" if label == "paused" else "watching"
    out["stale"] = stale
    out["observation"] = label
    return out


def _pause_watcher(holder, reason=None, timeout_seconds=None, now=None):
    """Item 32: the watcher declares it is pausing. Creates the entry if
    the watcher was not known (a pause is also a sign of life)."""
    holder = str(holder or "").strip()[:80]
    if not holder:
        return None
    now = time.time() if now is None else now
    _touch_watcher(holder, now=now)
    with _watch_lock:
        entry = _watchers.get(holder)
        if entry is None:  # evicted between the two locks; vanishingly rare
            return None
        entry["state"] = "paused"
        entry["paused_at"] = now
        entry["pause_timeout_seconds"] = _clamp_number(
            WATCHER_PAUSE_DEFAULT if timeout_seconds is None else timeout_seconds,
            WATCHER_PAUSE_DEFAULT, WATCHER_PAUSE_MIN, WATCHER_PAUSE_MAX,
        )
        entry["pause_reason"] = str(reason).strip()[:120] if reason else ""
        return _public_watcher(entry, now)


def _release_watcher(holder, now=None):
    now = time.time() if now is None else now
    with _watch_lock:
        removed = _watchers.pop(holder, None) is not None
        if removed:
            _mark_stopped_locked(holder, "released", now)
    return removed


def _expire_watchers_locked(now):
    for holder in [h for h, e in _watchers.items() if now >= _watcher_deadline(e)]:
        del _watchers[holder]
        _mark_stopped_locked(holder, "timed_out", now)
    for holder in [h for h, e in _stopped_watchers.items() if now >= e["stopped_at"] + STOPPED_KEEP_SECONDS]:
        del _stopped_watchers[holder]


def _current_watchers(now=None):
    """Live watchers (lazy expiry), oldest first. Each carries "state":
    "watching" or "paused" (item 32) and the R13 observation label."""
    now = time.time() if now is None else now
    with _watch_lock:
        _expire_watchers_locked(now)
        return sorted((_public_watcher(e, now) for e in _watchers.values()), key=lambda e: e["since"])


def _stopped_watcher_list(now=None):
    """Item 32: watchers released or timed out in the last
    STOPPED_KEEP_SECONDS, most recent first."""
    now = time.time() if now is None else now
    with _watch_lock:
        _expire_watchers_locked(now)
        return sorted((dict(e) for e in _stopped_watchers.values()), key=lambda e: -e["stopped_at"])


def _active_reservation_locked(now):
    """Reserved holder if the grace window is still open, else None.

    Review L2: a reservation that lapses UNUSED also drops that holder
    from the queue (it keeps no priority it did not use). Before, the
    lapsed head stayed first in line and was re-reserved on every
    release, so one abandoned session blocked every handoff for
    RESERVATION_GRACE seconds, indefinitely.
    """
    global _reserved_for, _reserved_until
    if _reserved_for is None:
        return None
    if now < _reserved_until:
        return _reserved_for
    lapsed = _reserved_for
    _reserved_for = None
    _reserved_until = 0.0
    _dequeue_locked(lapsed)
    return None


def _prune_queue_locked(now):
    """Drop queue entries not refreshed within QUEUE_ENTRY_TTL (review L2)."""
    fresh = [
        e for e in _claim_queue
        if now - float(e.get("refreshed_at", e.get("enqueued_at", now))) < QUEUE_ENTRY_TTL
    ]
    if len(fresh) != len(_claim_queue):
        _claim_queue[:] = fresh


def _reserve_head_locked(now):
    """Give the queue head an exclusive claim window, if none is active."""
    global _reserved_for, _reserved_until
    if _claim_queue and _reserved_for is None:
        _reserved_for = _claim_queue[0]["holder"]
        _reserved_until = now + RESERVATION_GRACE


def _current_claim_locked(now):
    global _claim
    _prune_queue_locked(now)
    if _claim is not None and now >= float(_claim.get("expires_at", 0.0)):
        # Lazy expiry: the claim is gone, and the queue head (if any)
        # earns a reservation from this moment of discovery.
        _claim = None
        _reserve_head_locked(now)
    return _claim


def _current_claim(now=None):
    """Copy of the live claim, or None if free / expired."""
    if now is None:
        now = time.time()
    with _claim_lock:
        current = _current_claim_locked(now)
        return dict(current) if current is not None else None


def _claim_info(claim, now):
    """Wishlist claim-yield: a readable view of a live claim -- holder,
    age, idle since last refresh, expiry and whether it is STALE. A claim
    is stale when its holder has not refreshed it for CLAIM_TTL_DEFAULT
    seconds (an expired claim is already gone: expiry is lazy). A stale
    claim may be force-released by anyone (POST /v1/hands/release
    {holder, force: true}); a fresh one only by its holder."""
    if not claim:
        return None
    since = float(claim.get("since", now))
    refreshed = _claim_refreshed_at if _claim_refreshed_at is not None else since
    refreshed = max(float(refreshed), since)
    expires_at = float(claim.get("expires_at", now))
    idle = max(0.0, now - refreshed)
    return {
        "holder": claim.get("holder"),
        "purpose": claim.get("purpose", ""),
        "since": since,
        "age_s": round(max(0.0, now - since), 1),
        "idle_s": round(idle, 1),
        "expires_at": expires_at,
        "expires_in_s": round(max(0.0, expires_at - now), 1),
        "stale_after_s": CLAIM_TTL_DEFAULT,
        "stale": idle >= CLAIM_TTL_DEFAULT,
    }


def _claim_held_message(info):
    return (
        f"hands claimed by {info['holder']!r} (claim age {info['age_s']:.0f}s, "
        f"expires in {info['expires_in_s']:.0f}s"
        + (", STALE: force-release allowed" if info["stale"] else "")
        + ")"
    )


def _claim_snapshot(now=None):
    """(claim copy or None, queue copies, reserved holder or None)."""
    if now is None:
        now = time.time()
    with _claim_lock:
        current = _current_claim_locked(now)
        reserved = _active_reservation_locked(now)
        return (
            dict(current) if current is not None else None,
            [dict(entry) for entry in _claim_queue],
            reserved,
        )


def _enqueue_locked(holder, purpose, now):
    """Append to the FIFO queue (deduped by holder); returns the 1-based
    position, or None when the queue is full. Re-queueing an existing
    holder keeps its position and refreshes the entry (review L2)."""
    for idx, entry in enumerate(_claim_queue):
        if entry["holder"] == holder:
            entry["refreshed_at"] = now
            return idx + 1
    if len(_claim_queue) >= CLAIM_QUEUE_MAX:
        return None
    _claim_queue.append(
        {"holder": holder, "purpose": purpose, "enqueued_at": now, "refreshed_at": now}
    )
    return len(_claim_queue)


def _queue_full_response(claim, reserved=None):
    body = {
        "ok": False,
        "error": f"claim queue is full ({CLAIM_QUEUE_MAX} waiting)",
        "queue_full": True,
        "claim": claim,
    }
    if reserved is not None:
        body["reserved_for"] = reserved
    return jsonify(body), 409


def set_paused(paused):
    """Pause or resume the relay (owner action). Pausing releases the
    current claim (pushed to the hands, so the resting hand lets go) and
    drops any reservation; the waiting queue is kept for resume."""
    global PAUSED, _claim, _reserved_for, _reserved_until
    paused = bool(paused)
    with _claim_lock:
        released = None
        if paused:
            now = time.time()
            current = _current_claim_locked(now)
            if current is not None:
                released = current["holder"]
                _claim = None
                _push_claim_locked(None, now)
            _reserved_for = None
            _reserved_until = 0.0
        PAUSED = paused
    if paused:
        log_event("relay PAUSED by the owner" + (f" (released claim held by {released})" if released else ""))
    else:
        log_event("relay resumed by the owner")
    return released


def _paused_response():
    return {"ok": False, "error": "relay paused by the owner", "error_code": "paused"}


def _dequeue_locked(holder):
    for idx, entry in enumerate(_claim_queue):
        if entry["holder"] == holder:
            del _claim_queue[idx]
            return True
    return False


def _reset_coordination_state():
    """Clear claim + receipt state. Test hook / internal use only."""
    global _claim, _reserved_for, _reserved_until
    with _claim_lock:
        _claim = None
        _claim_queue.clear()
        _reserved_for = None
        _reserved_until = 0.0
    with _receipts_lock:
        _receipts.clear()
        _pending_payloads.clear()
    global _watcher_timeout
    with _watch_lock:
        _watchers.clear()
        _stopped_watchers.clear()
        _watcher_timeout = None
    with _frame_hash_cache_lock:
        _frame_hash_cache.clear()


def _env_flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 16MB file-drop is ~22MB base64; hands still caps the file at 16MB

# ---------------------------------------------------------------------------
# Owner-visible events (review E4). Gateway auth failures used to go only
# to app.logger, which the windowed packaged app never shows, and nothing
# alerted -- although the gateway is the one service reachable from
# outside the PC. The packaged app drains log_queue into its window (as it
# does for the hands) and points ALERT_HOOK at the hands' ntfy alert; a
# standalone gateway can alert by itself via RELAY_NTFY_TOPIC.
# ---------------------------------------------------------------------------
LOG_QUEUE_MAX = 1000
log_queue = _queue_mod.Queue(maxsize=LOG_QUEUE_MAX)
ALERT_HOOK = None  # callable(title, message), set by the packaged app
NTFY_TOPIC = os.environ.get("RELAY_NTFY_TOPIC", "").strip()


def log_event(message):
    """Record an owner-relevant event: app logger + the window's queue."""
    app.logger.warning(message)
    try:
        log_queue.put_nowait(message)
    except _queue_mod.Full:
        pass  # nobody is draining (standalone): never block a request on it


def _post_ntfy(title, message):
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Tags": "warning"},
            timeout=10,
        )
    except requests.RequestException:
        pass


def _alert(title, message):
    """Best-effort phone alert; never raises, never blocks the request."""
    hook = ALERT_HOOK
    if callable(hook):
        try:
            hook(title, message)
        except Exception:
            pass
    elif NTFY_TOPIC:
        threading.Thread(target=_post_ntfy, args=(title, message), daemon=True).start()


def _json_body():
    """The request's JSON object, or {} for a missing/invalid/non-object body.

    Review E6: the old `get_json(force=True, silent=True) or {}` idiom let
    a JSON array or string through, and the next `.get` raised a 500.
    """
    body = request.get_json(force=True, silent=True)
    return body if isinstance(body, dict) else {}


def _short(value, limit=64):
    """A bounded printable form of a client-supplied value (review E6)."""
    text = value if isinstance(value, str) else repr(value)
    return text if len(text) <= limit else text[:limit] + "…"


_fail_by_peer = {}
_lockout_by_peer = {}
PEER_SWEEP_AT = 256  # review L6: sweep stale per-peer entries past this many peers


def _peer_key():
    try:
        return request.remote_addr or "-"
    except RuntimeError:
        return "-"


def _sweep_peers_locked(now):
    """Drop per-peer auth state that can no longer matter (review L6)."""
    for peer in [p for p, until in _lockout_by_peer.items() if now >= until]:
        del _lockout_by_peer[peer]
    for peer in [p for p, ts in _fail_by_peer.items() if not ts or now - ts[-1] >= FAIL_WINDOW]:
        del _fail_by_peer[peer]


def _gate_decision(provided):
    """Decide and record in one critical section -> (decision, tripped).

    A request that already presents the valid token is never locked out,
    even when this peer is over the failure budget. Empty TOKEN fails closed.
    tripped is True for the one failure that starts a lockout.
    """
    now = time.monotonic()
    peer = _peer_key()
    with _fail_lock:
        if TOKEN and hmac.compare_digest(provided, TOKEN):
            return "ok", False
        until = _lockout_by_peer.get(peer)
        if until is not None:
            if now < until:
                return "locked", False
            del _lockout_by_peer[peer]  # expired: forget it (review L6)
        times = [t for t in _fail_by_peer.get(peer, ()) if now - t < FAIL_WINDOW]
        times.append(now)
        tripped = len(times) >= MAX_FAILS
        if tripped:
            _lockout_by_peer[peer] = now + LOCKOUT_SECONDS
            _fail_by_peer.pop(peer, None)
        else:
            _fail_by_peer[peer] = times
        if len(_fail_by_peer) + len(_lockout_by_peer) > PEER_SWEEP_AT:
            _sweep_peers_locked(now)
        return "bad", tripped


@app.before_request
def gate():
    if request.path == "/health":
        return
    provided = request.headers.get("X-Relay-Token") or ""
    decision, tripped = _gate_decision(provided)
    if decision == "locked":
        return jsonify({"ok": False, "error": "locked out", "error_code": "burst"}), 429
    if decision != "ok":
        peer = _peer_key()
        log_event(f"rejected request to {request.path} from {peer} (bad gateway token)")
        if tripped:
            log_event(
                f"LOCKOUT: {MAX_FAILS} bad gateway tokens from {peer} inside "
                f"{FAIL_WINDOW // 60} minutes - authentication paused for "
                f"{LOCKOUT_SECONDS // 60} minutes"
            )
            _alert(
                "HandsRelay gateway LOCKOUT",
                f"{MAX_FAILS} bad gateway-token guesses from {peer}. "
                f"Authentication is paused for {LOCKOUT_SECONDS // 60} minutes.",
            )
        return jsonify({"ok": False, "error": "bad token"}), 401


@app.after_request
def noindex(resp):
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


@app.after_request
def integrity(resp):
    """R9 (#22): make a torn or emptied body detectable as torn.

    Every buffered response carries an explicit Content-Length and
    X-Body-SHA256 (hex digest of the exact body bytes). A client that
    receives a 2xx whose bytes do not hash to the header — including
    an empty body where the header names a non-empty one — knows the
    read was damaged in transit (proxy/tunnel), not "empty but valid".
    The gateway itself never emits an empty 2xx JSON body: every JSON
    route returns at least {"ok": ...}. Streamed responses are left
    alone (their length is not known up front).
    """
    if resp.direct_passthrough or resp.is_streamed:
        return resp
    data = resp.get_data()
    resp.headers["Content-Length"] = str(len(data))
    resp.headers["X-Body-SHA256"] = hashlib.sha256(data).hexdigest()
    return resp


def _hands_headers():
    return {"X-Secret": HANDS_SECRET}


def _probe(url, headers=None):
    try:
        r = requests.get(url, headers=headers or {}, timeout=2)
        return r.status_code == 200
    except requests.RequestException:
        return False


def _fetch_pc_state():
    """Hands GET /state (wishlist #13), or None on any failure."""
    try:
        r = requests.get(f"{HANDS_URL}/state", headers=_hands_headers(), timeout=2)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    if not isinstance(data, dict) or not data.get("ok"):
        return None
    return data


def _reachability():
    """Cached (5s) reachability of the two backends, for /v1/info.

    Returns (hands_ok, parser_ok, pc_state); pc_state is the hands
    /state payload, or None when the hands is unreachable or /state
    errors.
    """
    now = time.monotonic()
    with _probe_lock:
        if now - _probe_cache["at"] < 5 and _probe_cache["hands"] is not None:
            return _probe_cache["hands"], _probe_cache["parser"], _probe_cache.get("pc_state")
    hands = _probe(f"{HANDS_URL}/health", _hands_headers())
    parser = _probe(f"{PARSER_URL}/health")
    pc_state = _fetch_pc_state() if hands else None
    with _probe_lock:
        _probe_cache.update({"hands": hands, "parser": parser, "pc_state": pc_state, "at": now})
    return hands, parser, pc_state


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "gateway"})


@app.get("/v1/info")
def info():
    hands_ok, parser_ok, pc_state = _reachability()
    return jsonify(
        {
            "ok": True,
            "release": RELEASE,
            "hands_url": HANDS_URL,
            "hands_secret_configured": bool(HANDS_SECRET),
            "hands_reachable": hands_ok,
            "parser_url": PARSER_URL,
            "parser_reachable": parser_ok,
            "hands_claim": _current_claim(),
            "pc_state": pc_state,
            "watchers": _current_watchers(),
            "watchers_stopped": _stopped_watcher_list(),
            "watcher_timeout_seconds": _watcher_timeout_setting(),
            "paused": PAUSED,
            "watcher_stale_seconds": _watcher_stale_setting(),
            "claim_sync": dict(_sync_health),
        }
    )


@app.get("/v1/watch")
def watch_status():
    return jsonify(
        {
            "ok": True,
            "watchers": _current_watchers(),
            "watchers_stopped": _stopped_watcher_list(),
            "timeout_seconds": _watcher_timeout_setting(),
        }
    )


@app.post("/v1/watch")
def watch_touch():
    """{holder, timeout_seconds?}: start or refresh watcher (eyes-only) mode."""
    body = _json_body()
    paused = body.get("paused") if "paused" in body else None
    if paused is not None and not isinstance(paused, bool):
        return jsonify({"ok": False, "error": "paused must be a boolean"}), 400
    entry = _touch_watcher(
        body.get("holder"), body.get("timeout_seconds"), heartbeat=True, paused=paused
    )
    if entry is None:
        return jsonify({"ok": False, "error": "holder is required"}), 400
    return jsonify({"ok": True, "watcher": entry})


@app.post("/v1/watch/release")
def watch_release():
    body = _json_body()
    holder = str(body.get("holder") or "").strip()
    if not holder:
        return jsonify({"ok": False, "error": "holder is required"}), 400
    removed = _release_watcher(holder)
    return jsonify({"ok": True, "released": removed})


@app.post("/v1/watch/pause")
def watch_pause():
    """{holder, reason?, timeout_seconds?}: item 32, "I'm pausing". The
    watcher stays listed as paused (lease 900 s default, 30-3600 s) and
    resumes on its next touch."""
    body = _json_body()
    entry = _pause_watcher(body.get("holder"), body.get("reason"), body.get("timeout_seconds"))
    if entry is None:
        return jsonify({"ok": False, "error": "holder is required"}), 400
    return jsonify({"ok": True, "watcher": entry})


@app.post("/v1/watch/config")
def watch_config():
    """{timeout_seconds}: gateway-wide watcher timeout (30-60 s)."""
    global _watcher_timeout
    body = _json_body()
    value = body.get("timeout_seconds")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return jsonify({"ok": False, "error": "timeout_seconds must be a number"}), 400
    value = _clamp_number(value, WATCHER_TIMEOUT_DEFAULT, WATCHER_TIMEOUT_MIN, WATCHER_TIMEOUT_MAX)
    _watcher_timeout_setting()
    with _watch_lock:
        _watcher_timeout = value
    return jsonify({"ok": True, "timeout_seconds": value})


@app.get("/v1/hands/claim")
def hands_claim_status():
    claim, queue, reserved = _claim_snapshot()
    return jsonify({"ok": True, "claim": claim, "queue": queue, "reserved_for": reserved})


@app.get("/v1/hands/claim/status")
def hands_claim_status_detail():
    """Read-only claim status with holder, age, expiry and staleness:
    never grants, refreshes or releases."""
    now = time.time()
    claim, queue, reserved = _claim_snapshot(now)
    return jsonify(
        {
            "ok": True,
            "claim": claim,
            "claim_info": _claim_info(claim, now),
            "queue": queue,
            "reserved_for": reserved,
        }
    )


@app.post("/v1/hands/claim")
def hands_claim():
    global _claim, _reserved_for, _reserved_until, _claim_refreshed_at
    body = _json_body()
    holder = str(body.get("holder") or "").strip()[:HOLDER_MAX_CHARS]
    if not holder:
        return jsonify({"ok": False, "error": "holder is required"}), 400
    purpose = str(body.get("purpose") or "")[:PURPOSE_MAX_CHARS]
    ttl = _clamp_number(
        body.get("ttl_seconds", CLAIM_TTL_DEFAULT),
        CLAIM_TTL_DEFAULT,
        CLAIM_TTL_MIN,
        CLAIM_TTL_MAX,
    )
    want_queue = bool(body.get("queue"))
    if PAUSED:
        return jsonify(_paused_response()), 423
    now = time.time()
    with _claim_lock:
        current = _current_claim_locked(now)
        if current is not None and current["holder"] == holder:
            # Same holder re-claiming refreshes the lease.
            current["purpose"] = purpose or current["purpose"]
            current["expires_at"] = now + ttl
            _claim_refreshed_at = now
            _push_claim_locked(current, now)
            return jsonify({"ok": True, "granted": True, "claim": dict(current)})
        if current is not None:
            if want_queue:
                position = _enqueue_locked(holder, purpose, now)
                if position is None:
                    return _queue_full_response(dict(current))
                return jsonify(
                    {
                        "ok": True,
                        "granted": False,
                        "queued": True,
                        "position": position,
                        "claim": dict(current),
                    }
                )
            info = _claim_info(current, now)
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": _claim_held_message(info),
                        "error_code": "claim_held",
                        "claim": dict(current),
                        "claim_info": info,
                    }
                ),
                409,
            )
        # Desktop is free. An active reservation (queue head's grace
        # window after a release/expiry) blocks everyone else.
        reserved = _active_reservation_locked(now)
        if reserved is not None and reserved != holder:
            if want_queue:
                position = _enqueue_locked(holder, purpose, now)
                if position is None:
                    return _queue_full_response(None, reserved)
                return jsonify(
                    {
                        "ok": True,
                        "granted": False,
                        "queued": True,
                        "position": position,
                        "claim": None,
                        "reserved_for": reserved,
                    }
                )
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": f"hands reserved for {reserved!r} (queue head grace window)",
                        "claim": None,
                        "reserved_for": reserved,
                    }
                ),
                409,
            )
        if reserved == holder:
            _reserved_for = None
            _reserved_until = 0.0
            _dequeue_locked(holder)
        elif _claim_queue and _claim_queue[0]["holder"] == holder:
            _dequeue_locked(holder)
        elif _claim_queue:
            # A queue outlived its reservation window: the claim is a
            # free-for-all again, but a queued caller keeps no priority
            # it did not use — drop its stale entry if it jumps in.
            _dequeue_locked(holder)
        _claim = {
            "holder": holder,
            "purpose": purpose,
            "since": now,
            "expires_at": now + ttl,
            "id": uuid.uuid4().hex,
        }
        _claim_refreshed_at = now
        _push_claim_locked(_claim, now)
        return jsonify({"ok": True, "granted": True, "claim": dict(_claim)})


@app.post("/v1/hands/release")
def hands_release():
    global _claim, _reserved_for, _reserved_until
    body = _json_body()
    holder = str(body.get("holder") or "").strip()[:HOLDER_MAX_CHARS]
    if not holder:
        return jsonify({"ok": False, "error": "holder is required"}), 400
    now = time.time()
    with _claim_lock:
        if body.get("cancel_queue"):
            removed = _dequeue_locked(holder)
            if removed and _reserved_for == holder:
                _reserved_for = None
                _reserved_until = 0.0
                if _current_claim_locked(now) is None:
                    _reserve_head_locked(now)
            claim, queue, reserved = _claim_snapshot(now)
            return jsonify(
                {
                    "ok": True,
                    "cancelled_queue": removed,
                    "claim": claim,
                    "queue": queue,
                    "reserved_for": reserved,
                }
            )
        current = _current_claim_locked(now)
        if current is None:
            # Already expired (or never held). Expiry may have just reserved
            # the queue head; report that same snapshot, including reserved_for.
            claim, queue, reserved = _claim_snapshot(now)
            return jsonify(
                {
                    "ok": True,
                    "released": False,
                    "claim": claim,
                    "queue": queue,
                    "reserved_for": reserved,
                }
            )
        forced_from = None
        if current["holder"] != holder:
            info = _claim_info(current, now)
            if body.get("force") is not True:
                return (
                    jsonify(
                        {
                            "ok": False,
                            "error": "only the current holder can release the claim"
                            + (" (it is STALE: pass force: true to release it)" if info["stale"] else ""),
                            "error_code": "not_holder",
                            "claim": dict(current),
                            "claim_info": info,
                        }
                    ),
                    403,
                )
            if not info["stale"]:
                return (
                    jsonify(
                        {
                            "ok": False,
                            "error": f"force release refused: the claim is not stale (idle {info['idle_s']:.0f}s "
                            f"< {CLAIM_TTL_DEFAULT}s); {_claim_held_message(info)}",
                            "error_code": "claim_not_stale",
                            "claim": dict(current),
                            "claim_info": info,
                        }
                    ),
                    409,
                )
            forced_from = current["holder"]
        _claim = None
        _push_claim_locked(None, now)
        _reserve_head_locked(now)
        out = {
            "ok": True,
            "released": True,
            "claim": None,
            "reserved_for": _reserved_for,
        }
        if forced_from is not None:
            out["forced"] = True
            out["released_holder"] = forced_from
    if forced_from is not None:
        log_event(f"stale claim held by {forced_from!r} force-released by {holder!r}")
    return jsonify(out)


@app.post("/v1/parse")
def parse():
    """Forward a screenshot (JSON {"image_b64": ...}) to the parser service."""
    try:
        r = requests.post(f"{PARSER_URL}/parse", json=_json_body(), timeout=120)
    except requests.RequestException as exc:
        return jsonify({"ok": False, "error": f"parser unreachable: {exc}"}), 502
    try:
        return jsonify(r.json()), r.status_code
    except ValueError:
        # R4 (F12): a non-JSON parser reply (e.g. a waitress 500 page
        # during a crash loop) must not escape as Flask's HTML 500 —
        # drivers of this API parse JSON or nothing.
        return jsonify({"ok": False, "error": f"parser returned {r.status_code}"}), r.status_code


def _region_ok(region):
    """Gateway-side check of a region string: 'x,y,w,h', ints, w,h > 0."""
    try:
        parts = [int(p) for p in str(region).split(",")]
    except ValueError:
        return False
    return len(parts) == 4 and parts[2] > 0 and parts[3] > 0


def _screen_fetch_kwargs():
    """(kwargs, error) for a screen fetch, from the request's query args.

    monitor/region are only included when actually present, so a
    no-param call still invokes _fetch_screenshot() with zero arguments
    (existing stubs rely on that). Region format is validated here so a
    bad region is a 400 without the hands being called at all.
    """
    kwargs = {}
    region = request.args.get("region")
    if region is not None:
        if not _region_ok(region):
            return None, (
                jsonify(
                    {"ok": False, "error": "region must be 'x,y,w,h' (ints, w and h > 0)"}
                ),
                400,
            )
        kwargs["region"] = region
    monitor = request.args.get("monitor")
    if monitor is not None:
        try:
            kwargs["monitor"] = int(monitor)
        except ValueError:
            return None, (
                jsonify({"ok": False, "error": "monitor must be an integer"}),
                400,
            )
    return kwargs, None


@app.post("/v1/find")
def find():
    """Forward a find-text request to the parser service's /find."""
    body = _json_body()
    payload = {
        "image_b64": body.get("image_b64") or "",
        "text": body.get("text") or "",
        "case_sensitive": bool(body.get("case_sensitive")),
    }
    try:
        r = requests.post(f"{PARSER_URL}/find", json=payload, timeout=120)
    except requests.RequestException as exc:
        return jsonify({"ok": False, "error": f"parser unreachable: {exc}"}), 502
    try:
        return jsonify(r.json()), r.status_code
    except ValueError:
        # R4 (F12): same non-JSON guard as /v1/parse.
        return jsonify({"ok": False, "error": f"parser returned {r.status_code}"}), r.status_code


def _hands_forward(method, path, payload=None):
    """Forward one call to the hands, act()-style error passthrough.

    503 when no HANDS_SECRET is configured, 502 when the hands are
    unreachable or reject the gateway's secret.
    """
    if not HANDS_SECRET:
        return jsonify({"ok": False, "error": "HANDS_SECRET not configured on gateway"}), 503
    try:
        if method == "GET":
            r = requests.get(
                f"{HANDS_URL}{path}", headers=_hands_headers(), timeout=HANDS_TIMEOUT
            )
        else:
            r = requests.post(
                f"{HANDS_URL}{path}",
                json=payload,
                headers=_hands_headers(),
                timeout=HANDS_TIMEOUT,
            )
    except requests.RequestException as exc:
        return jsonify({"ok": False, "error": f"hands unreachable: {exc}"}), 502
    if r.status_code == 401:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "hands rejected the gateway's secret — HANDS_SECRET does not match the hands app's current session secret",
                }
            ),
            502,
        )
    try:
        return jsonify(r.json()), r.status_code
    except ValueError:
        return jsonify({"ok": r.ok, "status": r.status_code}), r.status_code


@app.post("/v1/elevate")
def elevate():
    """Request an elevated launch on the host.

    Body: {"name": "<allowlisted name>"}. The gateway supplies only a
    name; what runs is fixed by the operator's hands config, and the
    launch happens only if the person at the PC approves it there.
    """
    body = _json_body()
    return _hands_forward("POST", "/elevate", {"name": body.get("name") or ""})


@app.get("/v1/elevate/<eid>")
def elevate_status(eid):
    """Poll the state of an elevation request by id."""
    return _hands_forward("GET", f"/elevate/{eid}")


_capture_tls = threading.local()
_CAPTURE_RESPONSE_HEADERS = (
    "X-Screen-Width",
    "X-Screen-Height",
    "X-Screen-Monitor",
    "X-Screen-Region",
    "X-Screen-Origin",
    "X-Screen-Surface",
    "X-Screen-Rect",
)


def _variant_key(monitor=None, region=None):
    """Identity of one capture. A pixel hash alone is not this identity."""
    if monitor is not None:
        return f"monitor={int(monitor)}"
    if region is not None:
        return f"region={region}"
    return "legacy"


def _header_value(headers, name):
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    return getter(name)


def _capture_meta_from(headers, monitor, region):
    """Copy variant headers, or None when they describe a different capture."""
    monitor_header = _header_value(headers, "X-Screen-Monitor")
    region_header = _header_value(headers, "X-Screen-Region")
    if monitor is None and region is None:
        if monitor_header not in (None, "") or region_header not in (None, ""):
            return None
    if monitor is not None and monitor_header not in (None, "") and str(monitor_header) != str(int(monitor)):
        return None
    if region is not None and region_header not in (None, "") and str(region_header) != str(region):
        return None
    meta = {}
    for name in _CAPTURE_RESPONSE_HEADERS:
        value = _header_value(headers, name)
        if value not in (None, ""):
            meta[name] = str(value)
    return meta


def _fetch_screenshot(monitor=None, region=None):
    """One screenshot from the hands.

    With monitor=N appends ?monitor=N; with region="x,y,w,h" (validated
    here, in the same format the hands expect) appends ?region=... .
    With neither, the hands URL is exactly the historical one.

    Returns (png_bytes, content_type, None) on success, or
    (None, None, error_response) with the same failures /v1/screen has
    always surfaced.
    """
    if region is not None and not _region_ok(region):
        return None, None, (
            jsonify({"ok": False, "error": "region must be 'x,y,w,h' (ints, w and h > 0)"}),
            400,
        )
    url = f"{HANDS_URL}/screenshot"
    params = []
    if monitor is not None:
        params.append(f"monitor={int(monitor)}")
    if region is not None:
        params.append(f"region={region}")
    if params:
        url += "?" + "&".join(params)
    if not HANDS_SECRET:
        return None, None, (
            jsonify({"ok": False, "error": "HANDS_SECRET not configured on gateway"}),
            503,
        )
    try:
        r = requests.get(url, headers=_hands_headers(), timeout=HANDS_TIMEOUT)
    except requests.RequestException as exc:
        return None, None, (jsonify({"ok": False, "error": f"hands unreachable: {exc}"}), 502)
    if r.status_code == 401:
        return None, None, (
            jsonify(
                {
                    "ok": False,
                    "error": "hands rejected the gateway's secret — HANDS_SECRET does not match the hands app's current session secret",
                }
            ),
            502,
        )
    if r.status_code != 200:
        return None, None, (
            jsonify({"ok": False, "error": f"hands returned {r.status_code}"}),
            502,
        )
    meta = _capture_meta_from(r.headers, monitor, region)
    if meta is None:
        return None, None, (
            jsonify(
                {"ok": False, "error": "capture metadata does not match the requested variant"}
            ),
            502,
        )
    _capture_tls.meta = meta
    _capture_tls.variant = _variant_key(monitor, region)
    return r.content, r.headers.get("Content-Type", "image/png"), None


class _HashUnavailable(Exception):
    """Pillow is not installed on the gateway, so frames cannot be decoded."""


class _UndecodableFrame(Exception):
    """The hands returned bytes that are not a decodable image (review E7)."""


def _undecodable_response():
    return (
        jsonify({"ok": False, "error": "hands returned a frame that could not be decoded"}),
        502,
    )


def _open_rgb(png_bytes):
    """PNG bytes -> RGB image. _HashUnavailable without Pillow;
    _UndecodableFrame for truncated/corrupt/non-image bytes, which used to
    escape the routes as an HTML 500."""
    try:
        from PIL import Image
    except ImportError as exc:
        raise _HashUnavailable() from exc
    try:
        return Image.open(BytesIO(png_bytes)).convert("RGB")
    except Exception as exc:  # UnidentifiedImageError, truncated data, bombs
        raise _UndecodableFrame(str(exc)) from exc


# Review I4: an unchanged screen produces byte-identical PNGs, so the masked
# hash of a recently seen frame is reused instead of decoding it again (a
# /v1/screen/wait long-poll re-hashes every 200 ms; /v1/screen hashes every
# frame for its ETag). Bounded LRU keyed by the PNG's own SHA-256 + mask.
_FRAME_HASH_CACHE_MAX = 8
_frame_hash_cache = OrderedDict()
_frame_hash_cache_lock = threading.Lock()


def _decode_masked(png_bytes, mask_bottom):
    """PNG bytes -> RGB image with the bottom mask_bottom rows removed."""
    img = _open_rgb(png_bytes)
    mask = max(0, min(int(mask_bottom), img.height - 1))
    if mask:
        img = img.crop((0, 0, img.width, img.height - mask))
    return img


def _frame_hash(png_a, png_b, mask_bottom, size_match_out=None):
    """SHA-256 of a screenshot's masked pixels.

    With png_b given (stable mode), the two frames are composited first
    with a per-pixel minimum (ImageChops.darker). The minimum is
    symmetric, so the result does not depend on which sample caught a
    blink's bright phase — and a bright transient on a darker ground
    (a terminal caret) drops out entirely, leaving the untouched
    frame. Only persistent content moves the hash.
    Returns (hex_digest, width, height) of the masked frame.
    size_match_out, when a list, receives True/False: False means the two
    samples differed in size and were NOT composited. Callers that asked
    for stable=1 must report stable false in that case. The 3-tuple return
    is unchanged so existing unpackers keep working.
    """
    key = None
    if png_b is None and isinstance(png_a, (bytes, bytearray)):
        key = (hashlib.sha256(png_a).digest(), int(mask_bottom))
        with _frame_hash_cache_lock:
            hit = _frame_hash_cache.get(key)
            if hit is not None:
                _frame_hash_cache.move_to_end(key)
        if hit is not None:
            if size_match_out is not None:
                size_match_out.append(True)
            return hit
    img_a = _decode_masked(png_a, mask_bottom)
    sizes_match = True
    if png_b is not None:
        from PIL import ImageChops

        img_b = _decode_masked(png_b, mask_bottom)
        if img_b.size == img_a.size:
            img_a = ImageChops.darker(img_a, img_b)
        else:
            sizes_match = False
    if size_match_out is not None:
        size_match_out.append(sizes_match)
    result = (hashlib.sha256(img_a.tobytes()).hexdigest(), img_a.width, img_a.height)
    if key is not None:
        with _frame_hash_cache_lock:
            _frame_hash_cache[key] = result
            _frame_hash_cache.move_to_end(key)
            while len(_frame_hash_cache) > _FRAME_HASH_CACHE_MAX:
                _frame_hash_cache.popitem(last=False)
    return result


def _region_hash(png_bytes, region):
    """SHA-256 of one region's pixels (wishlist #5).

    region is [x, y, w, h]; it is clamped to the frame. Returns
    (hex_digest, width, height) of the cropped region.
    """
    img = _open_rgb(png_bytes)
    x, y, w, h = (int(v) for v in region)
    if w <= 0 or h <= 0:
        raise ValueError("region width/height must be positive")
    x0 = max(0, min(x, img.width))
    y0 = max(0, min(y, img.height))
    x1 = max(x0, min(x + w, img.width))
    y1 = max(y0, min(y + h, img.height))
    cropped = img.crop((x0, y0, x1, y1))
    return hashlib.sha256(cropped.tobytes()).hexdigest(), cropped.width, cropped.height


@app.get("/v1/monitors")
def monitors():
    """Monitor list from the hands, proxied (rectangles + count)."""
    return _proxy_hands_get("/monitors")


@app.get("/v1/screen")
def screen():
    """Current screenshot from the hands, proxied as PNG bytes.

    ?monitor=N / ?region=x,y,w,h select a capture variant on the hands;
    the ETag below then describes that variant. Carries an ETag (the
    masked-frame hash, as in /v1/screen/hash) when
    the gateway can decode images; a matching If-None-Match gets a 304
    so pollers can skip re-downloading unchanged frames.
    """
    holder = request.args.get("watcher")
    kwargs, err = _screen_fetch_kwargs()
    if err is not None:
        return err
    _capture_tls.meta = None
    _capture_tls.variant = None
    png, content_type, err = _fetch_screenshot(**kwargs)
    if err is not None:
        _record_watcher_fetch(holder, False, error="fetch_failed")
        return err
    if TRAJECTORY_RECORDING and isinstance(png, (bytes, bytearray)):
        _trajectory_screen(bytes(png))
    resp = Response(png, content_type=content_type)
    meta = getattr(_capture_tls, "meta", None) or {}
    variant = getattr(_capture_tls, "variant", None) or _variant_key(
        kwargs.get("monitor"), kwargs.get("region")
    )
    for name, value in meta.items():
        resp.headers[name] = value
    resp.headers["X-Screen-Variant"] = variant
    try:
        digest, _, _ = _frame_hash(png, None, DEFAULT_MASK_BOTTOM)
    except _HashUnavailable:
        _record_watcher_fetch(holder, False, error="decode_unavailable")
        return resp
    except (_UndecodableFrame, OSError, ValueError) as exc:
        # Review E7 (never an HTML 500) under the R13 freshness model: an
        # undecodable capture is a JSON 502 and never counts as a fresh
        # observation for the watcher.
        app.logger.warning("screen frame not decodable: %s", exc)
        _record_watcher_fetch(holder, False, error="invalid_capture")
        return jsonify({"ok": False, "error": "capture could not be validated"}), 502
    # A 304 below still validated this new capture. Contact alone does not.
    _record_watcher_fetch(holder, True, digest=digest)
    token = f"{variant}:{digest}"
    resp.headers["ETag"] = f'"{token}"'
    inm = (request.headers.get("If-None-Match") or "").strip()
    if inm and inm.strip('"') == token:
        resp.status_code = 304
        resp.set_data(b"")
    return resp


@app.get("/v1/screen/hash")
def screen_hash():
    """Hash of the current screenshot, without moving the PNG.

    A watcher polls this (tiny JSON) instead of the full frame and only
    fetches /v1/screen when the hash changes. The bottom mask_bottom px
    (default 80, the taskbar) are excluded. With ?stable=1 the gateway
    samples two frames ~0.65s apart and hashes only the pixels that
    agree, so a blinking caret cannot flip the result. ?monitor=N /
    ?region=x,y,w,h select the capture variant being hashed.
    """
    holder = request.args.get("watcher")
    stable = (request.args.get("stable") or "").lower() in ("1", "true", "yes")
    try:
        mask_bottom = int(request.args.get("mask_bottom", DEFAULT_MASK_BOTTOM))
    except ValueError:
        return jsonify({"ok": False, "error": "mask_bottom must be an integer"}), 400
    mask_bottom = max(0, min(mask_bottom, 2000))

    kwargs, err = _screen_fetch_kwargs()
    if err is not None:
        return err
    _capture_tls.meta = None
    _capture_tls.variant = None
    png_a, _, err = _fetch_screenshot(**kwargs)
    if err is not None:
        _record_watcher_fetch(holder, False, error="fetch_failed")
        return err
    png_b = None
    if stable:
        time.sleep(STABLE_SAMPLE_GAP)
        png_b, _, err = _fetch_screenshot(**kwargs)
        if err is not None:
            _record_watcher_fetch(holder, False, error="fetch_failed")
            return err
    size_match = []
    try:
        digest, width, height = _frame_hash(png_a, png_b, mask_bottom, size_match)
    except _HashUnavailable:
        _record_watcher_fetch(holder, False, error="decode_unavailable")
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "image decoding unavailable on gateway (pillow not installed)",
                }
            ),
            501,
        )
    except (_UndecodableFrame, OSError, ValueError):
        _record_watcher_fetch(holder, False, error="invalid_capture")
        return _undecodable_response()
    _record_watcher_fetch(holder, True, digest=digest)
    return jsonify(
        {
            "ok": True,
            "hash": digest,
            "stable": bool(stable and (size_match[0] if size_match else True)),
            "mask_bottom": mask_bottom,
            "width": width,
            "height": height,
            "variant": getattr(_capture_tls, "variant", None) or _variant_key(
                kwargs.get("monitor"), kwargs.get("region")
            ),
            "capture": getattr(_capture_tls, "meta", None) or {},
        }
    )


@app.get("/v1/screen/wait")
def screen_wait():
    """Long-poll sibling of /v1/screen/hash.

    Holds until the masked frame hash differs from `since` (also accepted
    as `hash`) or timeout elapses (default 25s, capped at 25). Returns the
    current hash JSON plus timed_out. Reuses _frame_hash / _fetch_screenshot.
    Single-sample (not the two-frame stable composite) so a 25s hold is not
    a chain of 0.65s gaps.
    """
    holder = request.args.get("watcher")
    since = request.args.get("since", request.args.get("hash", ""))
    if not isinstance(since, str):
        since = ""
    try:
        timeout = float(request.args.get("timeout", "25"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "timeout must be a number"}), 400
    if timeout != timeout or timeout in (float("inf"), float("-inf")):
        return jsonify({"ok": False, "error": "timeout must be a number"}), 400
    timeout = min(max(timeout, 0.0), 25.0)
    try:
        mask_bottom = int(request.args.get("mask_bottom", DEFAULT_MASK_BOTTOM))
    except ValueError:
        return jsonify({"ok": False, "error": "mask_bottom must be an integer"}), 400
    mask_bottom = max(0, min(mask_bottom, 2000))
    kwargs, err = _screen_fetch_kwargs()
    if err is not None:
        return err
    _capture_tls.meta = None
    _capture_tls.variant = None
    deadline = time.monotonic() + timeout
    timed_out = True
    digest = width = height = None
    while True:
        png, _, err = _fetch_screenshot(**kwargs)
        if err is not None:
            _record_watcher_fetch(holder, False, error="fetch_failed")
            return err
        try:
            digest, width, height = _frame_hash(png, None, mask_bottom)
        except _HashUnavailable:
            _record_watcher_fetch(holder, False, error="decode_unavailable")
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "image decoding unavailable on gateway (pillow not installed)",
                    }
                ),
                501,
            )
        except (_UndecodableFrame, OSError, ValueError):
            _record_watcher_fetch(holder, False, error="invalid_capture")
            return _undecodable_response()
        if digest != since:
            timed_out = False
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.2, remaining))
    _record_watcher_fetch(holder, True, digest=digest)
    return jsonify(
        {
            "ok": True,
            "hash": digest,
            "timed_out": timed_out,
            "stable": False,
            "mask_bottom": mask_bottom,
            "width": width,
            "height": height,
            "variant": getattr(_capture_tls, "variant", None) or _variant_key(
                kwargs.get("monitor"), kwargs.get("region")
            ),
            "capture": getattr(_capture_tls, "meta", None) or {},
        }
    )


# ---------------------------------------------------------------------------
# Queued act (wishlist #18): yielded-input accounting.
#
# The hands answer 409 when the desktop's owner is actively using it and
# an act yields. /v1/act surfaces that as-is; /v1/act/queued instead
# files a receipt and retries until a hold deadline, so the caller can
# account for the input later instead of guessing whether it landed.
#
# Privacy: a receipt records the op and the payload's key NAMES only —
# never payload values (typed text may be sensitive). The payload itself
# lives in a separate transient map, only while the receipt is held, and
# is dropped the moment the receipt reaches a terminal state; it never
# appears in any receipt response.
# ---------------------------------------------------------------------------
RECEIPT_MAX = 200
RECEIPT_RETRY_INTERVAL = 1.0
HOLD_DEFAULT = 30
HOLD_MIN = 1
HOLD_MAX = 120

_receipts_lock = threading.RLock()
_receipts = {}  # id -> receipt dict, insertion-ordered (oldest first)
_pending_payloads = {}  # receipt id -> payload, held receipts only
_receipt_thread = None
# Review L3: set when this module instance is retired (the packaged app's
# self-heal reloads the gateway in-process); the retry loop then exits
# instead of running -- and pinning the old module -- forever.
_receipt_stop = threading.Event()
PARAM_KEYS_MAX = 32  # review E6: receipts keep a bounded list of key names


def _is_yield_conflict(status, result):
    """Classify a hands 409 as a WP0b yield (hold and retry) or not.

    R4 (F11): the hands' structured error_code is authoritative when
    present ("owner_active" = owner active, "locked" = desktop locked; both
    mean hold and retry). The substring match on the human prose is
    only a fallback for an older hands that predates the code —
    rewording an error message must not silently reclassify holds as
    failures or failures as holds.
    """
    if status != 409 or not isinstance(result, dict):
        return False
    code = result.get("error_code")
    if code is not None:
        return code in ("owner_active", "burst", "locked")
    error = str(result.get("error") or "").lower()
    return "yield" in error or "user active" in error


def _store_receipt(receipt, payload=None):
    with _receipts_lock:
        _receipts[receipt["id"]] = receipt
        if payload is not None:
            _pending_payloads[receipt["id"]] = payload
        while len(_receipts) > RECEIPT_MAX:
            # R4 (F4): evict TERMINAL receipts before held ones —
            # settled history is expendable, and a burst of direct
            # applies must not push out a genuinely held receipt. The
            # just-stored receipt is never its own victim.
            victim = None
            for rid, rec in _receipts.items():
                if rid != receipt["id"] and rec.get("state") != "held":
                    victim = rid
                    break
            if victim is None:
                victim = next(iter(_receipts))
            evicted = _receipts.pop(victim)
            # Mark it BEFORE dropping it: the retry loop may already hold
            # a reference to this dict, and _pump_receipt only forwards
            # receipts whose state is still "held".
            evicted["_evicted"] = True
            if evicted.get("_dispatching") or evicted["state"] == "in-flight":
                evicted["state"] = "unknown-outcome"
            elif evicted["state"] == "held":
                evicted["state"] = "expired"
            _pending_payloads.pop(victim, None)


def _public_receipt(receipt):
    return {k: v for k, v in receipt.items() if not k.startswith("_")}


def _held_failure_diagnostics(result):
    """Retain bounded structured evidence without payload echoes.

    Deliberately omit arbitrary error/debug strings. This is diagnostic
    retention only; terminal-state classification remains unchanged.
    """
    if not isinstance(result, dict):
        return {"ok": False}
    safe = {"ok": False}
    enums = {
        "error_code": {"type_aborted", "input_aborted", "foreground_changed", "locked",
                       "yield", "owner_active", "burst", "claim_held"},
        "abort_reason": {"owner", "locked", "foreground_changed"},
        "owner_guard": {"hook", "dwtime", "none", None},
        "method_used": {"keys", "paste"},
    }
    for key, allowed in enums.items():
        value = result.get(key)
        if key in result and isinstance(value, (str, type(None))) and value in allowed:
            safe[key] = value
    # Hands caps the entire JSON request at 1,000,000 bytes.
    # This accommodates every supported input character count.
    for key in ("chars", "chars_typed"):
        value = result.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1_000_000:
            safe[key] = value
    if ("chars" in safe and "chars_typed" in safe
            and safe["chars_typed"] > safe["chars"]):
        safe.pop("chars")
        safe.pop("chars_typed")
    if isinstance(result.get("aborted"), bool):
        safe["aborted"] = result["aborted"]
    _copy_gateway_refusal(result, safe)
    return safe


def _bounded_scalars(value, limit=16):
    if not isinstance(value, dict):
        return None
    out = {}
    for key, item in list(value.items())[:limit]:
        if not isinstance(key, str):
            continue
        if isinstance(item, str):
            out[key[:64]] = item[:HOLDER_MAX_CHARS]
        elif item is None or isinstance(item, (bool, int)) or (
                isinstance(item, float) and math.isfinite(item)):
            out[key[:64]] = item
    return out


def _copy_gateway_refusal(result, safe):
    """v4 claim-yield / owner pause: refusals the GATEWAY wrote (never the
    hands, never request payload) keep their explanation -- who holds the
    claim, its age and expiry, or that the owner paused the relay."""
    code = result.get("error_code")
    if code not in ("claim_held", "paused"):
        return
    safe["error_code"] = code
    if isinstance(result.get("error"), str):
        safe["error"] = result["error"][:400]
    if isinstance(result.get("claim_held_by"), str):
        safe["claim_held_by"] = result["claim_held_by"][:HOLDER_MAX_CHARS]
    for key in ("claim_info", "claim"):
        bounded = _bounded_scalars(result.get(key))
        if bounded is not None:
            safe[key] = bounded


def classify_act_outcome(status, result):
    """Classify injection evidence, never application success or transport guesses.

    Return (state, HTTP status, copied privacy-safe evidence). Bare boolean
    ok is the existing hands completion contract; contradictory evidence
    invalidates it. Only coded, zero-effect refusals may be retried.
    """
    unknown = ("unknown-outcome", 502,
               {"ok": False, "error_code": "unknown_outcome"})
    if (not isinstance(result, dict) or type(result.get("ok")) is not bool
            or type(status) is not int):
        return unknown
    if result.get("error_code") == "unknown_outcome":
        return unknown
    counts = {}
    for key in ("chars", "chars_typed"):
        if key in result:
            v = result[key]
            if type(v) is not int or not 0 <= v <= 1_000_000:
                return unknown
            counts[key] = v
    if (len(counts) == 2 and counts["chars_typed"] > counts["chars"]):
        return unknown
    safe = _held_failure_diagnostics(result)
    if result["ok"]:
        if (not 200 <= status < 300 or result.get("aborted")
                or result.get("error_code") or result.get("abort_reason")
                or (len(counts) == 2 and counts["chars_typed"] != counts["chars"])):
            return unknown
        safe["ok"] = True
        return "applied", status, safe
    code = result.get("error_code")
    if code == "deadline_expired":
        safe["error_code"] = code
        return "refused-yielded", status, safe
    # v4 yield reasons: owner_active (owner at the desk), burst (hands
    # lockout), locked; "yield" is the pre-v4 hands spelling.
    if code in {"yield", "owner_active", "burst", "locked"} and status == 409:
        if result.get("aborted") or result.get("abort_reason") or counts.get("chars_typed", 0):
            return unknown
        return "held", status, safe
    if result.get("aborted") is True:
        reason = result.get("abort_reason")
        if reason == "owner" and "chars_typed" in counts:
            return "aborted-owner", status, safe
        if reason in {"locked", "foreground_changed"}:
            return "aborted", status, safe
    if code in {"operation_failed", "configuration_error", "invalid_operation", "capability_refused", "deadline_expired"}:
        safe["error_code"] = code
    return "failed", status, safe


def _attempt_act(op, payload):
    # An arbitrary forwarding exception cannot prove non-dispatch.
    try:
        status, result = _forward_act(op, payload)
    except Exception:
        status, result = 502, {"ok": False, "error_code": "unknown_outcome"}
    state, status, safe = classify_act_outcome(status, result)
    return state, status, safe, result


def _wall_deadline_passed(receipt, now):
    """Review E2 (v4): the pump's caller-supplied wall clock also bounds a
    hold. R13's monotonic deadline guards against clock jumps; either one
    lapsing settles the receipt, so a hold can end early but never late."""
    wall = receipt.get("deadline")
    return (type(wall) in (int, float) and type(now) in (int, float)
            and math.isfinite(wall) and now >= wall)


def _pump_receipt(receipt, now):
    """One retry step for a held receipt. Returns the receipt.

    Synchronous and thread-free so tests can drive it directly: attempts
    the forward once; a non-409 answer settles the receipt (applied, or
    failed when the hands turned out unreachable), a 409 past the
    deadline settles it as refused-yielded, otherwise it stays held.
    A receipt whose hold deadline has already passed is settled
    refused-yielded WITHOUT forwarding (review E2).
    """
    with _receipts_lock:
        if receipt.get("state") != "held" or receipt.get("_dispatching"):
            return receipt
        if receipt["id"] not in _pending_payloads:
            # The payload is gone (evicted or settled elsewhere). NEVER
            # forward an op with an empty body in its place: an empty
            # break fires a real Ctrl+C at the desktop, an empty scroll
            # scrolls by the hands' defaults. Settle honestly instead.
            receipt["state"] = "expired"
            receipt["updated_at"] = now
            return receipt
        deadline = receipt.get("_deadline_mono", receipt.get("deadline"))
        current = time.monotonic() if "_deadline_mono" in receipt else now
        if (type(deadline) not in (int, float) or not math.isfinite(deadline)
                or current >= deadline or _wall_deadline_passed(receipt, now)):
            receipt.update(state="refused-yielded", updated_at=time.time(),
                           result={"ok": False, "error_code": "deadline_expired"})
            _pending_payloads.pop(receipt["id"], None)
            return receipt
        receipt["_dispatching"] = True
        payload = dict(_pending_payloads[receipt["id"]])
        op = receipt["op"]
        context = receipt.get("_lease_context")
    attempt_started = time.monotonic()
    state, status, result, _ = _queued_attempt(op, payload, context)
    yielded = state == "held"
    with _receipts_lock:
        receipt["_dispatching"] = False
        # Eviction cannot turn an outstanding dispatch into never-started expiry.
        if receipt.get("_evicted"):
            receipt["state"] = "unknown-outcome"
            receipt["result"] = {"ok": False, "error_code": "unknown_outcome"}
            _pending_payloads.pop(receipt["id"], None)
            return receipt
        receipt["updated_at"] = time.time()
        if status is not None:
            receipt["hands_status"] = status
        if yielded:
            current = (time.monotonic() if "_deadline_mono" in receipt
                       else now + (time.monotonic() - attempt_started))
            if current >= deadline or _wall_deadline_passed(
                    receipt, now + (time.monotonic() - attempt_started)):
                receipt["state"] = "refused-yielded"
                receipt["result"] = {"ok": False, "error_code": "deadline_expired"}
                _pending_payloads.pop(receipt["id"], None)
            return receipt
        if state != "applied":
            receipt["state"] = state
            receipt["result"] = result
            _pending_payloads.pop(receipt["id"], None)
            return receipt
        receipt["state"] = "applied"
        receipt["applied_via"] = "after-hold"
        receipt["result"] = result
        _pending_payloads.pop(receipt["id"], None)
        return receipt


def _receipt_tick(clock=time.time):
    """One pass over the held receipts, oldest first (submission order).

    The clock is read again for EACH receipt, so a receipt whose turn
    comes after a slow forward is judged against the real time then.
    """
    with _receipts_lock:
        held = [r for r in _receipts.values() if r.get("state") == "held"]
    for receipt in held:
        if _receipt_stop.is_set():
            return
        try:
            _pump_receipt(receipt, clock())
        except Exception:
            continue


def _receipt_loop():
    while not _receipt_stop.wait(RECEIPT_RETRY_INTERVAL):
        _receipt_tick()


def _ensure_receipt_thread():
    global _receipt_thread
    with _receipts_lock:
        if _receipt_stop.is_set():
            return  # retired module: never start a loop that pins it
        if _receipt_thread is not None and _receipt_thread.is_alive():
            return
        _receipt_thread = threading.Thread(
            target=_receipt_loop, name="queued-act-retry", daemon=True
        )
        _receipt_thread.start()


def stop_background(timeout=2.0):
    """Stop this module's background threads (review L3).

    Called by the packaged app before it replaces this module (self-heal)
    and on Stop. A retired module never restarts its retry loop.
    """
    _receipt_stop.set()
    thread = _receipt_thread
    if thread is not None and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout)


# ---------------------------------------------------------------------------
# Toggle: trajectory recording (WP4; partner ruling 2026-11-15 18:57,
# "off by default; the switch carries the typed-text warning"). Off:
# nothing is written anywhere. On: every act forwarded to the hands is
# appended to <dir>/<YYYYMMDD>/actions.jsonl (op, parameters, status,
# error code) and every screenshot served by /v1/screen is saved next to
# it as screen-<ms>.png. Local folder only (default
# %USERPROFILE%\.HandsRelayV0.1\trajectories); nothing is uploaded.
# Typed text ("text" parameters: type, clipboard_set) is replaced by
# {"redacted": true, "chars": n} unless the second switch
# TRAJECTORY_TYPED_TEXT is also on. Key / hotkey names are kept (they
# are usually enter/tab/ctrl+s, but a password typed one key at a time
# would be visible). Recording errors never fail an act.
# ---------------------------------------------------------------------------
TRAJECTORY_RECORDING = _env_flag("HANDSRELAY_TRAJECTORY_RECORDING")
TRAJECTORY_TYPED_TEXT = _env_flag("HANDSRELAY_TRAJECTORY_TYPED_TEXT")
TRAJECTORY_DIR = os.environ.get("HANDSRELAY_TRAJECTORY_DIR", "").strip() or os.path.join(
    os.path.expanduser("~"), ".HandsRelayV0.1", "trajectories"
)
_TEXT_KEYS = ("text",)
_trajectory_lock = threading.Lock()
_trajectory_warned = False


def _trajectory_params(payload):
    out = {}
    for key, value in (payload or {}).items():
        key = str(key)[:64]
        if key in _TEXT_KEYS and not TRAJECTORY_TYPED_TEXT:
            out[key] = {"redacted": True, "chars": len(value) if isinstance(value, str) else None}
        elif isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value[:4096] if isinstance(value, str) else value
        else:
            try:
                text = json.dumps(value)
            except (TypeError, ValueError):
                text = repr(value)
            out[key] = json.loads(text) if len(text) <= 4096 else {"truncated": True}
    return out


def _trajectory_folder(now):
    folder = os.path.join(TRAJECTORY_DIR, time.strftime("%Y%m%d", time.localtime(now)))
    os.makedirs(folder, exist_ok=True)
    return folder


def _trajectory_failed(exc):
    global _trajectory_warned
    if not _trajectory_warned:
        _trajectory_warned = True
        log_event(f"trajectory recording could not write to {TRAJECTORY_DIR}: {exc.__class__.__name__}")


def _trajectory_record(op, payload, status, result, now=None):
    now = time.time() if now is None else now
    line = {
        "t": round(now, 3),
        "kind": "act",
        "op": _short(op),
        "params": _trajectory_params(payload),
        "status": status,
        "ok": isinstance(status, int) and 200 <= status < 300,
        "error_code": result.get("error_code") if isinstance(result, dict) else None,
        "typed_text": TRAJECTORY_TYPED_TEXT,
    }
    try:
        with _trajectory_lock:
            with open(os.path.join(_trajectory_folder(now), "actions.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, sort_keys=True) + "\n")
    except (OSError, TypeError, ValueError) as exc:
        _trajectory_failed(exc)


def _trajectory_screen(png, now=None):
    now = time.time() if now is None else now
    try:
        with _trajectory_lock:
            folder = _trajectory_folder(now)
            name = f"screen-{int(now * 1000)}.png"
            with open(os.path.join(folder, name), "wb") as fh:
                fh.write(png)
            with open(os.path.join(folder, "actions.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"t": round(now, 3), "kind": "screen", "file": name}, sort_keys=True) + "\n")
    except (OSError, TypeError) as exc:
        _trajectory_failed(exc)


def _forward_act(op, payload, path=None):
    """Forward one act (see _forward_act_once) and, when the trajectory
    recording toggle is on, append it to the local recording. Every act
    path (/v1/act, /v1/act/queued and its retry pump, /v1/act/batch)
    comes through here."""
    status, result = _claim_gate_act(op, payload, path=path)
    if TRAJECTORY_RECORDING:
        _trajectory_record(op, payload, status, result)
    return status, result


def _act_holder(payload):
    """(payload without 'holder', declared holder or ''). The caller may
    name itself in the body ("holder") or the X-Hands-Holder header."""
    holder = ""
    if isinstance(payload, dict) and "holder" in payload:
        payload = dict(payload)
        holder = str(payload.pop("holder") or "")
    if not holder:
        try:
            holder = request.headers.get("X-Hands-Holder", "") or ""
        except RuntimeError:  # retry pump: no request context
            holder = ""
    return payload, holder.strip()[:HOLDER_MAX_CHARS]


def _claim_gate_act(op, payload, path=None):
    """Wishlist claim-yield (muse 2026-11-16): a claim held by someone
    else must never look like a bare 'yield'.

    * A caller that names itself (holder) and is not the claim holder is
      refused BEFORE the hands is touched: 409 error_code "claim_held",
      naming the holder, the claim age and the expiry.
    * Any hands yield-409 while a claim is held by another (or by an
      unnamed caller's someone) carries the same claim details, so the
      refusal is explicit even for callers that do not name themselves.
    Callers that do not name themselves are otherwise unchanged (the
    claim stays advisory for them)."""
    payload, holder = _act_holder(payload)
    now = time.time()
    claim = _current_claim(now)
    if claim is not None and holder and claim["holder"] != holder and not PAUSED:
        info = _claim_info(claim, now)
        return 409, {
            "ok": False,
            "error": _claim_held_message(info) + " -- act refused, not forwarded",
            "error_code": "claim_held",
            "claim": claim,
            "claim_info": info,
        }
    status, result = _forward_act_once(op, payload, path=path)
    if claim is not None and claim["holder"] != holder and _is_yield_conflict(status, result):
        info = _claim_info(claim, time.time())
        result = dict(result)
        result["error_code"] = "claim_held"
        result["claim_held_by"] = info["holder"]
        result["claim_info"] = info
        result["error"] = f"{result.get('error', 'yielded')}; note: {_claim_held_message(info)}"
    return status, result


_execution_context = contextvars.ContextVar("execution_context", default=None)


def _prepare_queued_execution(op, payload, hold):
    """Negotiate before any protected action; failure never dispatches input."""
    try:
        response = requests.get(f"{HANDS_URL}/execution/capabilities",
                                headers=_hands_headers(), timeout=HANDS_TIMEOUT)
        caps = response.json()
        if (response.status_code != 200 or not isinstance(caps, dict)
                or caps.get("ok") is not True
                or type(caps.get("execution_lease_version")) is not int
                or caps["execution_lease_version"] != 1
                or not isinstance(caps.get("boot"), str) or not caps["boot"]):
            return None
        # Pin before minting: the digest cannot change on a later retry.
        hwnd = caps.get("foreground_hwnd")
        instance = caps.get("process_instance")
        # A locked desktop has no trustworthy foreground. Do not pin one.
        if (caps.get("locked") is not True and "expect_focus" not in payload
                and op != "focus" and type(hwnd) is int and hwnd > 0):
            pin = {"hwnd": hwnd}
            if isinstance(instance, str) and instance:
                pin["process_instance"] = instance
            payload["expect_focus"] = pin
        response = requests.post(f"{HANDS_URL}/execution/lease",
            headers=_hands_headers(), timeout=HANDS_TIMEOUT,
            json={"op": op, "payload": payload, "hold_seconds": hold})
        body = response.json()
        lease = body.get("lease") if isinstance(body, dict) else None
        if (response.status_code != 200 or body.get("ok") is not True
                or not isinstance(lease, dict) or type(lease.get("version")) is not int
                or lease["version"] != 1 or lease.get("boot") != caps["boot"]
                or not isinstance(lease.get("token"), str) or not lease["token"]):
            return None
        return {k: lease[k] for k in ("version", "boot", "token")}
    except (requests.RequestException, ValueError, TypeError, AttributeError):
        return None


def _queued_attempt(op, payload, context):
    token = _execution_context.set(context)
    try:
        return _attempt_act(op, payload)
    finally:
        _execution_context.reset(token)


def _forward_act_once(op, payload, path=None):
    """Forward one act to the hands. Returns (status_code, json_dict).

    This is exactly the forwarding /v1/act has always done, factored out
    so /v1/act/queued (and its retry pump) share the one code path.
    path: an explicit hands route for a non-/v1/act caller (/v1/uia/act);
    it skips the HANDS_OPS check, nothing else.
    """
    if PAUSED:
        return 423, _paused_response()
    if not HANDS_SECRET:
        return 503, {
            "ok": False,
            "error": "HANDS_SECRET not configured on gateway",
            "error_code": "configuration_error",
        }
    if path is None:
        # Review E6: a list/dict op used to raise TypeError (unhashable) -> 500.
        if not isinstance(op, str) or op not in HANDS_OPS:
            return 400, {
                "ok": False,
                "error": f"unknown op: {_short(op)!r}",
                "error_code": "invalid_operation",
            }
        path = HANDS_OP_PATHS.get(op, op)
    context = _execution_context.get()
    wire_payload = dict(payload)
    if context is not None:
        if context.get("lease") is None:
            context["lease"] = _prepare_queued_execution(op, payload, context["hold"])
            if context["lease"] is None:
                return 409, {"ok": False, "error_code": "capability_refused"}
        wire_payload = {**payload, "_execution_lease": context["lease"]}
    try:
        r = requests.post(
            f"{HANDS_URL}/{path}",
            json=wire_payload,
            headers=_hands_headers(),
            timeout=_act_timeout(op, payload),
        )
    except requests.RequestException as exc:
        return 502, {
            "ok": False,
            "error": f"hands unreachable: {exc}",
            "error_code": "unknown_outcome",
        }
    if r.status_code == 401:
        return 502, {
            "ok": False,
            "error": "hands rejected the gateway's secret — HANDS_SECRET does not match the hands app's current session secret",
        }
    try:
        return r.status_code, r.json()
    except ValueError:
        return 502, {"ok": False, "error_code": "unknown_outcome"}
def _act_timeout(op, payload):
    """Per-op timeout for the hands call (wishlist #9).

    A long "type" on the keystroke path runs at worst ~6 chars/s, so a
    flat 30s would kill legitimate long types at the gateway while the
    hands kept typing. Size the timeout to the payload instead:
    15s base + 1s per 6 chars, clamped to [HANDS_TIMEOUT, 600].
    Every other op keeps the flat HANDS_TIMEOUT.
    """
    if op == "type":
        text = (payload or {}).get("text") or ""
        try:
            n = len(text)
        except TypeError:
            n = 0
        return max(HANDS_TIMEOUT, min(600, 15 + n / 6))
    return HANDS_TIMEOUT


def _proxy_hands_get(path):
    """GET a JSON route on the hands, with _fetch_screenshot's errors:
    503 when no hands secret is configured, 502 when the hands is
    unreachable or rejects the gateway's secret."""
    if not HANDS_SECRET:
        return jsonify({"ok": False, "error": "HANDS_SECRET not configured on gateway"}), 503
    try:
        r = requests.get(
            f"{HANDS_URL}{path}", headers=_hands_headers(), timeout=HANDS_TIMEOUT
        )
    except requests.RequestException as exc:
        return jsonify({"ok": False, "error": f"hands unreachable: {exc}"}), 502
    if r.status_code == 401:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "hands rejected the gateway's secret — HANDS_SECRET does not match the hands app's current session secret",
                }
            ),
            502,
        )
    try:
        return jsonify(r.json()), r.status_code
    except ValueError:
        return jsonify({"ok": r.ok, "status": r.status_code}), r.status_code


def _uia_disabled_response():
    return {
        "ok": False,
        "error": "UI Automation tree is off (Settings: 'Let agents read the Windows UI Automation tree')",
        "error_code": "uia_disabled",
    }


@app.get("/v1/uia/tree")
def uia_tree():
    """Opt-in: the foreground window's UI Automation tree, proxied.

    Query: max_depth (default 6, max 20), max_nodes (default 400, max
    2000). Returns {snapshot, nodes:[{id, depth, name, control_type,
    rect, enabled, focused, automation_id}], truncated, window}.
    403 uia_disabled when the toggle is off; 423 paused while paused.
    """
    if not UIA_TREE_ENABLED:
        return jsonify(_uia_disabled_response()), 403
    if PAUSED:
        return jsonify(_paused_response()), 423
    qs = {}
    for key in ("max_depth", "max_nodes"):
        value = request.args.get(key)
        if value is not None:
            qs[key] = _short(value, 8)
    path = "/uia/tree"
    if qs:
        path += "?" + "&".join(f"{k}={requests.utils.quote(v)}" for k, v in qs.items())
    return _proxy_hands_get(path)


@app.post("/v1/uia/act")
def uia_act():
    """Opt-in: {snapshot, id, action: "click"|"focus", button?, human?,
    expect_focus?} on an element from /v1/uia/tree. Same forwarding as
    /v1/act (owner pause 423; the hands' owner yield 409 and focus guard
    apply). The claim stays advisory, exactly as for /v1/act."""
    if not UIA_TREE_ENABLED:
        return jsonify(_uia_disabled_response()), 403
    body = _json_body()
    payload = {
        k: body[k]
        for k in ("snapshot", "id", "action", "button", "human", "expect_focus")
        if k in body
    }
    status, result = _forward_act("uia_act", payload, path="uia/act")
    return jsonify(result), status


@app.get("/v1/windows")
def windows():
    """Open windows on the hands host (wishlist #3), proxied."""
    return _proxy_hands_get("/windows")


@app.get("/v1/clipboard")
def clipboard():
    """Current clipboard text from the hands (wishlist #6), proxied.

    Clipboard reads are sensitive: this route sits behind the gateway
    token like every /v1 route, and the hands logs every read.
    """
    return _proxy_hands_get("/clipboard/get")


@app.post("/v1/act")
def act():
    """One desktop action, forwarded to the hands.

    Body: {"op": "move"|"click"|"type"|"key"|"scroll"|"hotkey"|"break"|
           "drag"|"focus"|"clipboard_set", ...op params}
    Op params match the hands API exactly: move/click {x, y}, type {text},
    key {key}, scroll {clicks}, hotkey {keys}, break {times},
    drag {x1, y1, x2, y2}, focus {title}, clipboard_set {text} (forwarded
    to the hands' /clipboard/set).
    """
    body = _json_body()
    op = body.get("op") or ""
    payload = {k: v for k, v in body.items() if k != "op" and not k.startswith("_execution")}
    _, status, result, _ = _attempt_act(op, payload)
    return jsonify(result), status


# ---------------------------------------------------------------------------
# Toggle: batched actions (WP3; partner ruling 2026-11-15 18:57, "toggle,
# off by default"). Off: /v1/act/batch does not exist (404, the same
# response as any unknown route). On: one request runs up to BATCH_MAX
# acts in order through _forward_act, the same path /v1/act uses, so
# pause, yield and the hands' guards apply to every act. The batch stops
# at the first act the hands yields or refuses because the owner is
# active, or the relay is paused; with stop_on_error (default true) it
# also stops at any other failure. Set live by the app's settings
# (gw.BATCHED_ACTIONS) or env HANDSRELAY_BATCHED_ACTIONS=1 standalone.
# ---------------------------------------------------------------------------
BATCHED_ACTIONS = _env_flag("HANDSRELAY_BATCHED_ACTIONS")
BATCH_MAX = 50


def _owner_stop(status, result):
    """True when the act did not run because the owner stepped in (or
    paused the relay): the batch must not carry on past it."""
    if status == 423 or _is_yield_conflict(status, result):
        return True
    return (
        isinstance(result, dict)
        and result.get("error_code") == "type_aborted"
        and result.get("abort_reason") == "owner"
    )


@app.post("/v1/act/batch")
def act_batch():
    """{actions: [{op, ...params}, ...], stop_on_error?: true}."""
    if not BATCHED_ACTIONS:
        abort(404)
    body = _json_body()
    actions = body.get("actions")
    if not isinstance(actions, list) or not actions:
        return jsonify({"ok": False, "error": "actions must be a non-empty list"}), 400
    if len(actions) > BATCH_MAX:
        return jsonify({"ok": False, "error": f"at most {BATCH_MAX} actions per batch"}), 400
    if not all(isinstance(a, dict) for a in actions):
        return jsonify({"ok": False, "error": "each action must be an object with an op"}), 400
    stop_on_error = body.get("stop_on_error", True) is not False
    results, stopped_at, reason = [], None, None
    for index, action in enumerate(actions):
        op = action.get("op") or ""
        payload = {k: v for k, v in action.items() if k != "op"}
        status, result = _forward_act(op, payload)
        ok = isinstance(status, int) and 200 <= status < 300
        results.append({"index": index, "op": _short(op), "status": status, "ok": ok, "result": result})
        if _owner_stop(status, result):
            stopped_at, reason = index, ("paused" if status == 423 else "owner")
            break
        if not ok and stop_on_error:
            stopped_at, reason = index, "error"
            break
    completed = sum(1 for r in results if r["ok"])
    return jsonify(
        {
            "ok": completed == len(actions),
            "total": len(actions),
            "completed": completed,
            "stopped_at": stopped_at,
            "stop_reason": reason,
            "results": results,
        }
    )


# ---------------------------------------------------------------------------
# Toggle: custom-tools registry (WP2; partner ruling 2026-11-15 18:57,
# "toggle, off by default"). Off: /v1/tools and /v1/tools/<name>/run do
# not exist (404, like any unknown route). On: agents may LIST and RUN
# the tools the owner declared in a local JSON file (default
# %USERPROFILE%\.HandsRelayV0.1\tools.json, env HANDSRELAY_TOOLS_FILE).
# Agents cannot register tools; nothing is ever fetched from the network.
# The file is re-read on every request, so edits apply at once. Format:
#   {"version": 1, "tools": [{"name": "build", "description": "...",
#     "command": ["C:\\tools\\build.bat", "--quick"], "cwd": "C:\\src",
#     "timeout_seconds": 30, "allow_args": false}]}
# A run is subprocess.run(command [+ args], shell=False), stdin closed,
# stdout/stderr capped at TOOL_OUTPUT_MAX bytes each. Pausing the relay
# refuses runs (423) like every act. Invalid entries are skipped with a
# warning shown in GET /v1/tools.
# ---------------------------------------------------------------------------
CUSTOM_TOOLS = _env_flag("HANDSRELAY_CUSTOM_TOOLS")
TOOLS_FILE = os.environ.get("HANDSRELAY_TOOLS_FILE", "").strip() or os.path.join(
    os.path.expanduser("~"), ".HandsRelayV0.1", "tools.json"
)
TOOL_NAME_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789_-")
TOOL_TIMEOUT_DEFAULT = 30
TOOL_TIMEOUT_MAX = 600
TOOL_ARGS_MAX = 16
TOOL_ARG_CHARS_MAX = 4096
TOOL_OUTPUT_MAX = 64 * 1024
TOOLS_MAX = 64


def _load_tools(path=None):
    """(tools by name, warnings). Never raises."""
    path = path or TOOLS_FILE
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        return {}, [f"no tools file at {path}"]
    except (OSError, ValueError) as exc:
        return {}, [f"tools file could not be read ({exc.__class__.__name__})"]
    if not isinstance(doc, dict) or doc.get("version") != 1 or not isinstance(doc.get("tools"), list):
        return {}, ['tools file must be {"version": 1, "tools": [...]}']
    tools, warnings = {}, []
    for index, raw in enumerate(doc["tools"][:TOOLS_MAX]):
        where = f"tool #{index + 1}"
        if not isinstance(raw, dict):
            warnings.append(f"{where}: not an object; skipped")
            continue
        name = raw.get("name")
        if not isinstance(name, str) or not (1 <= len(name) <= 40) or not set(name) <= TOOL_NAME_CHARS:
            warnings.append(f"{where}: name must be 1-40 of a-z 0-9 _ -; skipped")
            continue
        command = raw.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(c, str) and c for c in command):
            warnings.append(f"{name}: command must be a non-empty list of strings; skipped")
            continue
        cwd = raw.get("cwd")
        if cwd is not None and not isinstance(cwd, str):
            warnings.append(f"{name}: cwd must be a string; skipped")
            continue
        timeout = raw.get("timeout_seconds", TOOL_TIMEOUT_DEFAULT)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not (1 <= timeout <= TOOL_TIMEOUT_MAX):
            warnings.append(f"{name}: timeout_seconds must be 1-{TOOL_TIMEOUT_MAX}; skipped")
            continue
        if name in tools:
            warnings.append(f"{name}: declared twice; the first is used")
            continue
        tools[name] = {
            "name": name,
            "description": str(raw.get("description") or "")[:300],
            "command": list(command),
            "cwd": cwd,
            "timeout_seconds": timeout,
            "allow_args": raw.get("allow_args") is True,
        }
    return tools, warnings


def _cap_output(data):
    data = data or b""
    text = data[:TOOL_OUTPUT_MAX].decode("utf-8", errors="replace")
    return text, len(data) > TOOL_OUTPUT_MAX


def _run_tool(tool, args, run=subprocess.run):
    started = time.monotonic()
    try:
        proc = run(
            tool["command"] + list(args),
            cwd=tool["cwd"],
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=tool["timeout_seconds"],
        )
    except subprocess.TimeoutExpired as exc:
        out, out_cut = _cap_output(exc.stdout)
        err, err_cut = _cap_output(exc.stderr)
        return {"ok": False, "timed_out": True, "exit_code": None, "stdout": out, "stderr": err,
                "truncated": out_cut or err_cut, "duration_s": round(time.monotonic() - started, 3)}
    except OSError as exc:
        return {"ok": False, "timed_out": False, "exit_code": None, "stdout": "", "stderr": "",
                "truncated": False, "error": f"could not start: {exc.__class__.__name__}: {exc}"[:300],
                "duration_s": round(time.monotonic() - started, 3)}
    out, out_cut = _cap_output(proc.stdout)
    err, err_cut = _cap_output(proc.stderr)
    return {"ok": proc.returncode == 0, "timed_out": False, "exit_code": proc.returncode, "stdout": out,
            "stderr": err, "truncated": out_cut or err_cut, "duration_s": round(time.monotonic() - started, 3)}


@app.get("/v1/tools")
def tools_list():
    if not CUSTOM_TOOLS:
        abort(404)
    tools, warnings = _load_tools()
    public = [
        {k: t[k] for k in ("name", "description", "allow_args", "timeout_seconds")}
        for t in tools.values()
    ]
    return jsonify({"ok": True, "tools": public, "warnings": warnings})


@app.post("/v1/tools/<name>/run")
def tools_run(name):
    """{args?: [str]}: run one owner-declared tool."""
    if not CUSTOM_TOOLS:
        abort(404)
    if PAUSED:
        return jsonify(_paused_response()), 423
    tools, _ = _load_tools()
    tool = tools.get(name)
    if tool is None:
        return jsonify({"ok": False, "error": f"no tool named {_short(name)!r} in the tools file"}), 404
    args = _json_body().get("args", [])
    if args and not tool["allow_args"]:
        return jsonify({"ok": False, "error": "this tool does not accept args (allow_args is false)"}), 400
    if not isinstance(args, list) or len(args) > TOOL_ARGS_MAX or not all(
        isinstance(a, str) and len(a) <= TOOL_ARG_CHARS_MAX for a in args
    ):
        return jsonify({"ok": False, "error": f"args must be a list of at most {TOOL_ARGS_MAX} strings"}), 400
    result = _run_tool(tool, args)
    log_event(f"custom tool '{name}' ran: exit {result.get('exit_code')}" + (" (timed out)" if result["timed_out"] else ""))
    if TRAJECTORY_RECORDING:
        _trajectory_record(f"tool:{name}", {"args": {"redacted": True, "count": len(args)}},
                           200, {"error_code": None if result["ok"] else "tool_failed"})
    return jsonify(dict(result, tool=name))


# ---------------------------------------------------------------------------
# Toggle: agent browser, invisible (setting agent_browser_invisible; order
# v4-toggle-nested). BROWSER TASKS ONLY, not a full desktop. Off: every
# /v1/agent-browser route is 404 like an unknown route, and turning it off
# kills the browser. On: the agent starts its own headless Edge/Chrome
# (dedicated profile, CDP on 127.0.0.1 random port; see agent_browser.py)
# and drives it over CDP only, never SendInput, so it needs no claim and
# never yields to the owner: the owner's input is never touched. Pause
# still refuses everything (423).
# ---------------------------------------------------------------------------
AGENT_BROWSER = _env_flag("HANDSRELAY_AGENT_BROWSER")
_agent_browser = None


def _get_agent_browser():
    global _agent_browser
    if _agent_browser is None:
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_browser.py")
        spec = importlib.util.spec_from_file_location("handsrelay_agent_browser", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _agent_browser = mod.AgentBrowser()
    return _agent_browser


def set_agent_browser(enabled):
    """Owner toggle, applied live. Off stops (kills) the agent's browser."""
    global AGENT_BROWSER
    AGENT_BROWSER = bool(enabled)
    if not AGENT_BROWSER and _agent_browser is not None:
        _agent_browser.stop()


def _agent_browser_call(op, fn):
    if not AGENT_BROWSER:
        abort(404)
    if PAUSED:
        return jsonify(_paused_response()), 423
    browser = _get_agent_browser()
    try:
        result = fn(browser, _json_body() if request.method == "POST" else {})
    except Exception as exc:  # BrowserError carries code/status
        code = getattr(exc, "code", "agent_browser_error")
        status = getattr(exc, "status", 500)
        return jsonify({"ok": False, "error_code": code, "error": str(exc)[:300]}), status
    log_event(f"agent browser: {op}")
    return jsonify(dict(result, ok=True))


@app.get("/v1/agent-browser/status")
def agent_browser_status():
    return _agent_browser_call("status", lambda b, _: b.status())


@app.post("/v1/agent-browser/start")
def agent_browser_start():
    return _agent_browser_call("start", lambda b, _: b.start())


@app.post("/v1/agent-browser/stop")
def agent_browser_stop():
    return _agent_browser_call("stop", lambda b, _: b.stop())


@app.post("/v1/agent-browser/open")
def agent_browser_open():
    return _agent_browser_call("open", lambda b, body: b.open(body.get("url")))


@app.post("/v1/agent-browser/read")
def agent_browser_read():
    return _agent_browser_call("read", lambda b, body: b.read(body.get("max_chars"), body.get("html") is True))


@app.post("/v1/agent-browser/screenshot")
def agent_browser_screenshot():
    return _agent_browser_call("screenshot", lambda b, _: b.screenshot())


@app.post("/v1/agent-browser/click")
def agent_browser_click():
    return _agent_browser_call("click", lambda b, body: b.click(body.get("selector")))


@app.post("/v1/agent-browser/type")
def agent_browser_type():
    return _agent_browser_call("type", lambda b, body: b.type_text(body.get("selector"), body.get("text")))


def _param_keys(payload, exclude=()):
    """Sorted payload key NAMES for a receipt, bounded in count and length
    (review E6: a receipt must not retain an arbitrarily large body)."""
    names = sorted(str(k)[:64] for k in payload.keys() if k not in exclude)
    return names[:PARAM_KEYS_MAX]


@app.post("/v1/act/queued")
def act_queued():
    """Like /v1/act, but a yield-409 is held and retried (wishlist #18).

    Same body as /v1/act plus optional hold_seconds (default 30, clamp
    1..120). A 409 whose error says the act yielded (the owner is
    actively using the desktop) files a receipt, starts the retry
    engine, and answers 202; poll GET /v1/act/queued/<id> for the
    outcome (applied / refused-yielded / failed). Any non-yield result
    is returned as /v1/act would return it, plus the receipt id: every
    submission files a receipt (R4, F4), so the outcome stays
    reconcilable via GET /v1/act/queued/<id> even after a lost
    response — a driver never has to guess whether an act landed.
    """
    body = _json_body()
    op = body.get("op") or ""
    hold = _clamp_number(body.get("hold_seconds", HOLD_DEFAULT), HOLD_DEFAULT, HOLD_MIN, HOLD_MAX)
    payload = {k: v for k, v in body.items() if k not in ("op", "hold_seconds") and not k.startswith("_execution")}
    submitted = time.time()
    receipt = {"id": uuid.uuid4().hex, "op": op, "param_keys": sorted(payload),
               "state": "in-flight", "submitted_at": submitted,
               "updated_at": submitted, "deadline": submitted, "result": None}
    _store_receipt(receipt)
    context = {"hold": hold, "lease": None}
    state, status, safe, result = _queued_attempt(op, payload, context)
    if (state == "held" and isinstance(result, dict)
            and result.get("error_code") == "locked"
            and "expect_focus" not in payload):
        state = "failed"
        safe = {"ok": False, "error_code": "target_unknown"}
    if state != "held":
        # R4 (F4): settle a receipt for the direct result too — no
        # payload is retained (nothing is held), and the id rides in
        # the response body so the caller can reconcile later.
        now = time.time()
        applied = state == "applied"
        receipt.update({
            "id": receipt["id"],
            "op": _short(op),
            "param_keys": _param_keys(payload),
            "state": state,
            # R9 (#21): which path settled it — "direct" here,
            # "after-hold" when the retry pump applies a held act.
            "applied_via": "direct" if applied else None,
            "submitted_at": now,
            "updated_at": now,
            "deadline": now,
            "hold_seconds": hold,
            "hands_status": status,
            "result": safe,
        })
        _store_receipt(receipt)
        body = dict(safe)
        body["id"] = receipt["id"]
        return jsonify(body), status
    now = time.time()
    # R9 (#20): pin the held act to the window it was aimed at. The
    # hands reports the foreground hwnd in its yield 409; retries then
    # carry expect_focus, so the hands' focus guard refuses (non-yield
    # 409 -> receipt failed) if the owner left a different window in
    # front. A caller's own expect_focus wins; focus itself is exempt
    # (its job is to change the foreground).
    pinned = None
    if (
        "expect_focus" not in payload
        and op != "focus"
        and isinstance(result, dict)
        and result.get("error_code") != "locked"
        and isinstance(result.get("foreground_hwnd"), int)
        and not isinstance(result.get("foreground_hwnd"), bool)
    ):
        pinned = result["foreground_hwnd"]
        payload["expect_focus"] = {"hwnd": pinned}
    receipt.update({
        "id": receipt["id"],
        "op": op,
        "param_keys": _param_keys(payload, exclude=("expect_focus",) if pinned is not None else ()),
        "pinned_foreground": pinned is not None,
        "state": "held",
        "submitted_at": now,
        "updated_at": now,
        "deadline": now + hold,
        "_deadline_mono": time.monotonic() + hold,
        "_lease_context": context,
        "hold_seconds": hold,
        "hands_status": status,
        "result": None,
    })
    _store_receipt(receipt, payload)
    _ensure_receipt_thread()
    return (
        jsonify({"ok": True, "queued": True, "id": receipt["id"], "state": "held"}),
        202,
    )


@app.get("/v1/act/queued")
def act_queued_list():
    with _receipts_lock:
        receipts = [
            {
                "id": r["id"],
                "op": r["op"],
                "state": r["state"],
                "submitted_at": r["submitted_at"],
            }
            for r in reversed(list(_receipts.values()))
        ]
    return jsonify({"ok": True, "receipts": receipts})


@app.get("/v1/act/queued/<receipt_id>")
def act_queued_get(receipt_id):
    with _receipts_lock:
        receipt = _receipts.get(receipt_id)
        if receipt is None:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "receipt unavailable; execution cannot be inferred",
                        "state": "unavailable",
                    }
                ),
                404,
            )
        body = _public_receipt(receipt)
    body["ok"] = True
    return jsonify(body)


# ---- file-drop (wishlist #2) -------------------------------------------------
# Gateway one-shot -> hands begin/chunk/commit. Raw chunks are 256KB
# (hands chunk_max is 512KB). File contents are never logged.

_FILEDROP_RAW_CHUNK = 256 * 1024


def _hands_post(path, payload):
    """POST JSON to the hands; returns (status_code, body_dict)."""
    try:
        r = requests.post(
            f"{HANDS_URL}{path}", json=payload, headers=_hands_headers(), timeout=HANDS_TIMEOUT
        )
    except requests.RequestException as exc:
        return 502, {"ok": False, "error": f"hands unreachable: {exc}"}
    try:
        body = r.json()
    except ValueError:
        body = {"ok": False, "error": f"hands returned {r.status_code}"}
    if not isinstance(body, dict):
        body = {"ok": False, "error": "hands returned a non-object response"}
    if r.status_code == 401:
        # Same mapping as the other hands forwarders: a hands secret mismatch
        # must not look like a bad gateway token.
        return (
            502,
            {
                "ok": False,
                "error": "hands rejected the gateway's secret — HANDS_SECRET does not match the hands app's current session secret",
            },
        )
    return r.status_code, body


def _filedrop_via_hands(folder, filename, data, sha256_hex, post=None):
    """Run begin/chunk/commit against the hands.

    `post` is a callable seam taking (path, payload) -> (status, dict);
    tests inject a fake hands through it (or by replacing _hands_post).
    Returns (body_dict, status_code); any hands error is surfaced as-is.
    """
    if post is None:
        post = _hands_post
    status, body = post(
        "/filedrop/begin",
        {"folder": folder, "filename": filename, "size": len(data), "sha256": sha256_hex},
    )
    if status != 200 or not body.get("ok"):
        return body, status
    upload_id = body.get("upload_id")
    if not upload_id:
        return {"ok": False, "error": "hands begin returned no upload_id"}, 502
    try:
        chunk_max = int(body.get("chunk_max") or _FILEDROP_RAW_CHUNK)
    except (TypeError, ValueError):
        chunk_max = _FILEDROP_RAW_CHUNK
    chunk_size = min(_FILEDROP_RAW_CHUNK, chunk_max) if chunk_max > 0 else _FILEDROP_RAW_CHUNK
    offset = 0
    while offset < len(data):
        chunk = data[offset : offset + chunk_size]
        status, body = post(
            "/filedrop/chunk",
            {
                "upload_id": upload_id,
                "offset": offset,
                "data_b64": base64.b64encode(chunk).decode("ascii"),
            },
        )
        if status != 200 or not body.get("ok"):
            try:
                post("/filedrop/abort", {"upload_id": upload_id})
            except Exception:
                pass
            return body, status
        offset += len(chunk)
    status, body = post("/filedrop/commit", {"upload_id": upload_id})
    if status != 200 or not isinstance(body, dict) or not body.get("ok"):
        try:
            post("/filedrop/abort", {"upload_id": upload_id})
        except Exception:
            pass
    return body, status


@app.post("/v1/filedrop")
def filedrop():
    """One-shot file drop: {"folder", "filename", "data_b64", "sha256"?}.

    The gateway computes sha256 itself when not supplied, and verifies
    it against a supplied one before sending anything to the hands.
    """
    if not HANDS_SECRET:
        return jsonify({"ok": False, "error": "HANDS_SECRET not configured on gateway"}), 503
    body = _json_body()
    folder = body.get("folder")
    filename = body.get("filename")
    data_b64 = body.get("data_b64")
    if not isinstance(folder, str) or not folder:
        return jsonify({"ok": False, "error": "folder required"}), 400
    if not isinstance(filename, str) or not filename:
        return jsonify({"ok": False, "error": "filename required"}), 400
    if not isinstance(data_b64, str) or not data_b64:
        return jsonify({"ok": False, "error": "data_b64 required"}), 400
    try:
        data = base64.b64decode(data_b64, validate=True)
    except (binascii.Error, ValueError):
        return jsonify({"ok": False, "error": "data_b64 is not valid base64"}), 400
    digest = hashlib.sha256(data).hexdigest()
    supplied = body.get("sha256")
    if supplied is not None:
        if not isinstance(supplied, str) or supplied.lower() != digest:
            return jsonify({"ok": False, "error": "sha256 mismatch"}), 400
    # Hands filedrop_begin rejects size > 16MB. Enforce the same here so a
    # raised MAX_CONTENT_LENGTH cannot sneak a larger object through.
    if len(data) > 16 * 1024 * 1024:
        return jsonify({"ok": False, "error": "file exceeds 16MB"}), 400
    result, status = _filedrop_via_hands(folder, filename, data, digest)
    return jsonify(result), status


# ---- wait-for (wishlist #5) ---------------------------------------------------

class _WaitFetchError(Exception):
    """Carries a ready-made (response, status) error from a fetcher."""

    def __init__(self, response):
        super().__init__("wait fetch failed")
        self.response = response


class _WaitUnsupported(Exception):
    """The hands does not support the polled capability."""


def _sample_screen_hash(fetch=None, stable=False):
    if fetch is None:
        fetch = _fetch_screenshot
    png_a, _, err = fetch()
    if err is not None:
        raise _WaitFetchError(err)
    png_b = None
    if stable:
        time.sleep(STABLE_SAMPLE_GAP)
        png_b, _, err = fetch()
        if err is not None:
            raise _WaitFetchError(err)
    try:
        digest, _, _ = _frame_hash(png_a, png_b, DEFAULT_MASK_BOTTOM)
    except _UndecodableFrame:
        raise _WaitFetchError(_undecodable_response())  # review E7: JSON 502, not HTML 500
    return digest


def _sample_region_hash(fetch=None, region=None):
    if fetch is None:
        fetch = _fetch_screenshot
    png, _, err = fetch()
    if err is not None:
        raise _WaitFetchError(err)
    try:
        digest, _, _ = _region_hash(png, region)
    except _UndecodableFrame:
        raise _WaitFetchError(_undecodable_response())  # review E7
    return digest


def _check_screen_hash_change(state, fetch=None, stable=False):
    return _sample_screen_hash(fetch=fetch, stable=stable) != state["initial_hash"]


def _check_screen_region_change(state, fetch=None):
    return _sample_region_hash(fetch=fetch, region=state["region"]) != state["initial_hash"]


def _fetch_window_titles():
    """Hands GET /window_titles -> (titles_or_None, status_code)."""
    try:
        r = requests.get(
            f"{HANDS_URL}/window_titles", headers=_hands_headers(), timeout=5
        )
    except requests.RequestException:
        return None, 502
    if r.status_code != 200:
        return None, r.status_code
    try:
        data = r.json()
    except ValueError:
        return None, r.status_code
    titles = data.get("titles") if isinstance(data, dict) else None
    return (list(titles) if isinstance(titles, list) else []), 200


def _check_window_title(contains, fetch=None):
    if fetch is None:
        fetch = _fetch_window_titles
    titles, status = fetch()
    if status in (404, 501):
        raise _WaitUnsupported()
    if status != 200:
        # _WaitFetchError carries a ready (response, status) tuple.
        # wait_for already returns exc.response. 401 is rewritten to 502 so a
        # hands secret mismatch does not look like a bad gateway token.
        out = 502 if status == 401 else status
        raise _WaitFetchError(
            (
                jsonify(
                    {
                        "ok": False,
                        "error": f"window title fetch failed ({status})",
                        "hands_status": status,
                    }
                ),
                out,
            )
        )
    if not titles:
        return False
    needle = str(contains).lower()
    return any(needle in str(t).lower() for t in titles)


def _fetch_parser_health():
    try:
        r = requests.get(f"{PARSER_URL}/health", timeout=2)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _check_parser_model_loaded(fetch=None):
    if fetch is None:
        fetch = _fetch_parser_health
    data = fetch()
    return bool(data and data.get("model_loaded"))


def _clamp_number(value, default, lo, hi):
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(num, hi))


def _check_process_exited(pid, state):
    """Pin the first observed process instance; a reused PID is not that process."""
    try:
        response = requests.get(
            f"{HANDS_URL}/process/{pid}", headers=_hands_headers(), timeout=5
        )
        if response.status_code != 200:
            status = 501 if response.status_code == 501 else 502
            raise _WaitFetchError(
                (jsonify({"ok": False, "error": "process probe unavailable",
                          "hands_status": response.status_code}), status)
            )
        body = response.json()
        if (not isinstance(body, dict) or body.get("ok") is not True
                or body.get("pid") != pid or type(body.get("running")) is not bool):
            raise ValueError("invalid process snapshot")
        if not body["running"]:
            return True
        instance = body.get("instance")
        if not isinstance(instance, str) or not instance:
            raise ValueError("missing process identity")
        original = state.setdefault("process_instance", instance)
        return instance != original
    except (requests.RequestException, ValueError):
        raise _WaitFetchError(
            (jsonify({"ok": False, "error": "process probe failed"}), 502)
        )


_WAIT_TYPES = {"screen_hash_change", "screen_region_change", "window_title",
               "parser_model_loaded", "process_exit"}


@app.post("/v1/wait")
def wait_for():
    """Poll a condition until it holds or the deadline passes.

    Body: {"condition": {...}, "timeout_seconds": 30, "poll_seconds": 1}
    """
    body = _json_body()
    condition = body.get("condition")
    if not isinstance(condition, dict):
        return jsonify({"ok": False, "error": "condition required"}), 400
    ctype = condition.get("type")
    if ctype not in _WAIT_TYPES:
        return jsonify({"ok": False, "error": f"unknown condition type: {ctype!r}"}), 400
    timeout = _clamp_number(body.get("timeout_seconds", 30), 30, 1, 300)
    poll = _clamp_number(body.get("poll_seconds", 1), 1, 0.2, 10)

    state = {}
    stable = False
    if ctype == "screen_hash_change":
        stable = bool(condition.get("stable"))
        try:
            state["initial_hash"] = _sample_screen_hash(stable=stable)
        except _WaitFetchError as exc:
            return exc.response
        except _HashUnavailable:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "image decoding unavailable on gateway (pillow not installed)",
                    }
                ),
                501,
            )
    elif ctype == "screen_region_change":
        region = condition.get("region")
        if (
            not isinstance(region, (list, tuple))
            or len(region) != 4
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in region)
            # Review E7: JSON allows NaN/Infinity, and int(inf) raised an
            # uncaught OverflowError (500) in the region hash.
            or any(not math.isfinite(v) for v in region)
        ):
            return jsonify({"ok": False, "error": "region must be [x, y, w, h]"}), 400
        state["region"] = list(region)
        try:
            state["initial_hash"] = _sample_region_hash(region=state["region"])
        except _WaitFetchError as exc:
            return exc.response
        except _HashUnavailable:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "image decoding unavailable on gateway (pillow not installed)",
                    }
                ),
                501,
            )
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
    elif ctype == "window_title":
        if not isinstance(condition.get("contains"), str) or not condition.get("contains"):
            return jsonify({"ok": False, "error": "contains required"}), 400
    elif ctype == "process_exit":
        pid = condition.get("pid")
        if type(pid) is not int or not 0 < pid <= 0xFFFFFFFF:
            return jsonify({"ok": False, "error": "pid must be a positive DWORD"}), 400

    start = time.monotonic()
    while True:
        try:
            if ctype == "screen_hash_change":
                held = _check_screen_hash_change(state, stable=stable)
            elif ctype == "screen_region_change":
                held = _check_screen_region_change(state)
            elif ctype == "window_title":
                held = _check_window_title(condition.get("contains"))
            elif ctype == "process_exit":
                held = _check_process_exited(condition["pid"], state)
            else:  # parser_model_loaded
                held = _check_parser_model_loaded()
        except _WaitFetchError as exc:
            return exc.response
        except _WaitUnsupported:
            return jsonify({"ok": False, "error": "hands does not support window titles"}), 501
        except _HashUnavailable:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "image decoding unavailable on gateway (pillow not installed)",
                    }
                ),
                501,
            )
        waited = time.monotonic() - start
        if held:
            return jsonify(
                {"ok": True, "held": True, "waited_seconds": waited, "condition": condition}
            )
        if waited >= timeout:
            return jsonify(
                {
                    "ok": True,
                    "held": False,
                    "timeout": True,
                    "waited_seconds": waited,
                    "condition": condition,
                }
            )
        time.sleep(min(poll, max(0.0, timeout - waited)))


# ---------------------------------------------------------------------------
# Browser DOM adapter (partner ruling 2026-11-15, toggle, default OFF).
#
# Attaches only to a browser the USER started with remote debugging on
# 127.0.0.1 (see dom_adapter.py). While the toggle is off every /v1/dom
# route answers 403 error_code "dom_adapter_off" and nothing else in the
# gateway changes. Reads (tabs, snapshot) respect Pause; acts (click,
# type) also need the caller to HOLD the hands claim and the owner to be
# idle past the give-way threshold, the same rules the hands enforce.
# ---------------------------------------------------------------------------
DOM_ADAPTER_ENABLED = False
_dom_mod = None


def set_dom_adapter(enabled):
    """Owner toggle (Settings -> browser_dom_adapter), applied live."""
    global DOM_ADAPTER_ENABLED
    enabled = bool(enabled)
    if enabled != DOM_ADAPTER_ENABLED:
        log_event("browser DOM adapter " + ("ENABLED by the owner" if enabled else "disabled by the owner"))
    DOM_ADAPTER_ENABLED = enabled
    return DOM_ADAPTER_ENABLED


def _dom():
    global _dom_mod
    if _dom_mod is None:
        import importlib.util
        import sys as _sys
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dom_adapter.py")
        spec = importlib.util.spec_from_file_location("hands_relay_dom_adapter", path)
        mod = importlib.util.module_from_spec(spec)
        _sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        _dom_mod = mod
    return _dom_mod


def _dom_refusal(act=False, holder=""):
    """(json, status) when the request may not proceed, else None."""
    if not DOM_ADAPTER_ENABLED:
        return {
            "ok": False,
            "error": "the browser DOM adapter is off; the PC owner can turn it on in the relay's Settings",
            "error_code": "dom_adapter_off",
        }, 403
    if PAUSED:
        return _paused_response(), 423
    if not act:
        return None
    claim = _current_claim()
    if not holder:
        return {"ok": False, "error": "holder is required for DOM actions", "error_code": "bad_request"}, 400
    if claim is None or claim.get("holder") != holder:
        return {
            "ok": False,
            "error": "DOM actions need the hands claim: POST /v1/hands/claim first",
            "error_code": "claim_required",
            "claim": claim,
        }, 409
    pc_state = _fetch_pc_state()
    idle = (pc_state or {}).get("owner_idle_seconds")
    try:
        threshold = float(os.environ.get("HANDS_YIELD_IDLE_SECONDS", "5"))
    except ValueError:
        threshold = 5.0
    if not isinstance(idle, (int, float)):
        return {
            "ok": False,
            "error": "cannot confirm the owner is away (hands /state unavailable); DOM action refused",
            "error_code": "owner_state_unknown",
        }, 503
    if idle < threshold:
        return {
            "ok": False,
            "error": "user active: giving way to the PC owner",
            "error_code": "owner_active",
            "owner_idle_seconds": idle,
        }, 409
    return None


def _dom_call(fn):
    dom = _dom()
    try:
        return jsonify({"ok": True, **fn(dom)}), 200
    except dom.DomError as exc:
        return jsonify({"ok": False, "error": exc.message, "error_code": exc.code}), exc.status


@app.get("/v1/dom/tabs")
def dom_tabs():
    refused = _dom_refusal()
    if refused:
        return jsonify(refused[0]), refused[1]
    return _dom_call(lambda dom: {"tabs": dom.list_tabs()})


@app.post("/v1/dom/snapshot")
def dom_snapshot():
    """Body: {"tab_id", "max_chars"? (1000-200000, default 20000),
    "include_values"? (default false; passwords are redacted always)}."""
    refused = _dom_refusal()
    if refused:
        return jsonify(refused[0]), refused[1]
    body = _json_body()
    return _dom_call(lambda dom: dom.snapshot(
        body.get("tab_id"), body.get("max_chars"), body.get("include_values") is True))


@app.post("/v1/dom/act")
def dom_act():
    """Body: {"holder", "tab_id", "op": "click"|"type", "selector", "text"? }."""
    body = _json_body()
    holder = str(body.get("holder") or "").strip()[:HOLDER_MAX_CHARS]
    refused = _dom_refusal(act=True, holder=holder)
    if refused:
        return jsonify(refused[0]), refused[1]
    op = body.get("op")
    tab_id, selector = body.get("tab_id"), body.get("selector")
    if op == "click":
        fn = lambda dom: {"op": op, **dom.click(tab_id, selector)}  # noqa: E731
    elif op == "type":
        fn = lambda dom: {"op": op, **dom.type_text(tab_id, selector, body.get("text"))}  # noqa: E731
    else:
        return jsonify({"ok": False, "error": f"unknown DOM op: {_short(op)!r} (click, type)",
                        "error_code": "bad_request"}), 400
    log_event(f"DOM {op} by {holder} on tab {_short(tab_id)} selector {_short(selector)}")
    return _dom_call(fn)


_claim_authority = None


def bind_claim_authority(directory=None):
    """Allocate this process's epoch before any claim traffic is served.

    Failure is fail-closed: the process does not start. The directory is
    the argument, else HANDS_RELAY_STATE_DIR, else services/gateway/state
    beside this file. The returned store stays held until release().
    """
    global _claim_authority
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "epoch_store.py")
    spec = importlib.util.spec_from_file_location("hands_gateway_epoch_store", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if directory is None:
        directory = os.environ.get("HANDS_RELAY_STATE_DIR") or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "state")
    store = module.EpochStore(directory)
    try:
        held = store.acquire()
    except module.SyncError as exc:
        raise SystemExit(f"claim authority fail closed: {exc}") from exc
    if not held:
        raise SystemExit("another gateway holds claim authority")
    _claim_authority = store
    reconcile_claim_snapshot()
    return store


if __name__ == "__main__":
    from waitress import serve

    bind_claim_authority()
    # R4 (F9): 8 threads starved act traffic whenever long-poll
    # endpoints (wait_for up to 300 s, screen/wait 25 s, a long type
    # forward up to 600 s) overlapped — every route, /v1/info
    # included, queued behind them. 32 costs nothing at this scale.
    # Review I1: the bind address is configurable. The default stays
    # 0.0.0.0 (all interfaces) so an agent that reaches the gateway by the
    # PC's tailnet IP keeps working; set RELAY_GATEWAY_HOST=127.0.0.1 when
    # the gateway is only reached through Tailscale Serve or a local proxy.
    host = os.environ.get("RELAY_GATEWAY_HOST", "0.0.0.0").strip() or "0.0.0.0"
    print(f"HandsRelay gateway listening on {host}:5010")
    serve(app, host=host, port=5010, threads=32)
