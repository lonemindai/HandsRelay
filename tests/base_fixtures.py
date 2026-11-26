"""Committed copies of the files the byte-for-byte guards compare against.

Their original commits are not in the published history, so each file is
kept here and pinned to its git blob id. A missing or changed fixture
fails the guard instead of skipping it.
"""
import hashlib
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "base"

BLOBS = {
    ("178a719", "hands/hands_relay_hands.py"): "ed1705bdc1723c2d39ad6b7a1bc57d4ec6d23064",
    ("178a719", "hands/motion.py"): "cad375c64be9c22a27a5f8a411c20890a5ef909b",
    ("178a719", "services/gateway/app.py"): "ad1714c2e7a34f4e165817641ae8d762ed562fba",
    ("e755d66", "services/gateway/app.py"): "3ad70575005eea7f94c6f95e044bafa4c1cf616e",
    ("1b585ac", "services/gateway/app.py"): "7b84688bfc109b5f9622be601d9a0ff080c61e00",
}


def base_bytes(commit, rel):
    """The exact bytes of rel at commit, verified against its blob id."""
    data = (FIXTURES / commit / f"{rel}.base").read_bytes()
    blob = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
    if blob != BLOBS[(commit, rel)]:
        raise AssertionError(f"base fixture changed: {commit}:{rel}")
    return data
