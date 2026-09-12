"""Crockford-base32 ULID-ish request ids: sortable, opaque, no deps."""

import os
import time

_ENCODING = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_request_id() -> str:
    """Return a 26-char ULID-shaped identifier (timestamp + randomness)."""
    ts = int(time.time() * 1000)
    ts_chars = []
    for i in range(9, -1, -1):
        ts_chars.append(_ENCODING[(ts >> (i * 5)) & 0x1F])
    entropy = os.urandom(16)
    ent_chars = [_ENCODING[b & 0x1F] for b in entropy]
    return "".join(ts_chars) + "".join(ent_chars)
