"""Idempotency keys for side-effecting actions (DH-2).

The same business action must produce the same key even when the caller
reorders the parameters, writes ``2026/9/12`` instead of ``2026-09-12``, or
hands us amap (GCJ-02) coordinates where the previous attempt used GPS
(WGS-84). Without that, a retry after a timeout silently becomes a second
booking.

Normalisation is where the correctness lives; hashing is only the last step
that turns a canonical structure into a database key.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

from app.domain.geo import Coord
from app.domain.timebase import to_ymd

#: Digest characters kept in the returned key. 128 bits makes an accidental
#: collision irrelevant, and the ledger's primary key enforces uniqueness.
_DIGEST_CHARS = 32


def _normalise_coord(coord: Coord) -> dict[str, float]:
    """Collapse a coordinate to WGS-84 at 4 decimals (~11 m).

    Two providers describing the same gate must not produce two keys just
    because one of them carries a few hundred metres of map bias.
    """

    wgs = coord.to_wgs84()
    return {"lat": round(wgs.lat, 4), "lon": round(wgs.lon, 4)}


def _is_coord_mapping(value: Any) -> bool:
    return isinstance(value, Mapping) and "lat" in value and "lon" in value


def _normalise_date(text: str) -> str | None:
    try:
        return to_ymd(text)
    except ValueError:
        return None


def _sort_key(item: Any) -> str:
    return json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)


def _canonical(value: Any) -> Any:
    """Recursively normalise one value into a deterministic, JSON-safe shape."""

    if isinstance(value, Coord):
        return _normalise_coord(value)

    if isinstance(value, Mapping):
        if _is_coord_mapping(value):
            try:
                return _normalise_coord(Coord.model_validate(dict(value)))
            except ValueError:
                pass
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key).strip()
            if not key:
                continue
            normalised = _canonical(raw_value)
            # Absent and empty carry no information, and would otherwise let
            # the same action produce two keys.
            if normalised is None or normalised == "":
                continue
            result[key] = normalised
        return {key: result[key] for key in sorted(result)}

    if isinstance(value, (list, tuple, set, frozenset)):
        items = [
            item
            for item in (_canonical(entry) for entry in value)
            if item is not None and item != ""
        ]
        # Passenger / ticket-class lists are sets in disguise, and a caller
        # that happens to sort them differently must not change the key.
        return sorted(items, key=_sort_key)

    if isinstance(value, bool):
        return value

    if isinstance(value, date):  # covers datetime as well
        return to_ymd(value)

    if isinstance(value, str):
        text = " ".join(value.split())
        if text and any(char.isdigit() for char in text):
            normalised_date = _normalise_date(text)
            if normalised_date is not None:
                return normalised_date
        return text

    if isinstance(value, int):
        return value

    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return int(value) if value.is_integer() else value

    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _action_key(action_kind: str) -> str:
    return str(action_kind or "").strip().lower()


def normalize_params(params: Mapping[str, Any]) -> dict[str, Any]:
    """Return the canonical form of an action's parameters."""

    normalised = _canonical(params)
    return normalised if isinstance(normalised, dict) else {}


def subject_fingerprint(action_kind: str, subject: Mapping[str, Any]) -> str:
    """Stable digest of *what* the action is about, ignoring presentation."""

    payload = {"action": _action_key(action_kind), "subject": normalize_params(subject)}
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


#: Identifiers that are regenerated every time a plan is built. They identify a
#: *rendering* of the plan, not the business act, so they must never reach the
#: key: replanning the same trip would otherwise mint a new key and the same
#: booking could be placed twice.
_VOLATILE_KEYS = frozenset(
    {
        "leg_id",
        "action_id",
        "candidate_id",
        "plan_version_id",
        "plan_hash",
        "consent_ref",
        "session_id",
        "trip_id",
        "segment_id",
        "attraction_id",
    }
)


def idem_subject(params: Mapping[str, Any]) -> dict[str, Any]:
    """Strip regenerated ids so the fingerprint survives a replan.

    Everything else is kept *including nested structures*, because a changed
    travel date or a different train genuinely is a different act and must get a
    different key.
    """

    def _strip(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                str(key): _strip(item)
                for key, item in value.items()
                if str(key) not in _VOLATILE_KEYS
            }
        if isinstance(value, (list, tuple, set, frozenset)):
            stripped = [_strip(item) for item in value]
            # A list that was only ids carries no business meaning once stripped.
            return [item for item in stripped if item not in (None, {}, "")]
        return value

    stripped = _strip(params)
    return stripped if isinstance(stripped, dict) else {}


def plan_action_key(*, session_id: str, action_kind: str, params: Mapping[str, Any]) -> str:
    """The ledger key for an action built from a plan.

    Derived from the business subject rather than the plan version, so the same
    act retried after a replan lands on the same primary key and the database --
    not the application -- rejects the duplicate (P4).
    """

    return idem_key(session_id=session_id, action_kind=action_kind, subject=idem_subject(params))


def idem_key(*, session_id: str, action_kind: str, subject: Mapping[str, Any]) -> str:
    """Database key for one action attempt inside one session.

    Scoped by session on purpose: repeating a booking in a *new* session is a
    new user intent, while repeating it inside the same session is a retry.
    """

    fingerprint = subject_fingerprint(action_kind, subject)
    digest = hashlib.sha256(f"{str(session_id or '').strip()}|{fingerprint}".encode("utf-8")).hexdigest()
    return f"{_action_key(action_kind)}:{digest[:_DIGEST_CHARS]}"
