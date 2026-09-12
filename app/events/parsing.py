"""Tolerant JSON reading for streamed tool-call arguments (framework §10).

The stream carries a tool call in fragments: ``tool_call_start`` /
``tool_call_delta`` / ``tool_call_end``. Framework §4.3.4 splits their duties:

* deltas exist for **display only** -- nothing may be parsed from them, because
  a half-received argument string is indistinguishable from a malformed one;
* arguments are parsed **exactly once**, at ``tool_call_end``.

That rule only holds if the end-of-stream parse is forgiving about the one
thing that legitimately happens in a stream: truncation. This module closes an
unterminated document instead of raising, so a dropped connection mid-argument
degrades into "we know the fields we received" rather than a 500.

Nothing here is allowed to invent data: a fragment that cannot be interpreted
even after completion returns ``None``.
"""

from __future__ import annotations

import json
from typing import Any, Iterator

#: Bound on how many times we may back off to an earlier value boundary before
#: giving up. Bounded so a pathological input cannot turn into quadratic work.
MAX_TRIM = 64

#: Characters that can legitimately end a JSON value token.
_VALUE_ENDINGS = frozenset('}"]elE') | frozenset("0123456789")


def _followed_by_colon(text: str, index: int) -> bool:
    """True when the next meaningful character is ``:``.

    That is what distinguishes an object *key* from a string *value*, and it is
    the only reason we must not treat ``{"a"`` as a recoverable prefix.
    """

    while index < len(text):
        if not text[index].isspace():
            return text[index] == ":"
        index += 1
    return False


def complete_json(text: str) -> str:
    """Append the minimum syntax needed to close a truncated JSON document.

    Handles the three ways a stream truncates: inside a string, between
    elements, and mid-token. When simply closing the document would fabricate a
    member (a key with no value), it instead rewinds to the last **complete
    value** and closes from there -- inventing data is worse than returning
    less of it.
    """

    closed = _close(text)
    if _parses(closed):
        return closed
    cut = _last_value_end(text)
    if cut is None or cut >= len(text):
        return text
    return _close(text[:cut])


def _close(text: str) -> str:
    out: list[str] = []
    closers: list[str] = []
    in_string = False
    escaped = False

    for ch in text:
        out.append(ch)
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            closers.append("}")
        elif ch == "[":
            closers.append("]")
        elif ch in "}]" and closers:
            closers.pop()

    if escaped:
        # A trailing lone backslash would escape our own closing quote.
        out.append("\\")
    if in_string:
        out.append('"')
    while out and out[-1] in ",:":
        out.pop()
        while out and out[-1].isspace():
            out.pop()
    out.extend(reversed(closers))
    return "".join(out)


def _last_value_end(text: str) -> int | None:
    """Index just past the last character that ends an object/array/string value."""

    in_string = False
    escaped = False
    last: int | None = None
    for index, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
                if not _followed_by_colon(text, index + 1):
                    last = index + 1
            continue
        if ch == '"':
            in_string = True
        elif ch in _VALUE_ENDINGS:
            last = index + 1
    return last


def _parses(text: str) -> bool:
    try:
        json.loads(text)
    except (ValueError, TypeError):
        return False
    return True


def _candidates(text: str) -> Iterator[str]:
    yield text
    completed = complete_json(text)
    if completed != text:
        yield completed
        if _parses(completed):
            return
    current = text
    for _ in range(MAX_TRIM):
        cut = _last_value_end(current)
        if cut is None or cut >= len(current):
            return
        current = current[:cut]
        yield current
        completed = complete_json(current)
        if completed == current:
            continue
        yield completed
        if _parses(completed):
            return


def parse_lenient(raw: str | None) -> Any | None:
    """Best-effort parse of a possibly-truncated JSON document.

    Returns ``None`` when even the completed forms are uninterpretable. Callers
    must treat ``None`` as "no arguments", never as "empty arguments".
    """

    text = (raw or "").strip()
    if not text:
        return None
    for candidate in _candidates(text):
        try:
            return json.loads(candidate)
        except (ValueError, TypeError):
            continue
    return None


def parse_complete(raw: str | None) -> Any:
    """Strict counterpart used where a truncated payload is a real error."""

    text = (raw or "").strip()
    if not text:
        raise ValueError("empty argument payload")
    return json.loads(text)


class ToolCallArgumentStream:
    """Accumulate ``tool_call_delta`` fragments without parsing them.

    The buffer exists so the UI can render partial arguments; the single parse
    happens in :meth:`finish`, which is the only method that interprets the
    payload. Keeping those two jobs in one object makes the §4.3.4 rule hard to
    violate by accident at a call site.
    """

    def __init__(self, tool_name: str = "") -> None:
        self.tool_name = tool_name
        self._fragments: list[str] = []

    def feed(self, delta: str | None) -> None:
        if delta:
            self._fragments.append(delta)

    @property
    def display_text(self) -> str:
        return "".join(self._fragments)

    @property
    def is_empty(self) -> bool:
        return not self._fragments

    def finish(self, *, arguments: str | None = None) -> Any | None:
        """Parse once, at the end. ``arguments`` overrides the accumulated text."""

        return parse_lenient(arguments if arguments is not None else self.display_text)


__all__ = [
    "MAX_TRIM",
    "ToolCallArgumentStream",
    "complete_json",
    "parse_complete",
    "parse_lenient",
]
