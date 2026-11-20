"""Configuration is read from the environment only, no config files."""

from __future__ import annotations

import os
from dataclasses import dataclass

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
    max_wait_seconds: int
    bind_host: str
    bind_port: int
    artifacts_dir: str

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            database_url=os.environ.get("AGENTBUS_DATABASE_URL", DEFAULT_DATABASE_URL),
            token=_required("AGENTBUS_TOKEN"),
            lease_ttl_seconds=_int("AGENTBUS_LEASE_TTL", 900),
            max_wait_seconds=_int("AGENTBUS_MAX_WAIT", 120),
            bind_host=os.environ.get("AGENTBUS_BIND_HOST", "127.0.0.1"),
            bind_port=_int("AGENTBUS_BIND_PORT", 8787),
            artifacts_dir=os.environ.get(
                "AGENTBUS_ARTIFACTS_DIR", "~/.local/state/agentbus/artifacts"
            ),
        )


@dataclass(frozen=True)
class BridgeSettings:
    share_dir: str
    agent: str
    url: str
    token: str
    state_dir: str
    interval: float

    @classmethod
    def from_env(cls) -> BridgeSettings:
        share = os.environ.get("AGENTBUS_SHARE", "").strip()
        if not share:
            raise RuntimeError("AGENTBUS_SHARE is required (path of the shared directory)")
        return cls(
            share_dir=os.path.expanduser(share),
            agent=os.environ.get("AGENTBUS_BRIDGE_AGENT", "cage-claude"),
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

    @classmethod
    def from_env(cls) -> WorkerSettings:
        repos = tuple(
            part.strip() for part in os.environ.get("AGENTBUS_REPOS", "").split(",") if part.strip()
        )
        if not repos:
            raise RuntimeError("AGENTBUS_REPOS is required (comma separated allowed prefixes)")
        return cls(
            url=os.environ.get("AGENTBUS_URL", "http://127.0.0.1:8787").rstrip("/"),
            token=_required("AGENTBUS_TOKEN"),
            agent=os.environ.get("AGENTBUS_AGENT", "local-opencode"),
            model=os.environ.get("AGENTBUS_MODEL", "opencode-go/deepseek-v4.1-flash"),
            repos=repos,
            state_dir=os.path.expanduser(
                os.environ.get("AGENTBUS_STATE_DIR", "~/.local/state/agentbus")
            ),
            task_timeout_seconds=_int("AGENTBUS_TASK_TIMEOUT", 2400),
            lease_ttl_seconds=_int("AGENTBUS_LEASE_TTL", 900),
            opencode_bin=os.environ.get("AGENTBUS_OPENCODE_BIN", "opencode"),
            extra_instructions=os.environ.get("AGENTBUS_POLICY", ""),
        )
