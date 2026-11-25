"""Delete the on-disk leftovers of long-closed threads, keeping the journal intact.

A thread is closed when it has no `queued` or `leased` messages: nothing can start in
its worktree any more. The newest closed threads stay (`--keep`, default 50); for the
rest this removes

- `<state_dir>/worktrees/<thread>` via `git worktree remove --force`,
- `<artifacts_dir>/<message_id>` for its messages,
- `<state_dir>/threads/<thread>.json` (the thread base commit),

and, with `--delete-merged-branches`, the branch `agentbus/<thread>` if it is fully
merged into the repository's `master`/`main` (`git branch -d`, never `-D`).

A worktree with unsaved work is kept together with its artifacts and base file: its
`git status` is not clean, or its HEAD is detached and not an ancestor of
`master`/`main`. `--force-dirty` removes such worktrees anyway (as older versions
did); in a dry run the ignored reasons are still printed.

Rows in Postgres are never touched. `--dry-run` prints the same plan and does nothing.
Repositories outside `AGENTBUS_REPOS` are skipped with a warning: the state directory
is ours, the repository may belong to another owner.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from agentbus import store
from agentbus.config import DEFAULT_DATABASE_URL

DEFAULT_KEEP = 50
BUSY_STATES = ("queued", "leased")
BASE_BRANCHES = ("master", "main")


@dataclass(frozen=True)
class PruneSettings:
    database_url: str
    state_dir: Path
    artifacts_dir: Path
    repos: tuple[Path, ...]

    @classmethod
    def from_env(cls) -> PruneSettings:
        raw_repos = os.environ.get("AGENTBUS_REPOS", "")
        return cls(
            database_url=os.environ.get("AGENTBUS_DATABASE_URL", DEFAULT_DATABASE_URL),
            state_dir=Path(
                os.environ.get("AGENTBUS_STATE_DIR", "~/.local/state/agentbus")
            ).expanduser(),
            artifacts_dir=Path(
                os.environ.get("AGENTBUS_ARTIFACTS_DIR", "~/.local/state/agentbus/artifacts")
            ).expanduser(),
            repos=tuple(
                Path(part.strip()).expanduser() for part in raw_repos.split(",") if part.strip()
            ),
        )


@dataclass(frozen=True)
class ThreadRecord:
    """A closed thread and the messages whose artifacts belong to it."""

    id: str
    repo: str | None
    message_ids: tuple[str, ...]


@dataclass
class Plan:
    worktrees: list[tuple[Path, Path]] = field(default_factory=list)  # (repo, path)
    artifact_dirs: list[Path] = field(default_factory=list)
    base_files: list[Path] = field(default_factory=list)
    branches: list[tuple[Path, str]] = field(default_factory=list)  # (repo, branch)
    skipped: list[str] = field(default_factory=list)
    dirty: list[tuple[Path, str]] = field(default_factory=list)  # (worktree, reason)
    failures: list[str] = field(default_factory=list)


def closed_threads(conn) -> list[ThreadRecord]:
    """Closed threads, newest first, with the ids of all their messages."""
    rows = conn.execute(
        """
        select t.id, t.repo,
               array_agg(m.id order by m.created_at, m.id) as message_ids,
               max(m.updated_at) as updated_at
          from threads t
          join messages m on m.thread_id = t.id
         where not exists (
               select 1 from messages busy
                where busy.thread_id = t.id
                  and busy.state = any(%(busy_states)s))
         group by t.id, t.repo
         order by max(m.updated_at) desc, t.id desc
        """,
        {"busy_states": list(BUSY_STATES)},
    ).fetchall()
    return [
        ThreadRecord(
            id=str(row["id"]),
            repo=row["repo"],
            message_ids=tuple(str(message_id) for message_id in row["message_ids"]),
        )
        for row in rows
    ]


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def _allowed_repo(settings: PruneSettings, raw: str | None) -> Path | None:
    """The repository path if it is inside `AGENTBUS_REPOS`, otherwise `None`."""
    if not raw:
        return None
    repo = _resolved(Path(raw).expanduser())
    for prefix in settings.repos:
        allowed = _resolved(prefix)
        if repo == allowed or allowed in repo.parents:
            return repo
    return None


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def _branch_exists(repo: Path, branch: str) -> bool:
    result = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    return result.returncode == 0


def _merged(repo: Path, branch: str) -> bool:
    """True when `branch` is an ancestor of the repository's master or main."""
    for base in BASE_BRANCHES:
        if not _branch_exists(repo, base):
            continue
        if _git(repo, "merge-base", "--is-ancestor", branch, base).returncode == 0:
            return True
    return False


def _merged_head(repo: Path) -> bool:
    """True when the commit checked out in `repo` is an ancestor of master or main."""
    for base in BASE_BRANCHES:
        if not _branch_exists(repo, base):
            continue
        if _git(repo, "merge-base", "--is-ancestor", "HEAD", base).returncode == 0:
            return True
    return False


def _dirty_reason(worktree: Path) -> str | None:
    """Why `worktree` holds work that removal would lose, or `None` when it is safe."""
    status = _git(worktree, "status", "--porcelain", "--untracked-files=normal")
    if status.returncode != 0:
        return status.stderr.strip() or f"git status exited {status.returncode}"
    changed = [line for line in status.stdout.splitlines() if line.strip()]
    if changed:
        return f"uncommitted changes: {len(changed)} files"

    head = _git(worktree, "rev-parse", "--abbrev-ref", "HEAD")
    if head.returncode != 0:
        return head.stderr.strip() or f"git rev-parse exited {head.returncode}"
    if head.stdout.strip() == "HEAD" and not _merged_head(worktree):
        return "detached HEAD with unmerged commits"
    return None


def build_plan(
    settings: PruneSettings,
    records: list[ThreadRecord],
    *,
    delete_merged_branches: bool,
    force_dirty: bool = False,
    dry_run: bool = False,
) -> Plan:
    plan = Plan()
    for record in records:
        repo = _allowed_repo(settings, record.repo)
        worktree = settings.state_dir / "worktrees" / record.id
        if worktree.exists():
            if repo is None:
                reason = (
                    "threads.repo is empty"
                    if not record.repo
                    else f"repo outside AGENTBUS_REPOS: {record.repo}"
                )
                plan.skipped.append(f"worktree {worktree}: {reason}")
            else:
                # The check is off with --force-dirty, but a dry run still reports it.
                reason = _dirty_reason(worktree) if dry_run or not force_dirty else None
                if reason and not force_dirty:
                    plan.dirty.append((worktree, reason))
                    plan.skipped.append(f"worktree {worktree}: {reason}")
                    # Artifacts and the base commit are the only other ways back.
                    continue
                if reason and dry_run:
                    plan.skipped.append(f"worktree {worktree}: {reason} (ignored by --force-dirty)")
                plan.worktrees.append((repo, worktree))

        for message_id in record.message_ids:
            artifacts = settings.artifacts_dir / message_id
            if artifacts.is_dir():
                plan.artifact_dirs.append(artifacts)

        base = settings.state_dir / "threads" / f"{record.id}.json"
        if base.is_file():
            plan.base_files.append(base)

        if delete_merged_branches and repo is not None:
            branch = f"agentbus/{record.id}"
            if not _branch_exists(repo, branch):
                continue
            if _merged(repo, branch):
                plan.branches.append((repo, branch))
            else:
                plan.skipped.append(f"branch {branch} in {repo}: not merged into master/main")
    return plan


def _remove_worktree(repo: Path, path: Path, *, dry_run: bool) -> None:
    if dry_run:
        print(f"would remove worktree {path}")
        return
    result = _git(repo, "worktree", "remove", "--force", str(path))
    if result.returncode == 0:
        print(f"removed worktree {path}")
    else:
        raise RuntimeError(
            result.stderr.strip() or f"git worktree remove exited {result.returncode}"
        )


def _remove_tree(path: Path, *, dry_run: bool) -> None:
    if dry_run:
        print(f"would remove artifacts {path}")
        return
    shutil.rmtree(path)
    print(f"removed artifacts {path}")


def _remove_file(path: Path, *, dry_run: bool) -> None:
    if dry_run:
        print(f"would remove base file {path}")
        return
    path.unlink()
    print(f"removed base file {path}")


def _delete_branch(repo: Path, branch: str, *, dry_run: bool) -> None:
    if dry_run:
        print(f"would delete branch {branch} in {repo}")
        return
    result = _git(repo, "branch", "-d", branch)
    if result.returncode == 0:
        print(f"deleted branch {branch} in {repo}")
    else:
        raise RuntimeError(result.stderr.strip() or f"git branch -d exited {result.returncode}")


def apply_plan(plan: Plan, *, dry_run: bool) -> None:
    """Run the plan; one failed step is recorded and the rest still run."""
    for repo, path in plan.worktrees:
        try:
            _remove_worktree(repo, path, dry_run=dry_run)
        except (RuntimeError, OSError) as exc:
            plan.failures.append(f"worktree {path}: {exc}")
    for path in plan.artifact_dirs:
        try:
            _remove_tree(path, dry_run=dry_run)
        except OSError as exc:
            plan.failures.append(f"artifacts {path}: {exc}")
    for path in plan.base_files:
        try:
            _remove_file(path, dry_run=dry_run)
        except OSError as exc:
            plan.failures.append(f"base file {path}: {exc}")
    for repo, branch in plan.branches:
        try:
            _delete_branch(repo, branch, dry_run=dry_run)
        except (RuntimeError, OSError) as exc:
            plan.failures.append(f"branch {branch}: {exc}")


def run(
    settings: PruneSettings,
    *,
    keep: int = DEFAULT_KEEP,
    dry_run: bool = False,
    delete_merged_branches: bool = False,
    force_dirty: bool = False,
) -> Plan:
    with store.connect(settings.database_url) as conn:
        records = closed_threads(conn)
    selected = records[keep:]
    print(
        f"{len(records)} closed threads, keeping {min(keep, len(records))}, "
        f"pruning {len(selected)}{' (dry run)' if dry_run else ''}"
    )
    plan = build_plan(
        settings,
        selected,
        delete_merged_branches=delete_merged_branches,
        force_dirty=force_dirty,
        dry_run=dry_run,
    )
    apply_plan(plan, dry_run=dry_run)
    for warning in plan.skipped:
        print(f"skipped {warning}", file=sys.stderr)
    for failure in plan.failures:
        print(f"failed to remove {failure}", file=sys.stderr)
    print(f"skipped {len(plan.dirty)} dirty worktrees")
    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agentbus.prune",
        description="Delete worktrees, artifacts and base files of closed threads.",
    )
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP, help="closed threads to keep")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, delete nothing")
    parser.add_argument(
        "--delete-merged-branches",
        action="store_true",
        help="also `git branch -d` thread branches merged into master/main",
    )
    parser.add_argument(
        "--force-dirty",
        action="store_true",
        help="remove worktrees with uncommitted changes or an unmerged detached HEAD",
    )
    args = parser.parse_args(argv)
    if args.keep < 0:
        parser.error("--keep must be 0 or more")

    plan = run(
        PruneSettings.from_env(),
        keep=args.keep,
        dry_run=args.dry_run,
        delete_merged_branches=args.delete_merged_branches,
        force_dirty=args.force_dirty,
    )
    return 1 if plan.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
