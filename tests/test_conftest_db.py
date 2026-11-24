"""The per-run test database: naming, lifetime, and isolation between parallel runs."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

from .conftest import ADMIN_URL, DB_NAME, TEST_DATABASE_URL_ENV, TEST_URL

REPO_ROOT = Path(__file__).resolve().parents[1]


def _database_exists(name: str) -> bool:
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        row = conn.execute("select 1 from pg_database where datname = %s", (name,)).fetchone()
    return row is not None


def _databases_with_prefix(prefix: str) -> list[str]:
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        rows = conn.execute(
            "select datname from pg_database where datname like %s", (f"{prefix}%",)
        ).fetchall()
    return [row[0] for row in rows]


def test_test_url_is_unique_per_run() -> None:
    if TEST_DATABASE_URL_ENV in os.environ:
        pytest.skip(f"{TEST_DATABASE_URL_ENV} pins the database explicitly")
    prefix = f"agentbus_test_{os.getpid()}_"
    name = TEST_URL.rsplit("/", 1)[1]
    assert name.startswith(prefix)
    suffix = name.removeprefix(prefix)
    assert len(suffix) == 6
    assert all(char in "0123456789abcdef" for char in suffix)


def test_database_from_test_url_exists_during_session() -> None:
    assert _database_exists(DB_NAME)
    with psycopg.connect(TEST_URL) as conn:
        current = conn.execute("select current_database()").fetchone()[0]
    assert current == DB_NAME


def test_parallel_runs_use_and_drop_separate_databases() -> None:
    env = {key: value for key, value in os.environ.items() if key != TEST_DATABASE_URL_ENV}
    command = [sys.executable, "-m", "pytest", "tests/test_queue.py", "-q"]
    runs = [
        subprocess.Popen(
            command,
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for _ in range(2)
    ]
    results = [(run, *run.communicate(timeout=300)) for run in runs]

    for run, output, _ in results:
        assert run.returncode == 0, output

    for run, _, _ in results:
        leftover = _databases_with_prefix(f"agentbus_test_{run.pid}_")
        assert leftover == [], f"run {run.pid} left databases behind: {leftover}"
