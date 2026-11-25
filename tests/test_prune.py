"""`prune` removes files of closed threads only, and branches only when merged."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agentbus import prune, store

from .conftest import TEST_URL


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def commit(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", message)


def branch_exists(repo: Path, branch: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    (path / "README.md").write_text("repo\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "master", str(path)], check=True)
    commit(path, "init")
    return path


@pytest.fixture()
def prune_settings(tmp_path: Path, repo: Path) -> prune.PruneSettings:
    return prune.PruneSettings(
        database_url=TEST_URL,
        state_dir=tmp_path / "state",
        artifacts_dir=tmp_path / "artifacts",
        repos=(tmp_path,),
    )


def closed_thread(conn, repo: Path, *, sender: str = "smoke-a") -> tuple[str, str]:
    """task -> result, result acked: no queued or leased messages left."""
    task = store.create_message(
        conn,
        sender=sender,
        recipient="local-opencode",
        kind="task",
        body="task",
        repo=str(repo),
        meta={"ticket": "T8"},
    )
    result = store.reply(
        conn,
        parent_id=task["id"],
        sender="local-opencode",
        kind="result",
        body="done",
        meta={},
    )
    store.set_state(conn, message_id=result["id"], state="done", note="accept")
    return str(task["thread_id"]), str(task["id"])


def active_thread(conn, repo: Path) -> tuple[str, str]:
    task = store.create_message(
        conn,
        sender="smoke-b",
        recipient="local-opencode",
        kind="task",
        body="task",
        repo=str(repo),
        meta={"ticket": "T8"},
    )
    return str(task["thread_id"]), str(task["id"])


def add_worktree(
    repo: Path, settings: prune.PruneSettings, thread_id: str, *, commit_file: bool = False
) -> Path:
    path = settings.state_dir / "worktrees" / thread_id
    path.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "-q", "-b", f"agentbus/{thread_id}", str(path))
    if commit_file:
        (path / f"{thread_id}.txt").write_text("x\n", encoding="utf-8")
        commit(path, thread_id)
    return path


def add_files(settings: prune.PruneSettings, thread_id: str, message_id: str) -> None:
    artifacts = settings.artifacts_dir / message_id
    artifacts.mkdir(parents=True)
    (artifacts / "run.log").write_text("log\n", encoding="utf-8")
    base = settings.state_dir / "threads" / f"{thread_id}.json"
    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_text(
        json.dumps({"base_commit": "0" * 40, "branch": f"agentbus/{thread_id}"}),
        encoding="utf-8",
    )


def test_keep_zero_prunes_only_closed(clean, repo, prune_settings):
    with store.connect(TEST_URL) as conn:
        closed_id, closed_message = closed_thread(conn, repo)
        active_id, active_message = active_thread(conn, repo)
        records = prune.closed_threads(conn)

    assert [record.id for record in records] == [closed_id]

    closed_worktree = add_worktree(repo, prune_settings, closed_id, commit_file=True)
    active_worktree = add_worktree(repo, prune_settings, active_id)
    add_files(prune_settings, closed_id, closed_message)
    add_files(prune_settings, active_id, active_message)

    plan = prune.run(prune_settings, keep=0)

    assert plan.failures == []
    assert not closed_worktree.exists()
    assert not (prune_settings.artifacts_dir / closed_message).exists()
    assert not (prune_settings.state_dir / "threads" / f"{closed_id}.json").exists()

    assert active_worktree.exists()
    assert (prune_settings.artifacts_dir / active_message).is_dir()
    assert (prune_settings.state_dir / "threads" / f"{active_id}.json").is_file()

    # --delete-merged-branches was not passed: the branch stays even if closed.
    assert branch_exists(repo, f"agentbus/{closed_id}")


def test_dry_run_deletes_nothing(clean, repo, prune_settings):
    with store.connect(TEST_URL) as conn:
        thread_id, message_id = closed_thread(conn, repo)
    worktree = add_worktree(repo, prune_settings, thread_id, commit_file=True)
    add_files(prune_settings, thread_id, message_id)

    plan = prune.run(prune_settings, keep=0, dry_run=True)

    assert plan.worktrees and plan.artifact_dirs and plan.base_files
    assert worktree.exists()
    assert (prune_settings.artifacts_dir / message_id).is_dir()
    assert (prune_settings.state_dir / "threads" / f"{thread_id}.json").is_file()
    assert branch_exists(repo, f"agentbus/{thread_id}")


def test_delete_merged_branches_keeps_unmerged(clean, repo, prune_settings):
    with store.connect(TEST_URL) as conn:
        merged_id, merged_message = closed_thread(conn, repo, sender="smoke-a")
        unmerged_id, unmerged_message = closed_thread(conn, repo, sender="smoke-b")

    merged_worktree = add_worktree(repo, prune_settings, merged_id, commit_file=True)
    unmerged_worktree = add_worktree(repo, prune_settings, unmerged_id, commit_file=True)
    add_files(prune_settings, merged_id, merged_message)
    add_files(prune_settings, unmerged_id, unmerged_message)
    git(
        repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "merge",
        "--no-ff",
        "-q",
        "-m",
        f"merge {merged_id}",
        f"agentbus/{merged_id}",
    )

    plan = prune.run(prune_settings, keep=0, delete_merged_branches=True)

    assert plan.failures == []
    assert not branch_exists(repo, f"agentbus/{merged_id}")
    assert branch_exists(repo, f"agentbus/{unmerged_id}")
    assert not merged_worktree.exists()
    assert not unmerged_worktree.exists()


def test_repo_outside_allowlist_skips_worktree(clean, repo, prune_settings, tmp_path):
    with store.connect(TEST_URL) as conn:
        thread_id, message_id = closed_thread(conn, repo)
    worktree = add_worktree(repo, prune_settings, thread_id, commit_file=True)
    add_files(prune_settings, thread_id, message_id)
    settings = prune.PruneSettings(
        database_url=TEST_URL,
        state_dir=prune_settings.state_dir,
        artifacts_dir=prune_settings.artifacts_dir,
        repos=(tmp_path / "other",),
    )

    plan = prune.run(settings, keep=0)

    assert worktree.exists()
    assert any("outside AGENTBUS_REPOS" in warning for warning in plan.skipped)


def make_dirty(worktree: Path, *, staged: bool = False) -> None:
    path = worktree / "dirty.txt"
    path.write_text("dirty\n", encoding="utf-8")
    if staged:
        git(worktree, "add", "dirty.txt")


def add_detached_worktree(
    repo: Path, settings: prune.PruneSettings, thread_id: str, *, commit_file: bool = False
) -> Path:
    path = settings.state_dir / "worktrees" / thread_id
    path.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "-q", "--detach", str(path), "master")
    if commit_file:
        (path / f"{thread_id}.txt").write_text("x\n", encoding="utf-8")
        commit(path, thread_id)
    return path


def test_uncommitted_worktree_kept(clean, repo, prune_settings):
    with store.connect(TEST_URL) as conn:
        thread_id, message_id = closed_thread(conn, repo)
    worktree = add_worktree(repo, prune_settings, thread_id, commit_file=True)
    make_dirty(worktree)
    add_files(prune_settings, thread_id, message_id)

    plan = prune.run(prune_settings, keep=0)

    assert plan.failures == []
    assert worktree.exists()
    assert (worktree / "dirty.txt").is_file()
    assert (prune_settings.artifacts_dir / message_id).is_dir()
    assert (prune_settings.state_dir / "threads" / f"{thread_id}.json").is_file()
    assert any("uncommitted changes: 1 files" in warning for warning in plan.skipped)
    assert [path for path, _ in plan.dirty] == [worktree]


def test_staged_worktree_kept(clean, repo, prune_settings):
    with store.connect(TEST_URL) as conn:
        thread_id, message_id = closed_thread(conn, repo)
    worktree = add_worktree(repo, prune_settings, thread_id, commit_file=True)
    make_dirty(worktree, staged=True)
    add_files(prune_settings, thread_id, message_id)

    plan = prune.run(prune_settings, keep=0)

    assert plan.failures == []
    assert worktree.exists()
    assert (worktree / "dirty.txt").is_file()
    assert (prune_settings.artifacts_dir / message_id).is_dir()
    assert (prune_settings.state_dir / "threads" / f"{thread_id}.json").is_file()
    assert any("uncommitted changes: 1 files" in warning for warning in plan.skipped)


def test_only_ignored_files_pruned(clean, repo, prune_settings):
    (repo / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    commit(repo, "ignore .venv")
    with store.connect(TEST_URL) as conn:
        thread_id, message_id = closed_thread(conn, repo)
    worktree = add_worktree(repo, prune_settings, thread_id, commit_file=True)
    ignored = worktree / ".venv"
    ignored.mkdir()
    (ignored / "state").write_text("x\n", encoding="utf-8")
    add_files(prune_settings, thread_id, message_id)

    plan = prune.run(prune_settings, keep=0)

    assert plan.failures == []
    assert not worktree.exists()
    assert not (prune_settings.artifacts_dir / message_id).exists()
    assert not (prune_settings.state_dir / "threads" / f"{thread_id}.json").exists()
    assert plan.dirty == []


def test_force_dirty_removes_uncommitted_worktree(clean, repo, prune_settings):
    with store.connect(TEST_URL) as conn:
        thread_id, message_id = closed_thread(conn, repo)
    worktree = add_worktree(repo, prune_settings, thread_id, commit_file=True)
    make_dirty(worktree)
    add_files(prune_settings, thread_id, message_id)

    plan = prune.run(prune_settings, keep=0, force_dirty=True)

    assert plan.failures == []
    assert not worktree.exists()
    assert not (prune_settings.artifacts_dir / message_id).exists()
    assert not (prune_settings.state_dir / "threads" / f"{thread_id}.json").exists()
    assert plan.dirty == []


def test_detached_head_kept_only_when_unmerged(clean, repo, prune_settings):
    with store.connect(TEST_URL) as conn:
        merged_id, merged_message = closed_thread(conn, repo, sender="smoke-a")
        unmerged_id, unmerged_message = closed_thread(conn, repo, sender="smoke-b")
    merged_worktree = add_detached_worktree(repo, prune_settings, merged_id)
    unmerged_worktree = add_detached_worktree(repo, prune_settings, unmerged_id, commit_file=True)
    add_files(prune_settings, merged_id, merged_message)
    add_files(prune_settings, unmerged_id, unmerged_message)

    plan = prune.run(prune_settings, keep=0)

    assert plan.failures == []
    assert not merged_worktree.exists()
    assert not (prune_settings.artifacts_dir / merged_message).exists()
    assert not (prune_settings.state_dir / "threads" / f"{merged_id}.json").exists()

    assert unmerged_worktree.exists()
    assert (prune_settings.artifacts_dir / unmerged_message).is_dir()
    assert (prune_settings.state_dir / "threads" / f"{unmerged_id}.json").is_file()
    assert any("detached HEAD with unmerged commits" in warning for warning in plan.skipped)
