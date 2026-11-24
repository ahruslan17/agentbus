"""Executor slots: parallelism, thread safety, registry and graceful stop."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from agentbus.config import WorkerSettings
from agentbus.worker import Client, repo_lock, run_slots

from .conftest import TOKEN

FAKE_OPENCODE = """#!{python}
import json
import os
import sys
import time
from pathlib import Path

journal = Path(os.environ["FAKE_JOURNAL"])
cwd = Path.cwd()
with journal.open("a", encoding="utf-8") as sink:
    sink.write(f"start {{time.time()}} {{cwd}}\\n")
    sink.flush()

time.sleep(float(os.environ.get("FAKE_SLEEP", "0.5")))
(cwd / "FAKE_OUTPUT").write_text("ok", encoding="utf-8")

print(json.dumps({{"type": "text", "part": {{"time": {{"end": time.time()}}, "text": "готово"}}}}))
print(json.dumps({{"type": "step_finish", "part": {{"cost": 0.0, "tokens": {{"input": 1, "output": 2}}}}}}))
print("stdout:", " ".join(sys.argv[1:]))

with journal.open("a", encoding="utf-8") as sink:
    sink.write(f"end {{time.time()}}\\n")
    sink.flush()
"""


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    (path / "README.md").write_text("repo\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-qm",
            "init",
        ],
        check=True,
    )
    return path


@pytest.fixture()
def fake_opencode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("FAKE_JOURNAL", str(tmp_path / "journal.txt"))
    monkeypatch.setenv("FAKE_SLEEP", "0.5")
    script = tmp_path / "fake-opencode"
    script.write_text(FAKE_OPENCODE.format(python=sys.executable), encoding="utf-8")
    script.chmod(0o755)
    return script


def worker_settings(
    tmp_path: Path,
    repo: Path,
    *,
    slots: int = 1,
    timeout: int = 60,
    grace: int = 10,
) -> WorkerSettings:
    return WorkerSettings(
        url="http://testserver",
        token=TOKEN,
        agent="local-opencode",
        model="fake/model",
        repos=(str(tmp_path),),
        state_dir=str(tmp_path / "state"),
        task_timeout_seconds=timeout,
        lease_ttl_seconds=60,
        opencode_bin=str(tmp_path / "fake-opencode"),
        extra_instructions="",
        slots=slots,
        stop_grace_seconds=grace,
        commit_author="agentbus executor <agentbus@localhost>",
    )


def send(client: TestClient, **overrides) -> dict:
    payload = {
        "sender": "cage-claude",
        "recipient": "local-opencode",
        "kind": "task",
        "body": "сделай дело",
    }
    payload.update(overrides)
    response = client.post("/v1/messages", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def wait_state(client: TestClient, message_id: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    row: dict = {}
    while time.monotonic() < deadline:
        row = client.get(f"/v1/messages/{message_id}").json()
        if row.get("state") in ("done", "failed"):
            return row
        time.sleep(0.02)
    raise AssertionError(f"{message_id} did not finish: {row.get('state')}")


def wait_for_result(client: TestClient, message: dict) -> None:
    assert wait_state(client, message["id"])["state"] == "done"
    messages = client.get(f"/v1/threads/{message['thread_id']}").json()["messages"]
    assert any(row["kind"] == "result" and row["parent_id"] == message["id"] for row in messages)


def join_all(threads: list[threading.Thread], timeout: float = 10.0) -> None:
    for thread in threads:
        thread.join(timeout=timeout)
        assert not thread.is_alive(), f"slot {thread.name} did not stop"


def journal_times(tmp_path: Path) -> tuple[list[float], list[float]]:
    starts: list[float] = []
    ends: list[float] = []
    journal = tmp_path / "journal.txt"
    if not journal.exists():
        return starts, ends
    for line in journal.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if parts[0] == "start":
            starts.append(float(parts[1]))
        elif parts[0] == "end":
            ends.append(float(parts[1]))
    return starts, ends


def wait_for_starts(tmp_path: Path, count: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(journal_times(tmp_path)[0]) >= count:
            return
        time.sleep(0.02)
    raise AssertionError(f"journal has fewer than {count} starts")


def test_two_slots_run_two_threads_of_one_repo_in_parallel(
    tmp_path: Path, repo: Path, client: TestClient, fake_opencode: Path
) -> None:
    settings = worker_settings(tmp_path, repo, slots=2)
    stop = threading.Event()
    first = send(client, repo=str(repo), body="первая")
    second = send(client, repo=str(repo), body="вторая")

    threads = run_slots(settings, stop, http=client)
    try:
        wait_for_result(client, first)
        wait_for_result(client, second)
    finally:
        stop.set()
        join_all(threads)

    starts, ends = journal_times(tmp_path)
    assert len(starts) == 2 and len(ends) == 2
    assert max(starts) < min(ends), "the two runs should overlap"

    for message in (first, second):
        worktree = tmp_path / "state" / "worktrees" / message["thread_id"]
        assert (worktree / "FAKE_OUTPUT").exists()


def test_two_slots_do_not_run_two_messages_of_one_thread(
    tmp_path: Path, repo: Path, client: TestClient, fake_opencode: Path
) -> None:
    settings = worker_settings(tmp_path, repo, slots=2)
    stop = threading.Event()
    task = send(client, repo=str(repo), body="работа")
    note = send(client, kind="note", body="заметка", thread_id=task["thread_id"])

    threads = run_slots(settings, stop, http=client)
    try:
        wait_for_result(client, task)
        wait_for_result(client, note)
    finally:
        stop.set()
        join_all(threads)

    starts, ends = journal_times(tmp_path)
    assert len(starts) == 2 and len(ends) == 2
    assert ends[0] <= starts[1], "messages of one thread must not overlap"


def test_one_slot_keeps_the_default_sequential_behaviour(
    tmp_path: Path, repo: Path, client: TestClient, fake_opencode: Path
) -> None:
    settings = worker_settings(tmp_path, repo, slots=1)
    stop = threading.Event()
    first = send(client, repo=str(repo), body="первая")
    second = send(client, repo=str(repo), body="вторая")

    threads = run_slots(settings, stop, http=client)
    try:
        wait_for_result(client, first)
        wait_for_result(client, second)
    finally:
        stop.set()
        join_all(threads)

    starts, ends = journal_times(tmp_path)
    assert len(starts) == 2 and len(ends) == 2
    assert ends[0] <= starts[1], "one slot must not run two tasks at once"


def test_registry_shows_busy_slot_and_stops_both(
    tmp_path: Path, repo: Path, client: TestClient, fake_opencode: Path, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_SLEEP", "1")
    settings = worker_settings(tmp_path, repo, slots=2)
    stop = threading.Event()
    task = send(client, repo=str(repo), body="работа")

    threads = run_slots(settings, stop, http=client)
    try:
        deadline = time.monotonic() + 10
        workers: dict[str, dict] = {}
        while time.monotonic() < deadline:
            workers = {row["id"]: row for row in client.get("/v1/workers").json()["workers"]}
            busy = [row for row in workers.values() if row["state"] == "busy"]
            if {"local-opencode#0", "local-opencode#1"} <= set(workers) and busy:
                break
            time.sleep(0.02)

        assert {"local-opencode#0", "local-opencode#1"} <= set(workers)
        busy = [row for row in workers.values() if row["state"] == "busy"]
        assert len(busy) == 1
        assert busy[0]["message_id"] == task["id"]
    finally:
        stop.set()
        join_all(threads)

    final = client.get("/v1/workers").json()["workers"]
    assert {row["id"] for row in final} == {"local-opencode#0", "local-opencode#1"}
    assert all(row["state"] == "stopped" for row in final)


def test_stop_finishes_the_running_task_and_keeps_new_ones_queued(
    tmp_path: Path, repo: Path, client: TestClient, fake_opencode: Path, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_SLEEP", "0.8")
    settings = worker_settings(tmp_path, repo, slots=1)
    stop = threading.Event()
    running = send(client, repo=str(repo), body="идёт")

    threads = run_slots(settings, stop, http=client)
    try:
        wait_for_starts(tmp_path, 1)
        stop.set()
        late = send(client, repo=str(repo), body="после стопа")
    finally:
        stop.set()
        join_all(threads)

    assert wait_state(client, running["id"])["state"] == "done"
    assert client.get(f"/v1/messages/{late['id']}").json()["state"] == "queued"


def test_lease_returned_while_stopping_goes_back_to_the_queue_at_once(
    tmp_path: Path,
    repo: Path,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = worker_settings(tmp_path, repo)
    stop = threading.Event()
    task = send(client, repo=str(repo), body="работа")
    original_lease = Client.lease

    def lease_then_stop(self, wait, owner=None):
        message = original_lease(self, wait=0, owner=owner)
        stop.set()
        return message

    monkeypatch.setattr(Client, "lease", lease_then_stop)

    threads = run_slots(settings, stop, http=client)
    try:
        join_all(threads)
    finally:
        stop.set()
        join_all(threads)

    # the slot released the lease itself: no TTL (60 s here) had to expire
    row = client.get(f"/v1/messages/{task['id']}").json()
    assert row["state"] == "queued"
    assert row["lease_owner"] is None
    assert row["attempt"] == 0


def test_repo_lock_serialises_one_repo_and_allows_different_ones(tmp_path: Path) -> None:
    settings = worker_settings(tmp_path, tmp_path)
    one = tmp_path / "one"
    two = tmp_path / "two"

    active = 0
    peak = 0
    guard = threading.Lock()

    def work(repo: Path) -> None:
        nonlocal active, peak
        with repo_lock(settings, repo, "test"):
            with guard:
                active += 1
                peak = max(peak, active)
            time.sleep(0.2)
            with guard:
                active -= 1

    threads = [threading.Thread(target=work, args=(one,)) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert peak == 1

    barrier = threading.Barrier(2)
    entered: list[Path] = []
    entered_guard = threading.Lock()

    def parallel(repo: Path) -> None:
        with repo_lock(settings, repo, "test"):
            barrier.wait(timeout=5)
            with entered_guard:
                entered.append(repo)

    threads = [
        threading.Thread(target=parallel, args=(one,)),
        threading.Thread(target=parallel, args=(two,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert set(entered) == {one, two}


def test_registry_failure_does_not_block_the_task(
    tmp_path: Path,
    repo: Path,
    client: TestClient,
    fake_opencode: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = worker_settings(tmp_path, repo)
    stop = threading.Event()
    task = send(client, repo=str(repo), body="работа")

    def broken_put(url: str, **_kwargs) -> httpx.Response:
        request = httpx.Request("PUT", f"http://testserver{url}")
        return httpx.Response(500, request=request)

    monkeypatch.setattr(client, "put", broken_put)

    threads = run_slots(settings, stop, http=client)
    try:
        wait_for_result(client, task)
    finally:
        stop.set()
        join_all(threads)


SCRIPTED_OPENCODE = """#!__PYTHON__
import json
import os
import subprocess
import time
from pathlib import Path

journal = os.environ.get("FAKE_JOURNAL")
if journal:
    with open(journal, "a", encoding="utf-8") as sink:
        sink.write(f"start {time.time()} {Path.cwd()}\\n")
        sink.flush()

for line in os.environ.get("FAKE_FIRST_LINES", "").splitlines():
    print(line, flush=True)

time.sleep(float(os.environ.get("FAKE_SLEEP", "0.1")))

cwd = Path.cwd()
for name, content in json.loads(os.environ.get("FAKE_FILES", "{}")).items():
    path = cwd / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

message = os.environ.get("FAKE_GIT_COMMIT")
if message:
    subprocess.run(
        ["git", "-c", "user.name=executor", "-c", "user.email=executor@test", "add", "-A"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=executor",
            "-c",
            "user.email=executor@test",
            "commit",
            "-qm",
            message,
        ],
        check=True,
    )

for line in os.environ.get("FAKE_EVENTS", "").splitlines():
    print(line, flush=True)

if journal:
    with open(journal, "a", encoding="utf-8") as sink:
        sink.write(f"end {time.time()}\\n")
        sink.flush()
"""


@pytest.fixture()
def scripted_opencode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    script = tmp_path / "fake-opencode"
    script.write_text(SCRIPTED_OPENCODE.replace("__PYTHON__", sys.executable), encoding="utf-8")
    script.chmod(0o755)

    def configure(**env: str) -> None:
        for key, value in env.items():
            monkeypatch.setenv(key, value)

    configure(FAKE_SLEEP="0.05")
    return configure


def git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def result_of(client: TestClient, message: dict) -> dict:
    messages = client.get(f"/v1/threads/{message['thread_id']}").json()["messages"]
    return next(
        row for row in messages if row["kind"] == "result" and row["parent_id"] == message["id"]
    )


def artifacts_of(settings: WorkerSettings, result: dict) -> Path:
    return Path(settings.state_dir) / "artifacts" / result["id"]


def run_one(settings: WorkerSettings, client: TestClient, **overrides) -> tuple[dict, dict, Path]:
    stop = threading.Event()
    message = send(client, **overrides)
    threads = run_slots(settings, stop, http=client)
    try:
        wait_for_result(client, message)
    finally:
        stop.set()
        join_all(threads)
    result = result_of(client, message)
    return message, result, artifacts_of(settings, result)


def test_task_makes_one_commit_and_accumulated_patch(
    tmp_path: Path, repo: Path, client: TestClient, scripted_opencode
) -> None:
    scripted_opencode(FAKE_FILES=json.dumps({"note.txt": "one\n"}))
    settings = worker_settings(tmp_path, repo)
    base = git(repo, "rev-parse", "HEAD")

    message, result, artifacts = run_one(settings, client, repo=str(repo), meta={"ticket": "T4b.1"})
    worktree = tmp_path / "state" / "worktrees" / message["thread_id"]

    assert git(worktree, "rev-list", "--count", f"{base}..HEAD") == "1"
    assert (
        git(worktree, "log", "-1", "--format=%an <%ae>") == "agentbus executor <agentbus@localhost>"
    )
    assert git(worktree, "log", "-1", "--format=%s") == "agentbus: T4b.1 attempt 1"

    meta = result["meta"]
    patch = (artifacts / "patch.diff").read_text(encoding="utf-8")
    assert "note.txt" in patch
    assert meta["files_changed"] == ["note.txt"]
    assert meta["patch_bytes"] > 0
    assert meta["head_commit"] == git(worktree, "rev-parse", "HEAD")


def test_fix_in_same_thread_keeps_full_patch_and_separate_attempt_patch(
    tmp_path: Path, repo: Path, client: TestClient, scripted_opencode
) -> None:
    scripted_opencode(FAKE_FILES=json.dumps({"note.txt": "one\n"}))
    settings = worker_settings(tmp_path, repo)
    base = git(repo, "rev-parse", "HEAD")
    stop = threading.Event()
    threads = run_slots(settings, stop, http=client)
    try:
        first = send(client, repo=str(repo), body="первая")
        wait_for_result(client, first)
        scripted_opencode(FAKE_FILES=json.dumps({"other.txt": "two\n"}))
        second = send(
            client,
            kind="fix",
            repo=str(repo),
            thread_id=first["thread_id"],
            body="доработка",
        )
        wait_for_result(client, second)
    finally:
        stop.set()
        join_all(threads)

    worktree = tmp_path / "state" / "worktrees" / first["thread_id"]
    assert git(worktree, "rev-list", "--count", f"{base}..HEAD") == "2"

    first_result = result_of(client, first)
    second_result = result_of(client, second)
    first_artifacts = artifacts_of(settings, first_result)
    second_artifacts = artifacts_of(settings, second_result)

    full = (second_artifacts / "patch.diff").read_text(encoding="utf-8")
    attempt = (second_artifacts / "attempt.diff").read_text(encoding="utf-8")
    assert "note.txt" in full and "other.txt" in full
    assert "other.txt" in attempt and "note.txt" not in attempt
    assert second_result["meta"]["files_changed"] == ["note.txt", "other.txt"]
    assert second_result["meta"]["attempt_files_changed"] == ["other.txt"]

    first_patch = (first_artifacts / "patch.diff").read_text(encoding="utf-8")
    assert "note.txt" in first_patch and "other.txt" not in first_patch
    assert first_result["meta"]["attempt_files_changed"] == ["note.txt"]


def test_executor_commits_are_kept_and_included_in_patch(
    tmp_path: Path, repo: Path, client: TestClient, scripted_opencode
) -> None:
    scripted_opencode(
        FAKE_FILES=json.dumps({"exec.txt": "executor\n"}),
        FAKE_GIT_COMMIT="executor: own commit",
    )
    settings = worker_settings(tmp_path, repo)
    base = git(repo, "rev-parse", "HEAD")

    message, result, artifacts = run_one(settings, client, repo=str(repo))
    worktree = tmp_path / "state" / "worktrees" / message["thread_id"]

    assert git(worktree, "rev-list", "--count", f"{base}..HEAD") == "1"
    assert git(worktree, "log", "-1", "--format=%an <%ae>") == "executor <executor@test>"
    assert git(worktree, "log", "-1", "--format=%s") == "executor: own commit"

    patch = (artifacts / "patch.diff").read_text(encoding="utf-8")
    assert "exec.txt" in patch
    assert result["meta"]["files_changed"] == ["exec.txt"]
    assert result["meta"]["patch_bytes"] > 0


def test_empty_attempt_makes_no_commit_and_zero_patch(
    tmp_path: Path, repo: Path, client: TestClient, scripted_opencode
) -> None:
    scripted_opencode()
    settings = worker_settings(tmp_path, repo)
    base = git(repo, "rev-parse", "HEAD")

    message, result, artifacts = run_one(settings, client, repo=str(repo))
    worktree = tmp_path / "state" / "worktrees" / message["thread_id"]

    assert git(worktree, "rev-list", "--count", f"{base}..HEAD") == "0"
    assert (artifacts / "patch.diff").read_text(encoding="utf-8") == ""
    assert (artifacts / "attempt.diff").read_text(encoding="utf-8") == ""
    assert result["meta"]["patch_bytes"] == 0
    assert result["meta"]["files_changed"] == []
    assert result["meta"]["attempt_files_changed"] == []


def test_usage_sums_all_steps_and_counts_tool_calls(
    tmp_path: Path, repo: Path, client: TestClient, scripted_opencode
) -> None:
    events = [
        {"type": "tool_use", "part": {"tool": "bash"}},
        {
            "type": "step_finish",
            "part": {
                "cost": 0.1,
                "tokens": {
                    "input": 1,
                    "output": 2,
                    "reasoning": 3,
                    "cache": {"read": 4, "write": 5},
                },
            },
        },
        {"type": "tool_use", "part": {"tool": "read"}},
        {
            "type": "step_finish",
            "part": {
                "cost": 0.2,
                "tokens": {
                    "input": 10,
                    "output": 20,
                    "reasoning": 30,
                    "cache": {"read": 40, "write": 50},
                },
            },
        },
        {
            "type": "step_finish",
            "part": {
                "cost": 0.3,
                "tokens": {
                    "input": 100,
                    "output": 200,
                    "reasoning": 300,
                    "cache": {"read": 400, "write": 500},
                },
            },
        },
    ]
    scripted_opencode(FAKE_EVENTS="\n".join(json.dumps(event) for event in events))
    settings = worker_settings(tmp_path, repo)

    _, result, _ = run_one(settings, client, repo=str(repo))
    usage = result["meta"]["usage"]

    assert usage["steps"] == 3
    assert usage["tool_calls"] == 2
    assert usage["tokens"]["input"] == 111
    assert usage["tokens"]["output"] == 222
    assert usage["tokens"]["reasoning"] == 333
    assert usage["tokens"]["cache"] == {"read": 444, "write": 555}
    assert usage["cost"] == pytest.approx(0.6)
    assert result["meta"]["model"] == "fake/model"
    assert result["meta"]["harness"] == "opencode"


def test_run_log_shows_lines_while_opencode_is_running(
    tmp_path: Path, repo: Path, client: TestClient, scripted_opencode
) -> None:
    line = json.dumps({"type": "text", "part": {"time": {"end": 1.0}, "text": "первая"}})
    scripted_opencode(
        FAKE_FIRST_LINES=line,
        FAKE_SLEEP="2",
        FAKE_JOURNAL=str(tmp_path / "journal.txt"),
    )
    settings = worker_settings(tmp_path, repo)
    stop = threading.Event()
    message = send(client, repo=str(repo), body="долго")
    threads = run_slots(settings, stop, http=client)
    try:
        log_path = tmp_path / "state" / "artifacts" / message["id"] / "run.log"
        deadline = time.monotonic() + 10
        seen = False
        while time.monotonic() < deadline:
            if log_path.exists() and line in log_path.read_text(encoding="utf-8"):
                seen = True
                break
            time.sleep(0.02)
        assert seen, "run.log did not expose the first line while opencode was running"
        _, ends = journal_times(tmp_path)
        assert not ends, "the line was only visible after opencode finished"
        wait_for_result(client, message)
    finally:
        stop.set()
        join_all(threads)


def test_thread_without_base_file_falls_back_to_merge_base(
    tmp_path: Path, repo: Path, client: TestClient, scripted_opencode
) -> None:
    scripted_opencode()
    settings = worker_settings(tmp_path, repo)
    base = git(repo, "rev-parse", "HEAD")
    message = send(client, repo=str(repo), body="старый тред")

    worktree = tmp_path / "state" / "worktrees" / message["thread_id"]
    worktree.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "-b", f"agentbus/{message['thread_id']}", str(worktree))
    (worktree / "legacy.txt").write_text("legacy\n", encoding="utf-8")
    git(worktree, "-c", "user.name=e", "-c", "user.email=e@t", "add", "-A")
    git(
        worktree,
        "-c",
        "user.name=e",
        "-c",
        "user.email=e@t",
        "commit",
        "-qm",
        "legacy change",
    )
    base_file = tmp_path / "state" / "threads" / f"{message['thread_id']}.json"
    assert not base_file.exists()

    stop = threading.Event()
    threads = run_slots(settings, stop, http=client)
    try:
        wait_for_result(client, message)
    finally:
        stop.set()
        join_all(threads)

    result = result_of(client, message)
    artifacts = artifacts_of(settings, result)
    patch = (artifacts / "patch.diff").read_text(encoding="utf-8")
    assert result["meta"]["base_commit"] == base
    assert "legacy.txt" in patch
    assert result["meta"]["files_changed"] == ["legacy.txt"]
    assert result["meta"]["patch_bytes"] > 0
