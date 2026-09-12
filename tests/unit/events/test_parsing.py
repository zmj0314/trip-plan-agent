"""Framework §12, stream protocol row: a truncated payload must not crash us.

The contract under test (framework §4.3.4) is narrow on purpose: deltas are
display-only, and arguments are interpreted exactly once, at the end. These
tests pin the *result* of that single interpretation.
"""

from __future__ import annotations

import pytest

from app.events.parsing import (
    ToolCallArgumentStream,
    complete_json,
    parse_complete,
    parse_lenient,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"origin": "望京SOHO", "travelers": 1}', {"origin": "望京SOHO", "travelers": 1}),
        ('{"origin": "北京市朝', {"origin": "北京市朝"}),
        ('{"a": 1, "b":', {"a": 1}),
        ("[1, 2", [1, 2]),
        ("[1, 2,", [1, 2]),
        ('{"a": 1,', {"a": 1}),
        ('{"a": [1, {"b": 2', {"a": [1, {"b": 2}]}),
        ("", None),
        ("   ", None),
        ("not json at all", None),
        ('{"a": "unterminated \\', {"a": "unterminated \\"}),
    ],
)
def test_truncated_payloads_still_parse(raw: str, expected):
    assert parse_lenient(raw) == expected


def test_complete_json_leaves_valid_input_untouched():
    valid = '{"a": [1, 2], "b": {"c": "d"}}'
    assert complete_json(valid) == valid


def test_completion_never_invents_a_member():
    """Closing `{"a": 1, "b":` must not fabricate an empty key."""

    assert parse_lenient('{"a": 1, "b":') == {"a": 1}
    assert parse_lenient('{"a": 1, "b": "x') == {"a": 1, "b": "x"}


def test_strict_parser_is_the_counterpart():
    """Where truncation IS an error, the strict form must still raise."""

    assert parse_complete('{"a": 1}') == {"a": 1}
    with pytest.raises(ValueError):
        parse_complete('{"a": 1,')
    with pytest.raises(ValueError):
        parse_complete("")


def test_argument_stream_parses_only_at_the_end():
    stream = ToolCallArgumentStream("booking.deeplink.build")
    expected = ""
    for chunk in ['{"train', '_no": "G', "1234", '", "date": "2026-09-12"}']:
        stream.feed(chunk)
        expected += chunk
        # Deltas are display-only: the buffer stays literal until ``finish``.
        assert stream.display_text == expected

    assert stream.finish() == {"train_no": "G1234", "date": "2026-09-12"}
    assert not stream.is_empty


def test_argument_stream_tolerates_a_dropped_connection():
    stream = ToolCallArgumentStream("booking.checklist.export")
    stream.feed('{"session": "ses_1", "items": [1, 2')

    assert stream.finish() == {"session": "ses_1", "items": [1, 2]}


def test_argument_stream_accepts_an_explicit_final_payload():
    stream = ToolCallArgumentStream("x")
    stream.feed("{")
    assert stream.finish(arguments="{}") == {}
