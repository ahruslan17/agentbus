"""Shared fixtures: a real Postgres, a clean schema per test, a TestClient."""

from __future__ import annotations

import os
import shutil
import tempfile

import psycopg
import pytest
from fastapi.testclient import TestClient

from agentbus import store
from agentbus.api import create_app
from agentbus.config import Settings

TEST_URL = os.environ.get(
    "AGENTBUS_TEST_DATABASE_URL", "postgresql://agentbus:agentbus@127.0.0.1:5455/agentbus_test"
)
ADMIN_URL = TEST_URL.rsplit("/", 1)[0] + "/postgres"
DB_NAME = TEST_URL.rsplit("/", 1)[1]
TOKEN = "test-token"


@pytest.fixture(scope="session", autouse=True)
def database() -> None:
    try:
        with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
            if (
                conn.execute("select 1 from pg_database where datname = %s", (DB_NAME,)).fetchone()
                is None
            ):
                conn.execute(f'create database "{DB_NAME}"')
    except psycopg.OperationalError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"postgres is not available: {exc}")
    with store.connect(TEST_URL) as conn:
        store.init_schema(conn)


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
    )


@pytest.fixture()
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings), headers={"Authorization": f"Bearer {TOKEN}"})
