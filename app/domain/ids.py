"""Opaque identifier generation."""

from __future__ import annotations

import secrets
import time


def new_id(prefix: str) -> str:
    """Return a sortable, collision-resistant id such as ``ses_m8f3k2a1c4``."""

    stamp = base36(int(time.time() * 1000))
    rand = secrets.token_hex(4)
    return f"{prefix}_{stamp}{rand}"


_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def base36(value: int) -> str:
    if value == 0:
        return "0"
    out = ""
    while value:
        value, rem = divmod(value, 36)
        out = _B36[rem] + out
    return out
