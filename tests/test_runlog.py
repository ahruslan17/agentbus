"""Parser of OpenCode JSON log lines into dashboard feed rows."""

from __future__ import annotations

import json

from agentbus.runlog import parse_lines


def line(**payload) -> str:
    return json.dumps(payload, ensure_ascii=False)


def test_every_known_event_becomes_a_feed_row() -> None:
    raw_lines = [
        line(type="step_start", timestamp=1000, part={"id": "p0"}),
        line(
            type="text",
            timestamp=1001,
            part={"text": "done", "time": {"start": 1000, "end": 1001}},
        ),
        line(
            type="text",
            timestamp=1002,
            part={"text": "still writing", "time": {"start": 1002}},
        ),
        line(
            type="tool_use",
            timestamp=1003,
            part={
                "tool": "shell",
                "state": {"status": "completed", "input": {"command": "make check"}},
            },
        ),
        line(
            type="tool_use",
            timestamp=1004,
            part={
                "tool": "edit",
                "state": {"status": "running", "input": {"filePath": "src/x.py"}},
            },
        ),
        line(
            type="tool_use",
            timestamp=1005,
            part={"tool": "custom", "state": {"status": "completed", "input": {"query": "abc"}}},
        ),
        line(
            type="step_finish",
            timestamp=1006,
            part={
                "tokens": {"input": 10, "output": 20, "cache": {"read": 3, "write": 2}},
                "cost": 0.01,
            },
        ),
        line(type="error", timestamp=1007, error={"name": "ProviderError", "message": "boom"}),
        "plain text",
        '{"type": "text", "part": ',
    ]

    rows = parse_lines(raw_lines)

    assert [row["type"] for row in rows] == [
        "text",
        "tool",
        "tool",
        "tool",
        "step",
        "error",
        "raw",
        "raw",
    ]
    assert [row["line"] for row in rows] == [2, 4, 5, 6, 7, 8, 9, 10]

    text = rows[0]
    assert text["summary"] == "done"
    assert text["ts"] == 1001
    assert text["detail"]["part"]["text"] == "done"

    shell, edit, custom = rows[1], rows[2], rows[3]
    assert (shell["tool"], shell["status"], shell["summary"]) == (
        "shell",
        "completed",
        "make check",
    )
    assert (edit["tool"], edit["status"], edit["summary"]) == ("edit", "running", "src/x.py")
    assert custom["summary"] == "abc"

    step = rows[4]
    assert step["summary"] == "tokens 10/20/5"
    assert step["detail"]["part"]["tokens"]["output"] == 20

    error = rows[5]
    assert error["summary"] == "boom"

    assert rows[6]["detail"] is None
    assert rows[6]["summary"] == "plain text"
    assert rows[7]["detail"] is None


def test_summary_is_truncated() -> None:
    long_text = "x" * 900
    rows = parse_lines(
        [
            line(
                type="tool_use",
                part={"tool": "shell", "state": {"input": {"command": "y" * 900}}},
            ),
            line(type="text", part={"text": long_text, "time": {"end": 1}}),
        ]
    )
    assert len(rows[0]["summary"]) == 200
    assert len(rows[1]["summary"]) == 500


def test_start_shifts_line_numbers() -> None:
    rows = parse_lines(["one", "two"], start=10)
    assert [row["line"] for row in rows] == [11, 12]
    assert rows[0]["type"] == "raw"


def test_parser_survives_anything() -> None:
    assert parse_lines([]) == []
    assert parse_lines(["", "null", "[]", "{}", "\x00"]) != []
