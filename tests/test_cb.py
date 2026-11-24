"""`cb` from a VM: mailbox selection, send receipts, wait, tickets and verdicts.

Everything runs through subprocess against `bin/cb` with a temporary share root, so the
tests need neither the API nor Postgres.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

CB = Path(__file__).resolve().parents[1] / "bin" / "cb"
MAILBOX_DIRS = ("outbox", "inbox", "sent", "read")


def run_cb(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    clean = {key: value for key, value in os.environ.items() if not key.startswith("AGENTBUS_")}
    clean.update(env or {})
    return subprocess.run(
        [sys.executable, str(CB), *args],
        env=clean,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def make_mailbox(path: Path) -> Path:
    for name in MAILBOX_DIRS:
        (path / name).mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def write_message(mailbox: Path, folder: str, message: dict[str, Any]) -> Path:
    message = {"sender": "local-opencode", "thread_id": "t_1", "body": "", **message}
    return write_json(mailbox / folder / str(message["id"]) / "message.json", message)


def write_receipt(mailbox: Path, stem: str, receipt: dict[str, Any]) -> Path:
    return write_json(mailbox / "sent" / f"{stem}.receipt.json", receipt)


def find_outbox_file(mailbox: Path, deadline: float = 10.0) -> Path | None:
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        files = [
            path for path in (mailbox / "outbox").glob("*.json") if not path.name.startswith(".")
        ]
        if files:
            return max(files, key=lambda path: path.stat().st_mtime)
        time.sleep(0.05)
    return None


def receipt_writer(mailbox: Path, *, delay: float, receipt: dict[str, Any]) -> threading.Thread:
    def worker() -> None:
        queued = find_outbox_file(mailbox)
        if queued is None:
            return
        time.sleep(delay)
        write_receipt(mailbox, queued.stem, receipt)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread


def failure_writer(mailbox: Path, *, delay: float, error: str) -> threading.Thread:
    def worker() -> None:
        queued = find_outbox_file(mailbox)
        if queued is None:
            return
        time.sleep(delay)
        failed = mailbox / "failed" / queued.name
        write_json(failed, {"rejected": True})
        failed.with_suffix(".error").write_text(error + "\n", encoding="utf-8")

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread


def answer_writer(mailbox: Path, *, delay: float, message: dict[str, Any]) -> threading.Thread:
    def worker() -> None:
        time.sleep(delay)
        write_message(mailbox, "inbox", message)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread


# --- 1. mailbox selection ---------------------------------------------------


def test_share_env_wins_over_everything(tmp_path: Path) -> None:
    box = make_mailbox(tmp_path / "explicit" / "box")
    root = tmp_path / "root"
    make_mailbox(root / "alpha")

    result = run_cb(
        ["env"],
        env={
            "AGENTBUS_SHARE": str(box),
            "AGENTBUS_SHARE_ROOT": str(root),
            "AGENTBUS_MAILBOX": "alpha",
        },
    )

    assert result.returncode == 0, result.stderr
    printed = json.loads(result.stdout)
    assert printed["share"] == str(box)
    assert printed["mailbox"] == "box"
    assert printed["source"] == "AGENTBUS_SHARE"


def test_mailbox_env_uses_share_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    box = make_mailbox(root / "alpha")

    result = run_cb(["env"], env={"AGENTBUS_SHARE_ROOT": str(root), "AGENTBUS_MAILBOX": "alpha"})

    assert result.returncode == 0, result.stderr
    printed = json.loads(result.stdout)
    assert printed["share"] == str(box)
    assert printed["mailbox"] == "alpha"
    assert printed["source"] == "AGENTBUS_MAILBOX"


def test_git_repository_name_becomes_mailbox(tmp_path: Path) -> None:
    repo = tmp_path / "My_Repo.v2"
    (repo / ".git").mkdir(parents=True)
    workdir = repo / "src" / "deep"
    workdir.mkdir(parents=True)
    root = tmp_path / "root"
    box = make_mailbox(root / "claude-my-repo-v2")

    result = run_cb(["env"], env={"AGENTBUS_SHARE_ROOT": str(root)}, cwd=workdir)

    assert result.returncode == 0, result.stderr
    printed = json.loads(result.stdout)
    assert printed["share"] == str(box)
    assert printed["mailbox"] == "claude-my-repo-v2"
    assert printed["source"] == "git"


def test_default_mailbox_outside_git(tmp_path: Path) -> None:
    root = tmp_path / "root"
    box = make_mailbox(root / "cage-claude")
    workdir = tmp_path / "plain"
    workdir.mkdir()

    result = run_cb(["env"], env={"AGENTBUS_SHARE_ROOT": str(root)}, cwd=workdir)

    assert result.returncode == 0, result.stderr
    printed = json.loads(result.stdout)
    assert printed["share"] == str(box)
    assert printed["mailbox"] == "cage-claude"
    assert printed["source"] == "default"


@pytest.mark.parametrize("name", ["Bad_Name", "x", "with space"])
def test_invalid_mailbox_name_is_an_error(tmp_path: Path, name: str) -> None:
    result = run_cb(
        ["env"],
        env={"AGENTBUS_SHARE_ROOT": str(tmp_path / "root"), "AGENTBUS_MAILBOX": name},
    )

    assert result.returncode != 0
    assert "invalid mailbox name" in result.stderr


def test_init_creates_the_mailbox(tmp_path: Path) -> None:
    root = tmp_path / "root"

    result = run_cb(["init", "beta"], env={"AGENTBUS_SHARE_ROOT": str(root)})

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(root / "beta")
    for name in MAILBOX_DIRS:
        assert (root / "beta" / name).is_dir()


# --- 2. id of the sent message ----------------------------------------------


def test_send_prints_id_from_receipt(tmp_path: Path) -> None:
    box = make_mailbox(tmp_path / "box")
    writer = receipt_writer(
        box,
        delay=1.0,
        receipt={
            "file": "queued.json",
            "id": "m_sent123",
            "thread_id": "t_sent123",
            "kind": "task",
            "recipient": "local-opencode",
        },
    )

    result = run_cb(
        ["send", "--to", "local-opencode", "--wait-id", "10", "сделай X"],
        env={"AGENTBUS_SHARE": str(box)},
    )
    writer.join(timeout=15)

    assert result.returncode == 0, result.stderr
    printed = json.loads(result.stdout)
    assert printed["id"] == "m_sent123"
    assert printed["thread_id"] == "t_sent123"
    assert printed["recipient"] == "local-opencode"


def test_send_without_receipt_warns_but_succeeds(tmp_path: Path) -> None:
    box = make_mailbox(tmp_path / "box")

    result = run_cb(
        ["send", "--to", "local-opencode", "--wait-id", "1", "сделай X"],
        env={"AGENTBUS_SHARE": str(box)},
    )

    assert result.returncode == 0
    assert "no receipt" in result.stderr
    printed = json.loads(result.stdout)
    assert printed["queued_file"]
    assert "id" not in printed


def test_send_reports_a_failed_file(tmp_path: Path) -> None:
    box = make_mailbox(tmp_path / "box")
    writer = failure_writer(box, delay=1.0, error="422: unknown recipient local-opencode")

    result = run_cb(
        ["send", "--to", "local-opencode", "--wait-id", "10", "сделай X"],
        env={"AGENTBUS_SHARE": str(box)},
    )
    writer.join(timeout=15)

    assert result.returncode == 1
    assert "unknown recipient" in result.stderr


# --- 3. wait ----------------------------------------------------------------


def test_wait_returns_after_the_first_answer(tmp_path: Path) -> None:
    box = make_mailbox(tmp_path / "box")
    answer_writer(
        box,
        delay=1.0,
        message={"id": "m_a1", "parent_id": "m_a", "kind": "result", "meta": {}},
    )

    start = time.monotonic()
    result = run_cb(["wait", "m_a", "m_b", "--timeout", "20"], env={"AGENTBUS_SHARE": str(box)})
    elapsed = time.monotonic() - start

    assert result.returncode == 0, result.stderr
    assert "m_a -> m_a1" in result.stdout
    assert "m_b ->" not in result.stdout
    assert elapsed < 20


def test_wait_all_returns_after_every_answer(tmp_path: Path) -> None:
    box = make_mailbox(tmp_path / "box")
    answer_writer(
        box,
        delay=0.5,
        message={"id": "m_a1", "parent_id": "m_a", "kind": "result", "meta": {}},
    )
    answer_writer(
        box,
        delay=2.5,
        message={"id": "m_b1", "parent_id": "m_b", "kind": "result", "meta": {}},
    )

    result = run_cb(
        ["wait", "m_a", "m_b", "--all", "--timeout", "30"], env={"AGENTBUS_SHARE": str(box)}
    )

    assert result.returncode == 0, result.stderr
    assert "m_a -> m_a1" in result.stdout
    assert "m_b -> m_b1" in result.stdout


def test_wait_times_out_with_code_2(tmp_path: Path) -> None:
    box = make_mailbox(tmp_path / "box")

    result = run_cb(["wait", "m_a", "--timeout", "1"], env={"AGENTBUS_SHARE": str(box)})

    assert result.returncode == 2
    assert result.stdout.strip() == ""


def test_wait_prints_checks_and_timeout(tmp_path: Path) -> None:
    box = make_mailbox(tmp_path / "box")
    write_message(
        box,
        "inbox",
        {
            "id": "m_res",
            "parent_id": "m_task",
            "kind": "result",
            "meta": {"checks": {"ok": False, "exit_code": 1}, "timeout": True},
        },
    )

    result = run_cb(["wait", "m_task", "--timeout", "5"], env={"AGENTBUS_SHARE": str(box)})

    assert result.returncode == 0, result.stderr
    assert "checks=fail" in result.stdout
    assert "timeout=true" in result.stdout


# --- 4. ticket in inbox and show -------------------------------------------


def ticket_chain(mailbox: Path) -> None:
    """task (ticket) -> result -> verdict (inherited) -> result."""
    write_json(
        mailbox / "sent" / "task.json",
        {
            "recipient": "local-opencode",
            "kind": "task",
            "body": "сделай X",
            "meta": {"ticket": "T2.1"},
        },
    )
    write_receipt(
        mailbox,
        "task",
        {"file": "task.json", "id": "m_task", "thread_id": "t_1", "kind": "task"},
    )
    write_message(
        mailbox,
        "inbox",
        {"id": "m_result", "parent_id": "m_task", "kind": "result", "meta": {}},
    )
    write_json(
        mailbox / "sent" / "verdict.json",
        {
            "recipient": "local-opencode",
            "kind": "verdict",
            "body": "правь Y",
            "parent_id": "m_result",
            "meta": {"ticket": "T2.1", "decision": "rework"},
        },
    )
    write_receipt(
        mailbox,
        "verdict",
        {"file": "verdict.json", "id": "m_verdict", "thread_id": "t_1", "kind": "verdict"},
    )
    write_message(
        mailbox,
        "inbox",
        {"id": "m_fix", "parent_id": "m_verdict", "kind": "result", "meta": {}},
    )


def test_inbox_shows_ticket_through_receipts(tmp_path: Path) -> None:
    box = make_mailbox(tmp_path / "box")
    ticket_chain(box)

    result = run_cb(["inbox"], env={"AGENTBUS_SHARE": str(box)})

    assert result.returncode == 0, result.stderr
    assert result.stdout.count("тикет T2.1") == 2
    assert "m_result" in result.stdout
    assert "m_fix" in result.stdout


def test_show_text_prints_ticket(tmp_path: Path) -> None:
    box = make_mailbox(tmp_path / "box")
    ticket_chain(box)

    result = run_cb(["show", "m_fix", "--text"], env={"AGENTBUS_SHARE": str(box)})

    assert result.returncode == 0, result.stderr
    assert "тикет T2.1" in result.stdout


def test_reply_verdict_gets_rework_and_inherits_ticket(tmp_path: Path) -> None:
    box = make_mailbox(tmp_path / "box")
    write_json(
        box / "sent" / "task.json",
        {"recipient": "local-opencode", "kind": "task", "meta": {"ticket": "T2.1"}},
    )
    write_receipt(box, "task", {"file": "task.json", "id": "m_task", "kind": "task"})
    write_message(
        box,
        "inbox",
        {"id": "m_result", "parent_id": "m_task", "kind": "result", "meta": {}},
    )

    result = run_cb(
        ["reply", "m_result", "--kind", "verdict", "--wait-id", "0", "правь Y"],
        env={"AGENTBUS_SHARE": str(box)},
    )

    assert result.returncode == 0, result.stderr
    queued = [path for path in (box / "outbox").glob("*.json") if not path.name.startswith(".")]
    assert len(queued) == 1
    payload = json.loads(queued[0].read_text(encoding="utf-8"))
    assert payload["kind"] == "verdict"
    assert payload["meta"]["decision"] == "rework"
    assert payload["meta"]["ticket"] == "T2.1"
    assert payload["parent_id"] == "m_result"


def test_explicit_decision_is_not_overwritten(tmp_path: Path) -> None:
    box = make_mailbox(tmp_path / "box")
    write_message(
        box,
        "inbox",
        {"id": "m_result", "parent_id": "m_task", "kind": "result", "meta": {}},
    )

    result = run_cb(
        [
            "reply",
            "m_result",
            "--kind",
            "verdict",
            "--wait-id",
            "0",
            "--meta",
            '{"decision": "accept"}',
            "годится",
        ],
        env={"AGENTBUS_SHARE": str(box)},
    )

    assert result.returncode == 0, result.stderr
    queued = [path for path in (box / "outbox").glob("*.json") if not path.name.startswith(".")]
    payload = json.loads(queued[0].read_text(encoding="utf-8"))
    assert payload["meta"]["decision"] == "accept"


def test_accept_writes_decision_and_moves_to_read(tmp_path: Path) -> None:
    box = make_mailbox(tmp_path / "box")
    write_message(
        box,
        "inbox",
        {"id": "m_result", "parent_id": "m_task", "kind": "result", "meta": {}},
    )

    result = run_cb(["accept", "m_result", "--note", "всё ок"], env={"AGENTBUS_SHARE": str(box)})

    assert result.returncode == 0, result.stderr
    assert not (box / "inbox" / "m_result").exists()
    decision = json.loads((box / "read" / "m_result" / "decision.json").read_text(encoding="utf-8"))
    assert decision == {"decision": "accept", "note": "всё ок"}
    assert (box / "read" / "m_result" / "message.json").is_file()
