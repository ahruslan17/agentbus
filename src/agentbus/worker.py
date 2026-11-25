"""Executor side: leases messages addressed to a local agent and runs OpenCode on them.

The worker owns no state beyond scratch files. Everything it learns and produces goes
back through the HTTP API, so the queue stays the single source of truth.

One thread == one git worktree == one branch == one OpenCode session, so a review loop
(task → result → verdict → fix) keeps its context.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Self
from urllib.parse import quote

import httpx

from agentbus.config import WorkerSettings

EXECUTABLE_KINDS = ("task", "fix", "question", "note", "verdict")


def log(message: str) -> None:
    print(f"[agentbus-worker] {time.strftime('%H:%M:%S')} {message}", flush=True)


class Client:
    def __init__(self, settings: WorkerSettings, http: httpx.Client | None = None) -> None:
        self.settings = settings
        self._owns_http = http is None
        self.http = (
            http
            if http is not None
            else httpx.Client(
                base_url=settings.url,
                headers={"Authorization": f"Bearer {settings.token}"},
                timeout=120.0,
            )
        )

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def lease(self, wait: int, owner: str | None = None) -> dict[str, Any] | None:
        params: dict[str, Any] = {"wait": wait, "kinds": ",".join(EXECUTABLE_KINDS)}
        if owner is not None:
            params["owner"] = owner
        response = self.http.post(
            f"/v1/inbox/{self.settings.agent}/lease",
            params=params,
            timeout=wait + 60,
        )
        if response.status_code == 204:
            return None
        response.raise_for_status()
        return response.json()

    def report_worker(self, owner: str, state: str, message_id: str | None = None) -> None:
        response = self.http.put(
            f"/v1/workers/{quote(owner, safe='')}",
            json={
                "agent": self.settings.agent,
                "host": socket.gethostname(),
                "pid": os.getpid(),
                "model": self.settings.model,
                "harness": "opencode",
                "state": state,
                "message_id": message_id,
            },
        )
        response.raise_for_status()

    def thread(self, thread_id: str) -> dict[str, Any]:
        response = self.http.get(f"/v1/threads/{thread_id}")
        response.raise_for_status()
        return response.json()["thread"]

    def reply(
        self, message_id: str, body: str, meta: dict[str, Any], kind: str = "result"
    ) -> dict[str, Any]:
        response = self.http.post(
            f"/v1/messages/{message_id}/reply",
            json={"sender": self.settings.agent, "kind": kind, "body": body, "meta": meta},
        )
        response.raise_for_status()
        return dict(response.json())

    def fail(self, message_id: str, note: str) -> None:
        response = self.http.post(
            f"/v1/messages/{message_id}/state", json={"state": "failed", "note": note}
        )
        response.raise_for_status()

    def heartbeat(self, message_id: str) -> None:
        response = self.http.post(f"/v1/messages/{message_id}/heartbeat")
        response.raise_for_status()

    def release(self, message_id: str, owner: str) -> None:
        response = self.http.post(f"/v1/messages/{message_id}/release", json={"owner": owner})
        response.raise_for_status()


class Heartbeat:
    """Keeps the lease, and the worker registry, alive while a long task runs."""

    def __init__(
        self,
        client: Client,
        message_id: str,
        interval: int,
        slot: Slot | None = None,
    ) -> None:
        self.client = client
        self.message_id = message_id
        self.interval = max(15, interval)
        self.slot = slot
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.client.heartbeat(self.message_id)
            except Exception as exc:  # noqa: BLE001 - heartbeat must never kill the task
                log(f"heartbeat failed: {exc}")
            if self.slot is not None:
                self.slot.report("busy", self.message_id)

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=300, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _branch_exists(repo: Path, branch: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _branch_of(worktree: Path) -> str:
    try:
        return _git(worktree, "rev-parse", "--abbrev-ref", "HEAD").strip()
    except RuntimeError:
        return ""


def resolve_repo(
    settings: WorkerSettings, message: dict[str, Any], thread: dict[str, Any]
) -> Path | None:
    meta = message.get("meta") or {}
    for raw in (meta.get("repo"), thread.get("repo")):
        if not raw:
            continue
        repo = Path(str(raw)).expanduser().resolve()
        allowed = tuple(str(Path(p).expanduser().resolve()) for p in settings.repos)
        if not any(str(repo).startswith(prefix) for prefix in allowed):
            raise RuntimeError(f"repo outside AGENTBUS_REPOS allowlist: {repo}")
        if not (repo / ".git").exists():
            raise RuntimeError(f"not a git repository: {repo}")
        return repo
    return None


@contextmanager
def repo_lock(settings: WorkerSettings, repo: Path, name: str) -> Iterator[None]:
    """Serialise work on one repository; `flock` also holds between processes.

    Parallel `git worktree add` in one repository races on `.git/*.lock`, and two
    `make check` runs may fight over the same build state. The lock is per absolute
    repository path, so different repositories still run in parallel.
    """
    lock_dir = Path(settings.state_dir).expanduser() / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(str(Path(repo).resolve()).encode("utf-8")).hexdigest()[:16]
    path = lock_dir / f"{digest}.lock"
    with path.open("w", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        log(f"repo lock {name}: {repo}")
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _base_file(settings: WorkerSettings, key: str) -> Path:
    return Path(settings.state_dir).expanduser() / "threads" / f"{key}.json"


def _read_base(settings: WorkerSettings, key: str) -> str | None:
    try:
        data = json.loads(_base_file(settings, key).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    commit = str(data.get("base_commit") or "").strip()
    return commit or None


def _write_base(settings: WorkerSettings, key: str, base_commit: str, branch: str) -> None:
    path = _base_file(settings, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"base_commit": base_commit, "branch": branch}
    path.write_text(json.dumps(payload), encoding="utf-8")


def _merge_base(worktree: Path, base_ref: str | None) -> str:
    """Base commit for a thread that predates `<state_dir>/threads/<id>.json`."""
    refs = [str(base_ref)] if base_ref else []
    refs += ["master", "main"]
    for ref in refs:
        try:
            value = _git(worktree, "merge-base", "HEAD", ref).strip()
        except RuntimeError:
            continue
        if value:
            return value
    roots = _git(worktree, "rev-list", "--max-parents=0", "HEAD").split()
    return roots[-1] if roots else _git(worktree, "rev-parse", "HEAD").strip()


def _existing_worktree(
    settings: WorkerSettings, key: str, path: Path, branch: str, base_ref: str | None
) -> tuple[Path, str, str]:
    actual = _branch_of(path) or branch
    base = _read_base(settings, key)
    if base is None:
        base = _merge_base(path, base_ref)
        _write_base(settings, key, base, actual)
    return path, actual, base


def prepare_worktree(
    settings: WorkerSettings, message: dict[str, Any], repo: Path
) -> tuple[Path, str, str]:
    """One worktree per thread: fixes land on the same branch the task created."""
    meta = message.get("meta") or {}
    key = str(message.get("thread_id") or message["id"])
    branch = f"agentbus/{key}"
    base_ref = meta.get("base_ref")

    if meta.get("worktree"):
        path = Path(str(meta["worktree"])).expanduser()
        if (path / ".git").exists():
            return _existing_worktree(settings, key, path, branch, base_ref)

    path = Path(settings.state_dir).expanduser() / "worktrees" / key
    if (path / ".git").exists():
        return _existing_worktree(settings, key, path, branch, base_ref)

    path.parent.mkdir(parents=True, exist_ok=True)
    if _branch_exists(repo, branch):
        _git(repo, "worktree", "add", str(path), branch)
        return _existing_worktree(settings, key, path, branch, base_ref)
    if base_ref:
        _git(repo, "worktree", "add", "-b", branch, str(path), str(base_ref))
    else:
        _git(repo, "worktree", "add", "-b", branch, str(path))
    base_commit = _git(path, "rev-parse", "HEAD").strip()
    _write_base(settings, key, base_commit, branch)
    return path, branch, base_commit


def build_prompt(message: dict[str, Any], worktree: Path | None, branch: str, policy: str) -> str:
    meta = message.get("meta") or {}
    parts = [
        (
            f'You are the executor paired with agent "{message["sender"]}". '
            "The message arrived through the agentbus queue."
        ),
        (
            f"Thread: {message.get('thread_id')}. Kind: {message.get('kind')}. "
            f"Attempt: {message.get('attempt')}."
        ),
    ]
    if worktree is not None:
        parts.append(f"Worktree: {worktree}. Branch: {branch}.")
    acceptance = meta.get("acceptance")
    if acceptance:
        parts.append("Acceptance criteria:\n" + "\n".join(f"- {item}" for item in acceptance))
    scope = meta.get("scope_files")
    if scope:
        parts.append("Files in scope:\n" + "\n".join(f"- {item}" for item in scope))
    parts.append("--- TASK ---\n" + str(message["body"]).strip() + "\n--- END OF TASK ---")
    if policy:
        parts.append(policy.strip())
    if worktree is not None:
        parts.append(
            "Rules:\n"
            "- Work only inside this worktree.\n"
            "- Do not git push, do not merge into main, do not switch branches.\n"
            "- Do not commit: the worker will commit.\n"
            "- Run the checks the task requires.\n"
            "- Finish with a short report: what you did, which files you changed, which check "
            "commands you ran and with what result, and what remains unclear.\n"
            "- If the task needs a decision the task text does not cover, list the options in "
            "the report and do not pick one silently."
        )
    else:
        parts.append(
            "No git rules apply: this task had no code. Answer to the point and keep it short."
        )
    return "\n\n".join(parts)


def run_opencode(
    settings: WorkerSettings,
    prompt: str,
    cwd: Path,
    session_id: str | None,
    raw_log: Path,
    on_process: Callable[[subprocess.Popen[str] | None], None] | None = None,
) -> dict[str, Any]:
    command = [
        settings.opencode_bin,
        "run",
        "--auto",
        "--format",
        "json",
        "--model",
        settings.model,
        "--title",
        f"agentbus {cwd.name}",
    ]
    if session_id:
        command += ["--session", session_id]
    command.append(prompt)

    started = time.monotonic()
    sink = raw_log.open("w", encoding="utf-8")
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    assert process.stdout is not None

    def pump() -> None:
        for line in process.stdout:
            sink.write(line)
            sink.flush()

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    if on_process is not None:
        on_process(process)

    timed_out = False
    try:
        process.wait(timeout=settings.task_timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
    finally:
        reader.join(timeout=30)
        sink.close()
        if on_process is not None:
            on_process(None)

    duration = round(time.monotonic() - started, 1)
    parsed = _parse_events(raw_log, session_id)
    parsed.update(
        {
            "timeout": timed_out,
            "exit_code": process.returncode,
            "duration_seconds": duration,
        }
    )
    if timed_out:
        parsed["text"] = ""
        parsed["errors"] = [*parsed["errors"], f"timeout after {settings.task_timeout_seconds}s"]
    return parsed


def _empty_usage() -> dict[str, Any]:
    return {
        "cost": 0.0,
        "steps": 0,
        "tool_calls": 0,
        "tokens": {
            "input": 0,
            "output": 0,
            "reasoning": 0,
            "cache": {"read": 0, "write": 0},
        },
    }


def _parse_events(raw_log: Path, session_id: str | None) -> dict[str, Any]:
    text: list[str] = []
    errors: list[str] = []
    usage = _empty_usage()
    seen_session = session_id
    with raw_log.open(encoding="utf-8", errors="replace") as source:
        for line in source:
            line = line.strip()
            if not line.startswith("{"):
                if line:
                    errors.append(line[:500])
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                errors.append(line[:500])
                continue
            seen_session = event.get("sessionID") or seen_session
            kind = event.get("type")
            if kind == "text":
                part = event.get("part") or {}
                if (part.get("time") or {}).get("end") and part.get("text"):
                    text.append(str(part["text"]).strip())
            elif kind == "error":
                errors.append(json.dumps(event.get("error"))[:500])
            elif kind == "tool_use":
                usage["tool_calls"] += 1
            elif kind == "step_finish":
                part = event.get("part") or {}
                tokens = part.get("tokens") or {}
                cache = tokens.get("cache") or {}
                usage["steps"] += 1
                usage["cost"] += float(part.get("cost") or 0)
                usage["tokens"]["input"] += int(tokens.get("input") or 0)
                usage["tokens"]["output"] += int(tokens.get("output") or 0)
                usage["tokens"]["reasoning"] += int(tokens.get("reasoning") or 0)
                usage["tokens"]["cache"]["read"] += int(cache.get("read") or 0)
                usage["tokens"]["cache"]["write"] += int(cache.get("write") or 0)
    return {
        "text": "\n\n".join(text).strip(),
        "errors": errors,
        "session_id": seen_session,
        "usage": usage,
    }


def _author_parts(raw: str) -> tuple[str, str]:
    name, sep, rest = raw.partition("<")
    email = rest.rstrip(">").strip() if sep else ""
    if not name.strip() or not email:
        raise RuntimeError("AGENTBUS_COMMIT_AUTHOR must look like 'Name <email>'")
    return name.strip(), email


def commit_attempt(settings: WorkerSettings, worktree: Path, message: dict[str, Any]) -> bool:
    """Commit whatever the executor left uncommitted; never rewrite its own commits."""
    _git(worktree, "add", "-A")
    if not _git(worktree, "diff", "--cached", "--name-only").strip():
        return False
    meta = message.get("meta") or {}
    label = meta.get("ticket") or message.get("thread_id") or message["id"]
    attempt = message.get("attempt")
    name, email = _author_parts(settings.commit_author)
    _git(
        worktree,
        "-c",
        f"user.name={name}",
        "-c",
        f"user.email={email}",
        "commit",
        "--no-verify",
        "-m",
        f"agentbus: {label} attempt {attempt}",
    )
    return True


def collect_diff(
    worktree: Path, artifacts: Path, base_commit: str, head_before: str
) -> dict[str, Any]:
    head_commit = _git(worktree, "rev-parse", "HEAD").strip()
    patch = _git(worktree, "diff", f"{base_commit}..HEAD")
    stat = _git(worktree, "diff", f"{base_commit}..HEAD", "--stat")
    changed = [
        line
        for line in _git(worktree, "diff", f"{base_commit}..HEAD", "--name-only").splitlines()
        if line
    ]
    attempt = _git(worktree, "diff", f"{head_before}..{head_commit}")
    attempt_changed = [
        line
        for line in _git(
            worktree, "diff", f"{head_before}..{head_commit}", "--name-only"
        ).splitlines()
        if line
    ]
    (artifacts / "patch.diff").write_text(patch, encoding="utf-8")
    (artifacts / "attempt.diff").write_text(attempt, encoding="utf-8")
    return {
        "base_commit": base_commit,
        "head_commit": head_commit,
        "diff_stat": stat.strip(),
        "files_changed": changed,
        "patch_bytes": len(patch.encode("utf-8")),
        "attempt_files_changed": attempt_changed,
    }


def run_checks(worktree: Path, artifacts: Path) -> dict[str, Any]:
    if not (worktree / "Makefile").exists():
        return {"skipped": "no Makefile"}
    try:
        result = subprocess.run(
            ["make", "check"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "skipped": "timeout after 1800s"}
    tail = (result.stdout + result.stderr)[-20000:]
    (artifacts / "checks.txt").write_text(tail, encoding="utf-8")
    return {"ok": result.returncode == 0, "exit_code": result.returncode, "tail": tail[-2000:]}


def handle(
    settings: WorkerSettings,
    client: Client,
    message: dict[str, Any],
    slot: Slot | None = None,
) -> None:
    message_id = str(message["id"])
    meta = dict(message.get("meta") or {})
    artifacts = Path(settings.state_dir).expanduser() / "artifacts" / message_id
    artifacts.mkdir(parents=True, exist_ok=True)

    thread = client.thread(str(message["thread_id"])) if message.get("thread_id") else {}
    repo = resolve_repo(settings, message, thread)

    if repo is not None:
        with repo_lock(settings, repo, "worktree"):
            worktree, branch, base_commit = prepare_worktree(settings, message, repo)
        head_before = _git(worktree, "rev-parse", "HEAD").strip()
        session_id = thread.get("session_id")
    else:
        worktree = (
            Path(settings.state_dir).expanduser()
            / "scratch"
            / (str(message.get("thread_id") or message_id))
        )
        worktree.mkdir(parents=True, exist_ok=True)
        branch = ""
        base_commit = ""
        head_before = ""
        session_id = thread.get("session_id")

    prompt = build_prompt(
        message, worktree if repo is not None else None, branch, settings.extra_instructions
    )
    log(f"{message_id} {message['kind']} from {message['sender']}: {worktree}")

    with Heartbeat(client, message_id, settings.lease_ttl_seconds // 3, slot=slot):
        outcome = run_opencode(
            settings,
            prompt,
            worktree,
            session_id,
            artifacts / "run.log",
            on_process=slot.on_process if slot is not None else None,
        )

        result_meta: dict[str, Any] = {
            "worktree": str(worktree) if repo is not None else None,
            "branch": branch or None,
            "repo": str(repo) if repo else None,
            "session_id": outcome.get("session_id"),
            "exit_code": outcome.get("exit_code"),
            "duration_seconds": outcome.get("duration_seconds"),
            "timeout": outcome.get("timeout"),
            "usage": outcome.get("usage"),
            "model": settings.model,
            "harness": "opencode",
            "artifacts": ["run.log"],
        }

        if repo is not None and not outcome.get("timeout"):
            commit_attempt(settings, worktree, message)
            result_meta.update(collect_diff(worktree, artifacts, base_commit, head_before))
            result_meta["artifacts"].extend(["patch.diff", "attempt.diff"])
            if meta.get("checks"):
                if meta.get("checks_parallel"):
                    result_meta["checks"] = run_checks(worktree, artifacts)
                else:
                    with repo_lock(settings, repo, "checks"):
                        result_meta["checks"] = run_checks(worktree, artifacts)
                result_meta["artifacts"].append("checks.txt")

        body = outcome.get("text") or "(empty report: the agent returned no text)"
        if outcome.get("timeout"):
            body = (
                "The task was killed on timeout. The full log is in the run.log artifact.\n\n"
                + body
            )
        if outcome.get("errors"):
            body += "\n\nError lines from the log:\n" + "\n".join(outcome["errors"][:10])
        created = client.reply(message_id, body, result_meta)
        _relocate_artifacts(artifacts, str(created.get("id") or ""))


def _relocate_artifacts(artifacts: Path, reply_id: str) -> None:
    """Artifacts are produced under the task id but referenced by the reply message id."""
    if not reply_id or reply_id == artifacts.name or not artifacts.is_dir():
        return
    target = artifacts.parent / reply_id
    try:
        if target.exists():
            import shutil

            shutil.rmtree(target)
        artifacts.rename(target)
    except OSError as exc:
        log(f"could not move artifacts to {reply_id}: {exc}")


class Slot:
    """One executor thread: its own client and lease owner, plus the process it runs."""

    def __init__(
        self,
        settings: WorkerSettings,
        index: int,
        stop: threading.Event,
        http: httpx.Client | None = None,
    ) -> None:
        self.settings = settings
        self.owner = f"{settings.agent}#{index}"
        self.stop = stop
        self.client = Client(settings, http=http)
        self._process_lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._busy = False

    def report(self, state: str, message_id: str | None = None) -> None:
        """Registry updates are best effort: they must never stop a slot or a task."""
        try:
            self.client.report_worker(self.owner, state, message_id)
        except Exception as exc:  # noqa: BLE001 - the queue stays the source of truth
            log(f"{self.owner} registry {state} failed: {exc}")

    def on_process(self, process: subprocess.Popen[str] | None) -> None:
        with self._process_lock:
            self._process = process

    @property
    def busy(self) -> bool:
        return self._busy

    def kill(self) -> None:
        with self._process_lock:
            process = self._process
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as exc:
            log(f"{self.owner} could not kill {process.pid}: {exc}")

    def run(self) -> None:
        self.report("idle")
        try:
            while not self.stop.is_set():
                try:
                    message = self.client.lease(wait=60, owner=self.owner)
                except httpx.HTTPError as exc:
                    log(f"{self.owner} lease failed: {exc}")
                    self.stop.wait(5)
                    continue
                except Exception as exc:  # noqa: BLE001 - a slot must survive API hiccups
                    log(f"{self.owner} lease failed: {exc}")
                    self.stop.wait(5)
                    continue
                if message is None:
                    if not self.stop.is_set():
                        self.report("idle")
                    continue
                if self.stop.is_set():
                    # A lease returned while stopping: give it back at once instead of
                    # holding it until the TTL runs out. A failed release only costs a
                    # wait, so it is logged and never raises.
                    stopped_id = str(message.get("id"))
                    try:
                        self.client.release(stopped_id, self.owner)
                    except Exception as exc:  # noqa: BLE001 - the lease expires on its own
                        log(f"{self.owner} could not release {stopped_id}: {exc}")
                    continue
                message_id = str(message.get("id"))
                self.report("busy", message_id)
                self._busy = True
                try:
                    handle(self.settings, self.client, message, slot=self)
                except Exception as exc:  # noqa: BLE001 - one bad task must not stop the slot
                    log(f"{message_id} failed: {exc}")
                    try:
                        self.client.fail(message_id, f"{type(exc).__name__}: {exc}")
                    except httpx.HTTPError as inner:
                        log(f"could not report failure: {inner}")
                finally:
                    self._busy = False
                if not self.stop.is_set():
                    self.report("idle")
        finally:
            self.report("stopping")
            self.report("stopped")
            self.client.close()


def run_slots(
    settings: WorkerSettings,
    stop: threading.Event,
    http: httpx.Client | None = None,
) -> list[threading.Thread]:
    """Start one thread per configured slot and return them for joining."""
    threads: list[threading.Thread] = []
    for index in range(settings.slots):
        slot = Slot(settings, index, stop, http=http)
        thread = threading.Thread(target=slot.run, name=slot.owner, daemon=True)
        thread.slot = slot  # type: ignore[attr-defined]
        thread.start()
        threads.append(thread)
    return threads


def main() -> int:
    settings = WorkerSettings.from_env()
    stop = threading.Event()
    log(f"agent={settings.agent} url={settings.url} model={settings.model} slots={settings.slots}")

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    threads = run_slots(settings, stop)
    slots: list[Slot] = [thread.slot for thread in threads]  # type: ignore[attr-defined]
    deadline: float | None = None
    try:
        while True:
            if stop.is_set() and deadline is None:
                running = sum(1 for slot in slots if slot.busy)
                log(f"stopping, waiting for {running} running tasks")
                deadline = time.monotonic() + settings.stop_grace_seconds
            if not any(thread.is_alive() for thread in threads):
                break
            if not stop.is_set():
                time.sleep(0.2)
                continue
            assert deadline is not None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log("grace expired, killing running opencode processes")
                for slot in slots:
                    slot.kill()
                for thread in threads:
                    thread.join(timeout=5)
                break
            for thread in threads:
                thread.join(timeout=min(0.5, remaining))
    except KeyboardInterrupt:
        stop.set()
        for slot in slots:
            slot.kill()
    log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
