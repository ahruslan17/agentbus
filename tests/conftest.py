"""Shared fixtures: a real Postgres, a clean schema per test, a TestClient."""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
import warnings
from collections.abc import Iterator

import psycopg
import pytest
from fastapi.testclient import TestClient

from agentbus import store
from agentbus.api import create_app
from agentbus.config import Settings

POSTGRES_URL = "postgresql://agentbus:agentbus@127.0.0.1:5455"
TEST_DATABASE_URL_ENV = "AGENTBUS_TEST_DATABASE_URL"
PER_RUN_DB_NAME = f"agentbus_test_{os.getpid()}_{uuid.uuid4().hex[:6]}"

# Parallel runs in separate worktrees must not share one database: messages and
# `truncate` from one run would corrupt another. Every run therefore gets its own
# database unless the caller pins one explicitly.
OWN_DATABASE = TEST_DATABASE_URL_ENV not in os.environ
TEST_URL = os.environ.get(TEST_DATABASE_URL_ENV, f"{POSTGRES_URL}/{PER_RUN_DB_NAME}")
ADMIN_URL = TEST_URL.rsplit("/", 1)[0] + "/postgres"
DB_NAME = TEST_URL.rsplit("/", 1)[1]
TOKEN = "test-token"


def _drop_database() -> None:
    """Drop this run's database; a failure here must not fail the test run."""
    try:
        with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
            conn.execute(f'drop database if exists "{DB_NAME}" with (force)')
    except psycopg.Error as exc:  # pragma: no cover - cleanup must never fail the run
        warnings.warn(f"could not drop test database {DB_NAME}: {exc}", stacklevel=2)


@pytest.fixture(scope="session", autouse=True)
def database() -> Iterator[None]:
    try:
        with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
            if (
                conn.execute("select 1 from pg_database where datname = %s", (DB_NAME,)).fetchone()
                is None
            ):
                conn.execute(f'create database "{DB_NAME}"')
    except psycopg.OperationalError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"postgres is not available: {exc}")
    try:
        with store.connect(TEST_URL) as conn:
            store.init_schema(conn)
        yield
    finally:
        if OWN_DATABASE:
            _drop_database()


@pytest.fixture()
def clean(database: None) -> None:
    with store.connect(TEST_URL) as conn:
        conn.execute("truncate messages, threads, events, workers cascade")


@pytest.fixture()
def artifacts() -> str:
    path = tempfile.mkdtemp(prefix="agentbus-test-")
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture()
def settings(clean: None, artifacts: str) -> Settings:
    return Settings(
        database_url=TEST_URL,
        token=TOKEN,
        lease_ttl_seconds=60,
        per_sender_leases=0,
        max_wait_seconds=1,
        bind_host="127.0.0.1",
        bind_port=0,
        artifacts_dir=artifacts,
        allowed_hosts=("testserver",),
    )


@pytest.fixture()
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings), headers={"Authorization": f"Bearer {TOKEN}"})
