"""Multi-city segment parsing (content layer F1, decision D3).

Deterministic on purpose (P1): the LLM's only job is to hand over whatever the
user said; deciding *which cities, in what order, for how many days* is a
decision, and decisions belong in code. A mis-parse here would silently reshape
the whole itinerary, so every guess is reported in ``notes`` and the caller is
expected to surface it as an assumption rather than hide it.

Pure module: no IO, no provider SDK, no LLM, no graph imports.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from app.domain.ids import new_id

#: Upper bound on how many city stays a single trip may contain. Beyond this the
#: parse is almost certainly wrong (a sentence, not an itinerary).
MAX_SEGMENTS = 4

#: Segment separators, longest first so "然后去" wins over "去".
_SEPARATORS = ("然后去", "然后", "之后去", "之后", "再去", "接着去", "接着", "转到", "回")
_SPLIT_CHARS = "+,+、;；，,。>→->~"

#: Chinese numerals that actually show up in travel requests.
_CN_DIGITS: dict[str, int] = {
    "零": 0, "〇": 0, "一": 1, "壹": 1, "二": 2, "两": 2, "贰": 2, "三": 3, "叁": 3,
    "四": 4, "肆": 4, "五": 5, "伍": 5, "六": 6, "陆": 6, "七": 7, "柒": 7,
    "八": 8, "捌": 8, "九": 9, "玖": 9, "十": 10, "拾": 10,
}

_DAY_WORDS = ("天", "日", "晚", "夜")
_UNIT_CLASS = "天日晚夜"
#: A city token must not contain a day word, or a greedy match would swallow
#: "天" out of "2天" and invent a place called "天".
_CITY_CLASS = r"(?:(?![" + _UNIT_CLASS + r"])[\u4e00-\u9fa5A-Za-z·．\.\-])"

#: A "<city><n><day-word>" pair. The city part must not contain a day word *or a
#: digit* -- without the digit guard the greedy group would swallow the "2" of
#: "2天", fail to find digits after it, and hand back a one-character place name.
_PAIR_RE = re.compile(
    r"((?:(?![" + _UNIT_CLASS + r"\d])[\u4e00-\u9fa5A-Za-z·．\.\-]){1,12})\s*(\d{1,2}|[零〇一壹二两贰三叁四肆五伍六陆七柒八捌九玖十拾]{1,3})\s*[" + _UNIT_CLASS + r"]"
)

#: "玩/待/住/停留/安排 N 天" -> the days belong to whatever city precedes it.
_BARE_DAYS_RE = re.compile(
    r"(?:玩|待|呆|住|停留|安排|留)\s*(\d{1,2}|[零〇一壹二两贰三叁四肆五伍六陆七柒八捌九玖十拾]{1,3})\s*[" + _UNIT_CLASS + r"]"
)

#: "<n>人 / <n>元 / 预算 <n>" and friends: counts that are neither a city nor a
#: stay duration.
_COUNT_RE = re.compile(r"\d{1,2}\s*(?:个?人|位|名|大|小|岁|元|块|预算|月|号)")
_CITY_RE = re.compile(r"(?:(?![" + _UNIT_CLASS + r"\d])[\u4e00-\u9fa5A-Za-z·．\.\-]){2,12}")
_NAME_STOPWORDS = (
    "赏樱之旅", "休闲游", "自由行", "亲子游", "商务", "蜜月",
    "之旅", "游玩", "预算", "左右", "以内", "出发", "往返", "单程",
    "五日游", "三日游", "七日游", "一日游", "两日游", "四日游", "六日游",
)
#: Verb fillers that sit between a city and its day count.
_VERB_FILLERS = ("玩", "待", "呆", "住", "停留", "安排", "逛", "留")

#: Trailing filler that should not become part of a city name.
_FILLER = (
    "我想去", "想去", "我要去", "要去", "去", "玩", "待", "呆", "住", "停留",
    "安排", "然后", "接着", "之后", "顺路", "顺便", "一共", "总共", "大约", "前后",
    "大概", "最后", "再", "和", "与", "及",
)

#: Words that are pure connectives -- a segment made only of these is junk.
_CONNECTIVES = frozenset(
    {"最后", "然后", "接着", "之后", "再", "回", "去", "和", "与", "及", "顺便", "顺路", "共", "一共", "总共"}
)


class SegmentSpec(BaseModel):
    """One city stay as requested by the user. ``days`` may be unknown."""

    city: str
    days: int | None = None
    lodging_area: str | None = None
    explicit_days: bool = False


class TripSegmentSpec(BaseModel):
    """Segment after day allocation, before geocoding.

    ``segments.py`` never calls a capability, so this carries a city *name* and
    an opaque coord slot; ``plan_content`` fills the coordinate in.
    """

    segment_id: str
    city: str
    coord: Any = None
    days: int = 1
    explicit_days: bool = False
    lodging_area: str | None = None
    notes: list[str] = Field(default_factory=list)


class SegmentParse(BaseModel):
    segments: list[SegmentSpec] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def explicit_total(self) -> int:
        return sum(s.days or 0 for s in self.segments if s.explicit_days)

    @property
    def has_unknown_days(self) -> bool:
        return any(not s.explicit_days for s in self.segments)


def cn_to_int(text: str) -> int | None:
    """``"3"`` -> 3, ``"三"`` -> 3, ``"十二"`` -> 12, ``"两"`` -> 2."""

    raw = (text or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    if "十" in raw or "拾" in raw:
        head, _, tail = re.sub(r"[拾]", "十", raw).partition("十")
        tens = _CN_DIGITS.get(head, 1) if head else 1
        ones = _CN_DIGITS.get(tail, 0) if tail else 0
        return tens * 10 + ones
    total = 0
    for char in raw:
        if char not in _CN_DIGITS:
            return None
        total = total * 10 + _CN_DIGITS[char]
    return total or None


def _clean_city(raw: str) -> str:
    """Strip connective filler and non-place noise from either end."""

    text = (raw or "").strip(" \t　的和与及、,，。.·-—~|0123456789")
    changed = True
    while changed and text:
        changed = False
        for filler in _FILLER:
            if text.startswith(filler) and len(text) > len(filler):
                text = text[len(filler) :].strip()
                changed = True
            if text.endswith(filler) and len(text) > len(filler):
                text = text[: -len(filler)].strip()
                changed = True
        for verb in _VERB_FILLERS:
            if text.endswith(verb) and len(text) > len(verb):
                text = text[: -len(verb)].strip()
                changed = True
        for stop in _NAME_STOPWORDS:
            if text.endswith(stop) and len(text) > len(stop):
                text = text[: -len(stop)].strip()
                changed = True
    return text.strip(" \t　的和与及、,，。.·-—~|0123456789")


def _is_place(text: str) -> bool:
    """Reject connectives, bare counts and empty strings as place names."""

    candidate = text.strip()
    if not candidate:
        return False
    if candidate in _CONNECTIVES:
        return False
    if re.fullmatch(r"[\d\s]+", candidate):
        return False
    return True


_PUNCT_CLASS = "，,。、；;：:！!？?（）()【】[]《》 \t　|+~—"
#: Same characters, escaped for use inside a character class.
_SPLIT_RE = re.compile("[" + re.escape(_PUNCT_CLASS) + "]+")
#: A trailing duration, digits or Chinese numerals ("两天", "3天").
_DURATION_TAIL_RE = re.compile(
    r"(?:\d{1,2}|[零〇一壹二两贰三叁四肆五伍六陆七柒八捌九玖十拾]{1,3})\s*(?:个)?\s*[" + _UNIT_CLASS + r"]$"
)

#: Any "<n><day-word>", used to locate durations positionally rather than
#: through one clever regex. Two orders ("北京3天" / "3天北京") and two numeral
#: systems are then handled by the same walk, which is far easier to reason
#: about -- and to test -- than a pattern that has to encode all four cases.
_DURATION_RE = re.compile(
    r"(\d{1,2}|[零〇一壹二两贰三叁四肆五伍六陆七柒八捌九玖十拾]{1,3})\s*(?:个)?\s*[" + _UNIT_CLASS + r"]"
)
#: Run of characters that could be a place name.
_TOKEN_RE = re.compile(r"[\u4e00-\u9fa5A-Za-z·．\.]{2,12}")
#: Country / region prefixes that must not be trimmed off a longer name
#: ("日本东京" must not become "本东京").
_PLACE_PREFIXES = ("日本", "韩国", "泰国", "中国", "越南", "美国", "欧洲", "英国", "法国")

#: Segment-only tokens that are never a destination, used to drop the noise that
#: prose leaves behind ("赏樱之旅", "休闲游").
_DROP_SEGMENTS = frozenset(
    {
        "之旅", "赏樱之旅", "自由行", "休闲游", "亲子游", "蜜月", "商务",
        "预算", "左右", "以内", "出发", "往返", "单程", "游玩", "行程",
    }
)
_DURATION_ONLY_RE = re.compile(
    r"^(?:\d{1,2}|[零〇一壹二两贰三叁四肆五伍六陆七柒八捌九玖十拾]{1,3})\s*(?:个)?\s*[" + _UNIT_CLASS + r"]$"
)


def _trim_token(text: str, *, leading: bool) -> str:
    """Cut a candidate place name down to its city, from the given end."""

    body = text.strip(_PUNCT_CLASS)
    # A name that already starts with a country/region prefix is complete.
    if any(body.startswith(prefix) and len(body) > len(prefix) for prefix in _PLACE_PREFIXES):
        leading = False
    # "从北京出发去天津" names the *destination*: keep what follows 出发.
    if "出发" in body:
        before, _, after = body.partition("出发")
        keep = after if (leading and after.strip(_PUNCT_CLASS)) else before
        if keep.strip(_PUNCT_CLASS):
            body = keep
    if leading:
        head = re.match(r"^[\u4e00-\u9fa5]{0,4}?(?:从|到|去|回|在|玩|待|呆|住|逛|宿|游)", body)
        if head and len(body) > head.end():
            body = body[head.end() :]
        else:
            # Once the digits have been moved behind the unit ("3天北京" ->
            # "天3北京"), a leading unit or numeral is leaked duration. Never
            # strip a unit when it is the first character of a real name such as
            # 天津, which is why the guard is "second character is not 津".
            if (
                body
                and body[0] in _UNIT_CLASS
                and len(body) >= 2
                and body[1] != "津"
                and len(body) > 2
            ):
                body = body[1:]
            # Only drop a leading numeral when it is not part of a real name
            # like 三亚 -- the name starts at index 1 and is not itself a number.
            if len(body) > 2 and body[0] in _CN_DIGITS and body[1] not in _CN_DIGITS:
                body = body[1:]
            if len(body) > 4:
                body = body[3:]
    else:
        body = re.split(r"(?:开始|结束|往返|行程|预算|左右|以内)", body)[0]
        for tail in ("之旅", "自由行", "休闲游", "亲子游", "蜜月", "商务", "双人", "出发"):
            if body.endswith(tail) and len(body) > len(tail):
                body = body[: -len(tail)]
    body = body.strip(_PUNCT_CLASS)
    # Verb and duration words can survive once the day count has been removed:
    # "北京玩三天" -> "北京玩三" -> "北京", "待两天" -> "".
    for _ in range(4):
        if len(body) > 2 and body[-1] in _UNIT_CLASS:
            body = body[:-1]
        if len(body) > 2 and body[-1] in _CN_DIGITS:
            body = body[:-1]
        if len(body) > 2 and _DURATION_TAIL_RE.search(body):
            body = _DURATION_TAIL_RE.sub("", body)
        for filler in _FILLER + _VERB_FILLERS:
            if body.startswith(filler) and len(body) > len(filler):
                body = body[len(filler) :]
            if body.endswith(filler) and len(body) > len(filler):
                body = body[: -len(filler)]
        body = body.strip(_PUNCT_CLASS)
    return body


def _part_city(part: str) -> tuple[str, int | None]:
    """Extract ``(city, days)`` from one clause.

    Works clause by clause rather than over the whole request, which removes the
    need to guess which duration belongs to which place: a clause names at most
    one place and at most one duration.
    """

    chunk = part.strip(_PUNCT_CLASS)
    if not chunk:
        return "", None
    # A clause that is *only* a duration carries no place name.
    if _DURATION_ONLY_RE.match(chunk):
        found = _DURATION_RE.search(chunk)
        return "", cn_to_int(found.group(1)) if found else None

    # "第二天：北京3天" -- labels are not part of the place name.
    if ":" in chunk or "：" in chunk:
        chunk = re.split(r"[:：]", chunk)[-1].strip()

    # A bare "<verb><n>天" needs the verb removed before order detection, or the
    # verb would be read as the place name.
    duration = _BARE_DAYS_RE.search(chunk)
    if duration:
        chunk = f"{chunk[: duration.start()]}{chunk[duration.end() :]}"
        days = cn_to_int(duration.group(1))
    else:
        found = _DURATION_RE.search(chunk)
        days = cn_to_int(found.group(1)) if found else None
        if found:
            chunk = chunk[: found.start()] + chunk[found.end() :]

    names = [
        _trim_token(run.group(0), leading=True)
        for run in _TOKEN_RE.finditer(chunk)
    ]
    names = [name for name in names if _is_place(name)]
    if not names:
        return "", days
    joined = "".join(names)
    # "丽江大理" is one stay, not two.
    city = joined if len(joined) <= 12 else max(names, key=len)
    return city, days


def _extract(text: str) -> list[tuple[str, int | None]]:
    """Ordered ``(city, days)`` extraction.

    Clause-by-clause: connectives and punctuation are the segment separators, so
    "北京3天然后天津2天" and "北京3天+天津2天" take the same path, and so does
    "3天北京，2天天津" -- the trailing number is dropped wherever it sits.
    """

    tokens: list[tuple[str, int | None]] = []
    for part in _split_parts(text):
        city, days = _part_city(part)
        if not city and days is not None and tokens and tokens[-1][1] is None:
            # "想去日本东京，5天，2人" -- punctuation separated the duration from
            # the city it belongs to. Give it back rather than inventing a
            # destination called "5天".
            tokens[-1] = (tokens[-1][0], days)
            continue
        if city:
            tokens.append((city, days))
    return tokens


def _split_parts(text: str) -> list[str]:
    """Split on connectives and punctuation, keeping the order."""

    body = text or ""
    for sep in _SEPARATORS:
        body = body.replace(sep, "|")
    return [part for part in _SPLIT_RE.split(body) if part.strip(_PUNCT_CLASS)]


def _fallback_extract(text: str) -> list[tuple[str, int | None]]:
    """City names without any duration: "北京，天津"."""

    tokens: list[tuple[str, int | None]] = []
    for part in _split_parts(text):
        chunk = _COUNT_RE.sub("", part)
        bare = _BARE_DAYS_RE.search(chunk)
        chunk = _BARE_DAYS_RE.sub("", chunk)
        names = [
            _trim_token(run.group(0), leading=True)
            for run in _TOKEN_RE.finditer(chunk)
        ]
        names = [name for name in names if _is_place(name)]
        if not names:
            continue
        joined = "".join(names)
        city = joined if len(joined) <= 12 else max(names, key=len)
        tokens.append((city, cn_to_int(bare.group(1)) if bare else None))
    return tokens


def parse_segments(text: str) -> SegmentParse:
    """Parse an ordered, multi-city request.

    Understands the shapes people actually type::

        北京3天+天津2天
        北京玩三天，然后去天津待两天
        3天北京，2天天津
        北京，天津
        北京3天然后天津2天

    Anything it cannot be sure about is left as ``days=None`` (or the whole text
    as a single segment) and reported through ``notes`` -- never guessed.
    """

    raw = (text or "").strip()
    result = SegmentParse()
    if not raw:
        return result

    # The connective *is* the separator ("北京3天然后天津2天"), which is why the
    # split happens before anything is stripped as filler.
    pairs = _extract(raw)
    tokens = pairs if pairs else _fallback_extract(raw)
    specs = [
        SegmentSpec(city=city, days=days, explicit_days=days is not None)
        for city, days in tokens
        if _is_place(city)
    ]

    # Prose leaves day-less noise behind ("赏樱之旅", "休闲游"). Drop it, but only
    # when something with real information survives -- never empty the plan.
    if any(spec.days is not None for spec in specs):
        specs = [
            spec
            for spec in specs
            if spec.days is not None or spec.city not in _DROP_SEGMENTS
        ]

    # Collapse a duplicate produced by a trailing "最后回北京" without days.
    deduped: list[SegmentSpec] = []
    for spec in specs:
        if deduped and deduped[-1].city == spec.city and spec.days is None:
            continue
        deduped.append(spec)

    if len(deduped) > MAX_SEGMENTS:
        result.notes.append(
            f"识别到 {len(deduped)} 个目的地，超过上限 {MAX_SEGMENTS}，只保留前 {MAX_SEGMENTS} 个"
        )
        deduped = deduped[:MAX_SEGMENTS]

    if not deduped:
        # Nothing looked like a place name. Do not invent one: hand the raw text
        # back as a single segment and let geocoding decide.
        deduped = [SegmentSpec(city=raw)]
        result.notes.append("未能识别多目的地，按单一目的地处理")

    result.segments = deduped
    return result

def complete_days(
    segments: list[SegmentSpec],
    activity_days: int,
    *,
    notes: list[str] | None = None,
) -> list[SegmentSpec]:
    """Fill in missing day counts so the total equals ``activity_days``.

    Rules, in order:
      1. explicit counts win; the remainder is spread over the segments that
         did not state a count;
      2. if nothing was stated, every segment gets at least one day and the
         remainder goes to the earliest segments first;
      3. the total is always exactly ``activity_days`` (never more), and any
         adjustment is appended to ``notes``.
    """

    sink = notes if notes is not None else []
    if not segments:
        return segments

    filled = [spec.model_copy() for spec in segments]
    explicit_total = sum(s.days for s in filled if s.explicit_days and s.days)
    unknown = [s for s in filled if not (s.explicit_days and s.days)]

    if not unknown:
        # Every destination stated a count, so the list is the constraint and
        # whatever was requested beyond it is trimmed proportionally.
        return _scale(filled, activity_days)

    if not explicit_total:
        base, extra = divmod(activity_days, len(filled))
        if base == 0:
            # Fewer days than cities: keep the first N cities and say so.
            sink.append(f"可安排天数不足，只保留前 {activity_days} 个目的地")
            return [
                spec.model_copy(update={"days": 1, "explicit_days": False})
                for spec in filled[:activity_days]
            ]
        for index, spec in enumerate(filled):
            spec.days = base + (1 if index < extra else 0)
        return filled

    if activity_days < explicit_total:
        sink.append(
            f"可安排天数 {activity_days} 天少于已指定的 {explicit_total} 天，已优先保证前序目的地"
        )
        return _scale(filled, activity_days)

    remainder = activity_days - explicit_total
    base, extra = divmod(remainder, len(unknown))
    for index, spec in enumerate(unknown):
        spec.days = base + (1 if index < extra else 0)
        if spec.days == 0:
            sink.append(f"「{spec.city}」未能分到天数，已改为当日往返")
            spec.days = 0
    return filled


def _scale(segments: list[SegmentSpec], target: int) -> list[SegmentSpec]:
    """Trim or grow counts proportionally, keeping order and reporting drops.

    Returns the same number of specs as it was given, in the same order, with a
    ``days`` count of 0 for anything that no longer fits. Order is part of the
    answer for a multi-city trip, so it must never be silently reshuffled.
    """

    count = len(segments)
    weights = [max(1, spec.days or 1) for spec in segments]
    total_weight = sum(weights)
    floor = max(1, target // count) if target >= count else 0

    scaled = [floor] * count
    index = 0
    while sum(scaled) < target:
        # Largest-remainder style: hand the extra days to the segments with the
        # biggest share, cycling so they are spread rather than stacked.
        scaled[index % count] += 1
        index += 1
    while sum(scaled) > target:
        position = max(range(count), key=lambda i: scaled[i])
        scaled[position] -= 1

    if target >= count:
        # Honour the requested proportions for whatever is left over.
        fitted = [floor + (1 if weight * target / total_weight > floor else 0) for weight in weights]
        if sum(fitted) == target:
            scaled = [max(1, item) for item in fitted]

    return [
        spec.model_copy(update={"days": max(0, day_count)})
        for spec, day_count in zip(segments, scaled)
    ]


def allocate_days(
    segments: list[SegmentSpec],
    trip_days: int,
    *,
    notes: list[str] | None = None,
) -> tuple[list[TripSegmentSpec], int]:
    """Split a trip into city stays plus the transfer days between them.

    Two readings of the same sentence are possible and they are **not**
    interchangeable::

        北京3天 + 天津2天

    * if the trip is 5 days, the user described *calendar* days and the transfer
      day is one of them -- rescaling would silently delete a day they asked
      for, so the counts are taken literally and one of those days doubles as the
      transfer day (``transfer_days=0``);
    * if the trip is 4 days, the counts describe *activity* days and one day must
      be spent moving between the cities (``transfer_days=1``).

    Returns ``(segments_with_days, transfer_days)``.
    """

    sink = notes if notes is not None else []
    if not segments or trip_days <= 0:
        return [], 0

    hops = max(0, len(segments) - 1)
    explicit_total = sum(spec.days or 0 for spec in segments if spec.explicit_days)

    if explicit_total and explicit_total == trip_days:
        # The counts add up to the whole trip: they are calendar days, and one of
        # them doubles as the transfer day. Re-scaling would delete a day the
        # user explicitly asked for.
        completed = [spec.model_copy() for spec in segments]
        transfer_days = 0
    else:
        if explicit_total and explicit_total > trip_days:
            sink.append(
                f"各目的地天数合计 {explicit_total} 天，超过可安排天数 {trip_days} 天，"
                "已优先保证前序目的地"
            )
        # Always leave at least one day of actual content: an itinerary that is
        # nothing but a train ride is not what the user asked for, and a plan
        # with zero days would also be impossible to preview.
        transfer_days = min(hops, max(0, trip_days - 1))
        if transfer_days < hops:
            sink.append("天数不足，跨城当日往返，未单独预留移动日")
        completed = complete_days(segments, max(0, trip_days - transfer_days), notes=sink)

    dropped = [spec.city for spec in completed if int(spec.days or 0) <= 0]
    completed = [spec for spec in completed if int(spec.days or 0) > 0]
    if not completed:
        sink.append("按天数无法形成任何目的地分段")
        return [], transfer_days
    if dropped:
        sink.append(f"天数不足，已略过：{'、'.join(dict.fromkeys(dropped))}")

    planned: list[TripSegmentSpec] = []
    for spec in completed:
        planned.append(
            TripSegmentSpec(
                segment_id=new_id("seg"),
                city=spec.city,
                days=int(spec.days or 0),
                lodging_area=spec.lodging_area,
                explicit_days=spec.explicit_days,
            )
        )
    return planned, transfer_days
