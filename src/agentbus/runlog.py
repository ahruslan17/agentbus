"""Parse OpenCode JSON event lines from `run.log` into rows fit for a feed.

Each line of an OpenCode JSON log is one event. The dashboard only wants the events a
human can read: tool calls, finished assistant text, finished steps and errors. Anything
else (step starts, unknown types, broken JSON) is skipped or shown as a raw line.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

SUMMARY_LIMIT = 200
TEXT_LIMIT = 500

FILE_TOOLS = ("read", "write", "edit", "patch", "multiedit")


def parse_lines(lines: Iterable[str], start: int = 0) -> list[dict[str, Any]]:
    """Turn raw log lines into feed rows, skipping what a human need not see.

    `start` is the number of lines already behind the cursor: the first line of `lines`
    gets `line = start + 1`. That keeps numbering stable when the API reads a tail.
    """
    rows: list[dict[str, Any]] = []
    for offset, raw in enumerate(lines):
        row = _parse_line(raw, start + offset + 1)
        if row is not None:
            rows.append(row)
    return rows


def _parse_line(raw: str, number: int) -> dict[str, Any] | None:
    event = _decode(raw)
    if not isinstance(event, dict):
        return _row(number, "raw", summary=_shorten(str(raw).strip()), detail=None)

    kind = event.get("type")
    if kind == "step_start":
        return None
    if kind == "text":
        return _text_row(event, number)
    if kind == "tool_use":
        return _tool_row(event, number)
    if kind == "step_finish":
        return _step_row(event, number)
    if kind == "error":
        return _row(number, "error", summary=_error_summary(event), detail=event)
    return _row(number, "raw", summary=_shorten(str(raw).strip()), detail=event)


def _decode(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _row(
    number: int,
    kind: str,
    *,
    ts: float | None = None,
    tool: str | None = None,
    status: str | None = None,
    summary: str | None = None,
    detail: Any = None,
) -> dict[str, Any]:
    return {
        "line": number,
        "ts": ts,
        "type": kind,
        "tool": tool,
        "status": status,
        "summary": summary,
        "detail": detail,
    }


def _timestamp(event: dict[str, Any]) -> float | None:
    value = event.get("timestamp")
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _part(event: dict[str, Any]) -> dict[str, Any]:
    part = event.get("part")
    return part if isinstance(part, dict) else {}


def _text_row(event: dict[str, Any], number: int) -> dict[str, Any] | None:
    part = _part(event)
    if not part.get("text") or not (part.get("time") or {}).get("end"):
        return None  # unfinished text: the agent is still writing it
    return _row(
        number,
        "text",
        ts=_timestamp(event),
        summary=str(part["text"])[:TEXT_LIMIT],
        detail=event,
    )


def _tool_row(event: dict[str, Any], number: int) -> dict[str, Any]:
    part = _part(event)
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    tool = str(part.get("tool") or "tool")
    return _row(
        number,
        "tool",
        ts=_timestamp(event),
        tool=tool,
        status=state.get("status"),
        summary=_tool_summary(tool, state.get("input")),
        detail=event,
    )


def _step_row(event: dict[str, Any], number: int) -> dict[str, Any]:
    part = _part(event)
    tokens = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    cached = _number(cache.get("read")) + _number(cache.get("write"))
    summary = f"tokens {_number(tokens.get('input'))}/{_number(tokens.get('output'))}/{cached}"
    return _row(number, "step", ts=_timestamp(event), summary=summary, detail=event)


def _error_summary(event: dict[str, Any]) -> str | None:
    error = event.get("error")
    if isinstance(error, dict):
        for key in ("message", "name"):
            if error.get(key):
                return _shorten(str(error[key]))
    if error is not None:
        return _shorten(str(error))
    return None


def _tool_summary(tool: str, payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    keys: tuple[str, ...] = ()
    if tool == "shell":
        keys = ("command",)
    elif tool in FILE_TOOLS:
        keys = ("filePath", "path", "file")
    for key in keys:
        if payload.get(key) is not None:
            return _shorten(str(payload[key]))
    for value in payload.values():
        if isinstance(value, str) and value:
            return _shorten(value)
    return None


def _shorten(text: str) -> str:
    return text[:SUMMARY_LIMIT]


def _number(value: Any) -> int | float:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0
