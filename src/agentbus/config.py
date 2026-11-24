"""Configuration is read from the environment only, no config files."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DATABASE_URL = "postgresql://agentbus:agentbus@127.0.0.1:5455/agentbus"


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    database_url: str
    token: str
    lease_ttl_seconds: int
    per_sender_leases: int
    max_wait_seconds: int
    bind_host: str
    bind_port: int
    artifacts_dir: str
    # Hosts accepted in the HTTP `Host` header (DNS rebinding guard). Empty means
    # "derive from bind_port"; kept defaulted so older constructors still work.
    allowed_hosts: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> Settings:
        port = _int("AGENTBUS_BIND_PORT", 8787)
        raw_hosts = os.environ.get("AGENTBUS_ALLOWED_HOSTS", "").strip()
        allowed_hosts = (
            tuple(part.strip() for part in raw_hosts.split(",") if part.strip())
            if raw_hosts
            else (f"127.0.0.1:{port}", f"localhost:{port}")
        )
        return cls(
            database_url=os.environ.get("AGENTBUS_DATABASE_URL", DEFAULT_DATABASE_URL),
            token=_required("AGENTBUS_TOKEN"),
            lease_ttl_seconds=_int("AGENTBUS_LEASE_TTL", 900),
            per_sender_leases=_int("AGENTBUS_PER_SENDER_LEASES", 0),
            max_wait_seconds=_int("AGENTBUS_MAX_WAIT", 120),
            bind_host=os.environ.get("AGENTBUS_BIND_HOST", "127.0.0.1"),
            bind_port=port,
            artifacts_dir=os.environ.get(
                "AGENTBUS_ARTIFACTS_DIR", "~/.local/state/agentbus/artifacts"
            ),
            allowed_hosts=allowed_hosts,
        )


@dataclass(frozen=True)
class BridgeSettings:
    share_root: str
    url: str
    token: str
    state_dir: str
    interval: float

    @classmethod
    def from_env(cls) -> BridgeSettings:
        # AGENTBUS_SHARE_ROOT wins; otherwise the root is the parent of the old
        # single-mailbox AGENTBUS_SHARE, so existing env files keep working.
        root = os.environ.get("AGENTBUS_SHARE_ROOT", "").strip()
        if not root:
            share = os.environ.get("AGENTBUS_SHARE", "").strip()
            if share:
                root = str(Path(share).expanduser().parent)
        if not root:
            raise RuntimeError(
                "AGENTBUS_SHARE_ROOT or AGENTBUS_SHARE is required (path of the mailboxes root)"
            )
        return cls(
            share_root=os.path.expanduser(root),
            url=os.environ.get("AGENTBUS_URL", "http://127.0.0.1:8787").rstrip("/"),
            token=_required("AGENTBUS_TOKEN"),
            state_dir=os.path.expanduser(
                os.environ.get("AGENTBUS_STATE_DIR", "~/.local/state/agentbus")
            ),
            interval=float(os.environ.get("AGENTBUS_BRIDGE_INTERVAL", "2")),
        )


@dataclass(frozen=True)
class WorkerSettings:
    url: str
    token: str
    agent: str
    model: str
    repos: tuple[str, ...]
    state_dir: str
    task_timeout_seconds: int
    lease_ttl_seconds: int
    opencode_bin: str
    extra_instructions: str
    slots: int
    stop_grace_seconds: int
    commit_author: str

    @classmethod
    def from_env(cls) -> WorkerSettings:
        repos = tuple(
            part.strip() for part in os.environ.get("AGENTBUS_REPOS", "").split(",") if part.strip()
        )
        if not repos:
            raise RuntimeError("AGENTBUS_REPOS is required (comma separated allowed prefixes)")
        task_timeout = _int("AGENTBUS_TASK_TIMEOUT", 2400)
        return cls(
            url=os.environ.get("AGENTBUS_URL", "http://127.0.0.1:8787").rstrip("/"),
            token=_required("AGENTBUS_TOKEN"),
            agent=os.environ.get("AGENTBUS_AGENT", "local-opencode"),
            model=os.environ.get("AGENTBUS_MODEL", "opencode-go/deepseek-v4.1-flash"),
            repos=repos,
            state_dir=os.path.expanduser(
                os.environ.get("AGENTBUS_STATE_DIR", "~/.local/state/agentbus")
            ),
            task_timeout_seconds=task_timeout,
            lease_ttl_seconds=_int("AGENTBUS_LEASE_TTL", 900),
            opencode_bin=os.environ.get("AGENTBUS_OPENCODE_BIN", "opencode"),
            extra_instructions=os.environ.get("AGENTBUS_POLICY", ""),
            slots=max(1, _int("AGENTBUS_WORKER_SLOTS", 1)),
            stop_grace_seconds=_int("AGENTBUS_STOP_GRACE", task_timeout),
            commit_author=os.environ.get(
                "AGENTBUS_COMMIT_AUTHOR", "agentbus executor <agentbus@localhost>"
            ),
        )
