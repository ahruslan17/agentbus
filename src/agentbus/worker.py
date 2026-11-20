"""Executor side: leases messages addressed to a local agent and runs OpenCode on them.

The worker owns no state beyond scratch files. Everything it learns and produces goes
back through the HTTP API, so the queue stays the single source of truth.

One thread == one git worktree == one branch == one OpenCode session, so a review loop
(task → result → verdict → fix) keeps its context.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Self

import httpx

from agentbus.config import WorkerSettings

EXECUTABLE_KINDS = ("task", "fix", "question", "note", "verdict")


def log(message: str) -> None:
    print(f"[agentbus-worker] {time.strftime('%H:%M:%S')} {message}", flush=True)


class Client:
    def __init__(self, settings: WorkerSettings) -> None:
        self.settings = settings
        self.http = httpx.Client(
            base_url=settings.url,
            headers={"Authorization": f"Bearer {settings.token}"},
            timeout=120.0,
        )

    def close(self) -> None:
        self.http.close()

    def lease(self, wait: int) -> dict[str, Any] | None:
        response = self.http.post(
            f"/v1/inbox/{self.settings.agent}/lease",
            params={"wait": wait, "kinds": ",".join(EXECUTABLE_KINDS)},
            timeout=wait + 60,
        )
        if response.status_code == 204:
            return None
        response.raise_for_status()
        return response.json()

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


class Heartbeat:
    """Keeps the lease alive while a long task runs."""

    def __init__(self, client: Client, message_id: str, interval: int) -> None:
        self.client = client
        self.message_id = message_id
        self.interval = max(15, interval)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.client.heartbeat(self.message_id)
            except Exception as exc:  # noqa: BLE001 - heartbeat must never kill the task
                log(f"heartbeat failed: {exc}")

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


def prepare_worktree(
    settings: WorkerSettings, message: dict[str, Any], repo: Path
) -> tuple[Path, str]:
    """One worktree per thread: fixes land on the same branch the task created."""
    meta = message.get("meta") or {}
    key = str(message.get("thread_id") or message["id"])
    branch = f"agentbus/{key}"

    if meta.get("worktree"):
        path = Path(str(meta["worktree"])).expanduser()
        if (path / ".git").exists():
            return path, _branch_of(path) or branch

    path = Path(settings.state_dir).expanduser() / "worktrees" / key
    if (path / ".git").exists():
        return path, _branch_of(path) or branch

    path.parent.mkdir(parents=True, exist_ok=True)
    base = meta.get("base_ref")
    if _branch_exists(repo, branch):
        _git(repo, "worktree", "add", str(path), branch)
    elif base:
        _git(repo, "worktree", "add", "-b", branch, str(path), str(base))
    else:
        _git(repo, "worktree", "add", "-b", branch, str(path))
    return path, branch


def build_prompt(message: dict[str, Any], worktree: Path | None, branch: str, policy: str) -> str:
    meta = message.get("meta") or {}
    parts = [
        f"Ты исполнитель в паре с агентом «{message['sender']}». Сообщение пришло через очередь agentbus.",
        f"Тред: {message.get('thread_id')}. Тип: {message.get('kind')}. Попытка: {message.get('attempt')}.",
    ]
    if worktree is not None:
        parts.append(f"Рабочее дерево: {worktree}. Ветка: {branch}.")
    acceptance = meta.get("acceptance")
    if acceptance:
        parts.append("Критерии приёмки:\n" + "\n".join(f"- {item}" for item in acceptance))
    scope = meta.get("scope_files")
    if scope:
        parts.append("Файлы в области задачи:\n" + "\n".join(f"- {item}" for item in scope))
    parts.append("--- ЗАДАЧА ---\n" + str(message["body"]).strip() + "\n--- КОНЕЦ ЗАДАЧИ ---")
    if policy:
        parts.append(policy.strip())
    if worktree is not None:
        parts.append(
            "Правила:\n"
            "- Работай только внутри этого рабочего дерева.\n"
            "- Не делай git push, не мержи в main, не переключай ветку.\n"
            "- Запускай проверки, которые требует постановка.\n"
            "- В конце — короткий отчёт: что сделано, какие файлы изменены, какие команды "
            "проверки запускал и с каким результатом, что осталось неясным.\n"
            "- Если для задачи нужно решение, которого нет в постановке, опиши варианты в "
            "отчёте и ничего не выбирай молча."
        )
    else:
        parts.append("Правил про git нет: задачи с кодом не было. Ответь по существу и коротко.")
    return "\n\n".join(parts)


def run_opencode(
    settings: WorkerSettings,
    prompt: str,
    cwd: Path,
    session_id: str | None,
    raw_log: Path,
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


def _parse_events(raw_log: Path, session_id: str | None) -> dict[str, Any]:
    text: list[str] = []
    errors: list[str] = []
    usage: dict[str, Any] = {}
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
            elif kind == "step_finish":
                part = event.get("part") or {}
                usage = {"cost": part.get("cost"), "tokens": part.get("tokens")}
    return {
        "text": "\n\n".join(text).strip(),
        "errors": errors,
        "session_id": seen_session,
        "usage": usage,
    }


def collect_diff(worktree: Path, artifacts: Path) -> dict[str, Any]:
    _git(worktree, "add", "-A")
    patch = _git(worktree, "diff", "--cached")
    stat = _git(worktree, "diff", "--cached", "--stat")
    changed = [
        line for line in _git(worktree, "diff", "--cached", "--name-only").splitlines() if line
    ]
    (artifacts / "patch.diff").write_text(patch, encoding="utf-8")
    head = _git(worktree, "rev-parse", "--short", "HEAD").strip()
    return {
        "diff_stat": stat.strip(),
        "files_changed": changed,
        "patch_bytes": len(patch.encode("utf-8")),
        "base_commit": head,
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


def handle(settings: WorkerSettings, client: Client, message: dict[str, Any]) -> None:
    message_id = str(message["id"])
    meta = dict(message.get("meta") or {})
    artifacts = Path(settings.state_dir).expanduser() / "artifacts" / message_id
    artifacts.mkdir(parents=True, exist_ok=True)

    thread = client.thread(str(message["thread_id"])) if message.get("thread_id") else {}
    repo = resolve_repo(settings, message, thread)

    if repo is not None:
        worktree, branch = prepare_worktree(settings, message, repo)
        session_id = thread.get("session_id")
    else:
        worktree = (
            Path(settings.state_dir).expanduser()
            / "scratch"
            / (str(message.get("thread_id") or message_id))
        )
        worktree.mkdir(parents=True, exist_ok=True)
        branch = ""
        session_id = thread.get("session_id")

    prompt = build_prompt(
        message, worktree if repo is not None else None, branch, settings.extra_instructions
    )
    log(f"{message_id} {message['kind']} from {message['sender']}: {worktree}")

    with Heartbeat(client, message_id, settings.lease_ttl_seconds // 3):
        outcome = run_opencode(settings, prompt, worktree, session_id, artifacts / "run.log")

        result_meta: dict[str, Any] = {
            "worktree": str(worktree) if repo is not None else None,
            "branch": branch or None,
            "repo": str(repo) if repo else None,
            "session_id": outcome.get("session_id"),
            "exit_code": outcome.get("exit_code"),
            "duration_seconds": outcome.get("duration_seconds"),
            "timeout": outcome.get("timeout"),
            "usage": outcome.get("usage"),
            "artifacts": ["run.log"],
        }

        if repo is not None and not outcome.get("timeout"):
            result_meta.update(collect_diff(worktree, artifacts))
            result_meta["artifacts"].append("patch.diff")
            if meta.get("checks"):
                result_meta["checks"] = run_checks(worktree, artifacts)
                result_meta["artifacts"].append("checks.txt")

        body = outcome.get("text") or "(пустой отчёт: агент не вернул текст)"
        if outcome.get("timeout"):
            body = "Задача убита по таймауту. Полный лог в артефакте run.log.\n\n" + body
        if outcome.get("errors"):
            body += "\n\nСтроки ошибок из лога:\n" + "\n".join(outcome["errors"][:10])
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


def main() -> int:
    settings = WorkerSettings.from_env()
    client = Client(settings)
    log(f"agent={settings.agent} url={settings.url} model={settings.model}")
    try:
        while True:
            try:
                message = client.lease(wait=60)
            except httpx.HTTPError as exc:
                log(f"lease failed: {exc}")
                time.sleep(5)
                continue
            if message is None:
                continue
            try:
                handle(settings, client, message)
            except Exception as exc:  # noqa: BLE001 - one bad task must not stop the worker
                log(f"{message.get('id')} failed: {exc}")
                try:
                    client.fail(str(message.get("id")), f"{type(exc).__name__}: {exc}")
                except httpx.HTTPError as inner:
                    log(f"could not report failure: {inner}")
    except KeyboardInterrupt:
        log("stopped")
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
