"""Capability result cache (framework §4.1.4, §6.10).

Two decisions do the real work here:

* **Keys are canonical.** Coordinates are collapsed to WGS-84 at four decimals
  and dates to ``YYYY-MM-DD`` before hashing. Without that, a geocode of the
  same gate via two providers -- or the same request written ``2026/9/12`` --
  would never hit the cache, and the 5,000/month amap search pool would be spent
  on duplicates.
* **Some results are never stored.** Errors are not cached (a transient outage
  must not become sticky), and payloads carrying identity data are not written
  at all (L8: PII does not accumulate on disk because of a cache).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any

from pydantic import BaseModel

from app.capabilities.contract import CapabilityResult, Provenance, ResultStatus
from app.domain.geo import Coord
from app.domain.timebase import now_local, to_ymd
from app.store.repositories import CacheRepo

#: Keys whose presence means the payload contains identity data.
PII_MARKERS = (
    "rider_identity",
    "passenger",
    "id_card",
    "idcard",
    "passport",
    "phone",
    "mobile",
    "证件",
    "身份证",
)


def contains_pii(value: Any, *, _depth: int = 0) -> bool:
    """Recursively look for identity data before it can reach the disk."""

    if _depth > 8:
        return False
    if isinstance(value, Mapping):
        for key, item in value.items():
            if any(marker in str(key).lower() for marker in PII_MARKERS):
                return True
            if contains_pii(item, _depth=_depth + 1):
                return True
        return False
    if isinstance(value, (list, tuple, set)):
        return any(contains_pii(item, _depth=_depth + 1) for item in value)
    return False


class CachedEntry(BaseModel):
    cache_key: str
    capability_id: str
    payload: Any = None
    provenance: Provenance | None = None
    fetched_at: datetime
    ttl_seconds: int = 0
    expires_at: datetime
    contains_pii: bool = False
    stale: bool = False

    def ttl_left(self, *, now: datetime | None = None) -> int:
        moment = now or now_local()
        return int((self.expires_at - moment).total_seconds())


def _canonical(value: Any, *, precision: int) -> Any:
    if isinstance(value, Coord):
        wgs = value.to_wgs84()
        return {"lat": round(wgs.lat, precision), "lon": round(wgs.lon, precision)}
    if isinstance(value, Mapping):
        if "lat" in value and "lon" in value:
            try:
                return _canonical(Coord.model_validate(dict(value)), precision=precision)
            except ValueError:
                pass
        return {
            str(key).strip(): _canonical(item, precision=precision)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key).strip()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [_canonical(item, precision=precision) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True, default=str, ensure_ascii=False))
    if isinstance(value, bool):
        return value
    if isinstance(value, (date, datetime)):
        return to_ymd(value)
    if isinstance(value, str):
        text = " ".join(value.split())
        try:
            if text and any(char.isdigit() for char in text):
                return to_ymd(text)
        except ValueError:
            pass
        return text
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


class CacheStore:
    def __init__(self, db: Any, *, coord_precision: int = 4, repo: CacheRepo | None = None) -> None:
        self.coord_precision = int(coord_precision)
        self._repo = repo or CacheRepo(db)

    # ------------------------------------------------------------------ keys
    def make_key(self, capability_id: str, params: Mapping[str, Any]) -> str:
        canonical = json.dumps(
            {
                "capability": str(capability_id),
                "params": _canonical(dict(params or {}), precision=self.coord_precision),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------ read
    def get(self, key: str, *, now: datetime | None = None) -> CachedEntry | None:
        """Return the entry whether or not it has expired; ``stale`` says which."""

        row = self._repo.get(key)
        if row is None:
            return None
        return self._entry(row, now=now)

    def get_fresh(self, key: str, *, now: datetime | None = None) -> CachedEntry | None:
        entry = self.get(key, now=now)
        if entry is None or entry.stale:
            return None
        return entry

    # ----------------------------------------------------------------- write
    def put(
        self,
        key: str,
        result: CapabilityResult,
        *,
        ttl_seconds: int,
        capability_id: str = "",
    ) -> bool:
        """Store a successful result. Returns whether anything was written."""

        if result.status is ResultStatus.ERROR or ttl_seconds <= 0:
            return False
        if contains_pii(result.data):
            return False

        fetched = result.provenance.fetched_at if result.provenance else now_local()
        expires = fetched + timedelta(seconds=ttl_seconds)
        self._repo.upsert(
            cache_key=key,
            capability_id=capability_id,
            payload_json=json.dumps(result.data, ensure_ascii=False, default=str),
            provenance_json=result.provenance.model_dump_json() if result.provenance else None,
            fetched_at=fetched.isoformat(),
            ttl_seconds=int(ttl_seconds),
            expires_at=expires.isoformat(),
        )
        return True

    # --------------------------------------------------------------- helpers
    def _entry(self, row: Mapping[str, Any], *, now: datetime | None = None) -> CachedEntry:
        moment = now or now_local()
        fetched_at = _parse(row.get("fetched_at")) or moment
        expires_at = _parse(row.get("expires_at")) or fetched_at
        provenance = None
        raw_provenance = row.get("provenance_json")
        if raw_provenance:
            try:
                provenance = Provenance.model_validate_json(raw_provenance)
            except ValueError:
                provenance = None
        try:
            payload = json.loads(row.get("payload_json") or "null")
        except ValueError:
            payload = None
        return CachedEntry(
            cache_key=row["cache_key"],
            capability_id=row.get("capability_id") or "",
            payload=payload,
            provenance=provenance,
            fetched_at=fetched_at,
            ttl_seconds=int(row.get("ttl_seconds") or 0),
            expires_at=expires_at,
            contains_pii=bool(row.get("contains_pii")),
            stale=expires_at <= moment,
        )

    def purge_expired(self, *, now: datetime | None = None) -> int:
        return self._repo.purge_expired(before=now or now_local())


def _parse(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


__all__ = ["CacheStore", "CachedEntry", "PII_MARKERS", "contains_pii"]
