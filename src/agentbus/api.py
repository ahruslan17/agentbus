"""HTTP surface. Thin: validation, auth, path guards. All SQL is in `store`."""

from __future__ import annotations

import secrets
import time
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi import Path as PathParam
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agentbus import runlog, store
from agentbus.config import Settings

MAX_FILE_BYTES = 256 * 1024
MAX_LOG_BYTES = 2 * 1024 * 1024
POLL_INTERVAL = 0.25
WORKER_ID_PATTERN = r"^[A-Za-z0-9._#-]+$"
UI_DIR = Path(__file__).resolve().parent / "ui"
TOKEN_COOKIE = "agentbus_token"


class SendRequest(BaseModel):
    sender: str = Field(min_length=1, max_length=64)
    recipient: str = Field(min_length=1, max_length=64)
    kind: str = "task"
    body: str = Field(min_length=1)
    thread_id: str | None = None
    repo: str | None = None
    base_ref: str | None = None
    parent_id: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=200)


class ReplyRequest(BaseModel):
    sender: str = Field(min_length=1, max_length=64)
    kind: str = "result"
    body: str = Field(min_length=1)
    meta: dict[str, Any] = Field(default_factory=dict)


class StateRequest(BaseModel):
    state: str
    note: str | None = None


class WorkerRequest(BaseModel):
    agent: str = Field(min_length=1, max_length=64)
    host: str | None = None
    pid: int | None = Field(default=None, ge=0)
    model: str | None = None
    harness: str | None = None
    state: Literal["idle", "busy", "stopping", "stopped"]
    message_id: str | None = None


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="agentbus", version="0.1.0")
    app.state.settings = settings
    app.state.artifacts_dir = Path(settings.artifacts_dir).expanduser()
    allowed_hosts = set(_allowed_hosts(settings))

    def current_settings() -> Settings:
        return app.state.settings

    @app.middleware("http")
    async def enforce_host(request: Request, call_next):
        # Bind to loopback is not enough: a browser can be tricked into resolving a
        # foreign name to 127.0.0.1 (DNS rebinding). Only our own names may talk here.
        host = request.headers.get("host", "")
        if host not in allowed_hosts:
            return JSONResponse({"detail": "bad host"}, status_code=421)
        return await call_next(request)

    def authorized(request: Request, authorization: Annotated[str | None, Header()] = None) -> None:
        expected = f"Bearer {current_settings().token}"
        if authorization and secrets.compare_digest(authorization, expected):
            return
        # The browser session may only read; writes still need the Bearer token.
        if request.method == "GET":
            cookie = request.cookies.get(TOKEN_COOKIE, "")
            if cookie and secrets.compare_digest(cookie, current_settings().token):
                return
        raise HTTPException(status_code=401, detail="bad token")

    guard = Depends(authorized)

    @app.get("/ui/login")
    def ui_login(token: str = Query(default="")):
        if not token or not secrets.compare_digest(token, current_settings().token):
            raise HTTPException(status_code=401, detail="bad token")
        response = RedirectResponse(url="/ui/", status_code=303)
        response.set_cookie(
            TOKEN_COOKIE,
            current_settings().token,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return response

    @app.get("/v1/health")
    def health() -> dict[str, Any]:
        try:
            with store.connect(current_settings().database_url) as conn:
                conn.execute("select 1")
            return {"ok": True, "db": "up"}
        except Exception as exc:  # noqa: BLE001 - health must report, not raise
            return {"ok": False, "db": "down", "error": str(exc)}

    @app.post("/v1/messages", status_code=201, dependencies=[guard])
    def send(payload: SendRequest) -> dict[str, Any]:
        with store.connect(current_settings().database_url) as conn:
            try:
                return store.create_message(conn, **payload.model_dump())
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/v1/messages/{message_id}", dependencies=[guard])
    def show(message_id: str) -> dict[str, Any]:
        with store.connect(current_settings().database_url) as conn:
            row = store.get_message(conn, message_id)
            if row is None:
                raise HTTPException(status_code=404, detail="unknown message")
            return row

    @app.get("/v1/threads/{thread_id}", dependencies=[guard])
    def thread(thread_id: str) -> dict[str, Any]:
        with store.connect(current_settings().database_url) as conn:
            head = store.get_thread(conn, thread_id)
            if head is None:
                raise HTTPException(status_code=404, detail="unknown thread")
            return {"thread": head, "messages": store.thread_messages(conn, thread_id)}

    @app.get("/v1/inbox/{agent}", dependencies=[guard])
    def list_inbox(agent: str, limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
        with store.connect(current_settings().database_url) as conn:
            return {"messages": store.inbox(conn, agent=agent, limit=limit)}

    @app.get("/v1/threads/{thread_id}/events", dependencies=[guard])
    def thread_events(thread_id: str) -> dict[str, Any]:
        with store.connect(current_settings().database_url) as conn:
            return {"events": store.thread_events(conn, thread_id)}

    @app.get("/v1/events", dependencies=[guard])
    def events(
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        with store.connect(current_settings().database_url) as conn:
            return {"events": store.recent_events(conn, after_id=after, limit=limit)}

    @app.put("/v1/workers/{worker_id}", dependencies=[guard])
    def put_worker(
        payload: WorkerRequest,
        worker_id: Annotated[str, PathParam(max_length=64, pattern=WORKER_ID_PATTERN)],
    ) -> dict[str, Any]:
        with store.connect(current_settings().database_url) as conn:
            return store.upsert_worker(conn, id=worker_id, **payload.model_dump())

    @app.get("/v1/workers", dependencies=[guard])
    def workers() -> dict[str, Any]:
        with store.connect(current_settings().database_url) as conn:
            return {
                "workers": store.list_workers(
                    conn, lease_ttl_seconds=current_settings().lease_ttl_seconds
                )
            }

    @app.post("/v1/inbox/{agent}/lease", dependencies=[guard])
    def lease(
        agent: str,
        wait: int = Query(default=0, ge=0, le=300),
        kinds: str | None = Query(default=None, description="comma separated kinds"),
        owner: str | None = Query(
            default=None,
            max_length=64,
            pattern=r"^[A-Za-z0-9._#-]+$",
            description="lease owner, e.g. local-opencode#2; defaults to agent",
        ),
    ):
        wanted = tuple(k.strip() for k in kinds.split(",") if k.strip()) if kinds else None
        ttl = current_settings().lease_ttl_seconds
        deadline = time.monotonic() + min(wait, current_settings().max_wait_seconds)
        while True:
            with store.connect(current_settings().database_url) as conn:
                row = store.lease_message(
                    conn,
                    agent=agent,
                    ttl_seconds=ttl,
                    kinds=wanted,
                    owner=owner,
                    per_sender_limit=current_settings().per_sender_leases,
                )
            if row is not None:
                return jsonable_encoder(row)
            if time.monotonic() >= deadline:
                return Response(status_code=204)
            time.sleep(POLL_INTERVAL)

    @app.post("/v1/messages/{message_id}/reply", status_code=201, dependencies=[guard])
    def reply(message_id: str, payload: ReplyRequest) -> dict[str, Any]:
        with store.connect(current_settings().database_url) as conn:
            try:
                return store.reply(
                    conn,
                    parent_id=message_id,
                    sender=payload.sender,
                    kind=payload.kind,
                    body=payload.body,
                    meta=payload.meta,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/v1/messages/{message_id}/state", dependencies=[guard])
    def set_state(message_id: str, payload: StateRequest) -> dict[str, Any]:
        with store.connect(current_settings().database_url) as conn:
            try:
                row = store.set_state(
                    conn, message_id=message_id, state=payload.state, note=payload.note
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if row is None:
                raise HTTPException(status_code=404, detail="unknown message")
            return row

    @app.post("/v1/messages/{message_id}/heartbeat", dependencies=[guard])
    def heartbeat(message_id: str) -> dict[str, Any]:
        with store.connect(current_settings().database_url) as conn:
            row = store.heartbeat(
                conn, message_id=message_id, ttl_seconds=current_settings().lease_ttl_seconds
            )
            if row is None:
                raise HTTPException(status_code=409, detail="not leased")
            return row

    @app.get("/v1/messages/{message_id}/file", dependencies=[guard])
    def read_file(message_id: str, path: str = Query(default="")) -> dict[str, Any]:
        with store.connect(current_settings().database_url) as conn:
            row = store.get_message(conn, message_id)
            if row is None:
                raise HTTPException(status_code=404, detail="unknown message")
            root = _worktree_root(conn, row)
        if root is None:
            raise HTTPException(status_code=409, detail="message has no worktree or repo")
        target = _resolve_within(root, path)
        if target.is_dir():
            return {
                "path": str(target),
                "kind": "dir",
                "entries": sorted(p.name for p in target.iterdir())[:500],
            }
        if not target.exists():
            raise HTTPException(status_code=404, detail=f"no such file: {path}")
        data = target.read_bytes()
        return {
            "path": str(target),
            "kind": "file",
            "truncated": len(data) > MAX_FILE_BYTES,
            "content": data[:MAX_FILE_BYTES].decode("utf-8", errors="replace"),
        }

    @app.get("/v1/messages/{message_id}/artifact", dependencies=[guard])
    def read_artifact(message_id: str, name: str = Query(...)) -> dict[str, Any]:
        if "/" in name or name in ("", ".", ".."):
            raise HTTPException(status_code=400, detail="bad artifact name")
        path = app.state.artifacts_dir / message_id / name
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"no such artifact: {name}")
        data = path.read_bytes()
        return {
            "name": name,
            "truncated": len(data) > MAX_FILE_BYTES,
            "content": data[:MAX_FILE_BYTES].decode("utf-8", errors="replace"),
        }

    @app.get("/v1/overview", dependencies=[guard])
    def get_overview() -> dict[str, Any]:
        with store.connect(current_settings().database_url) as conn:
            return store.overview(conn, lease_ttl_seconds=current_settings().lease_ttl_seconds)

    @app.get("/v1/mailboxes", dependencies=[guard])
    def get_mailboxes() -> dict[str, Any]:
        with store.connect(current_settings().database_url) as conn:
            return {"mailboxes": store.mailboxes(conn)}

    @app.get("/v1/threads", dependencies=[guard])
    def get_threads(
        mailbox: str | None = Query(default=None, max_length=64),
        repo: str | None = Query(default=None),
        state: Literal["active", "review", "done", "failed"] | None = Query(default=None),
        q: str | None = Query(default=None, max_length=200),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        with store.connect(current_settings().database_url) as conn:
            return {
                "threads": store.list_threads(
                    conn,
                    mailbox=mailbox,
                    repo=repo,
                    state=state,
                    q=q,
                    limit=limit,
                    offset=offset,
                )
            }

    @app.get("/v1/threads/{thread_id}/timeline", dependencies=[guard])
    def get_timeline(thread_id: str) -> dict[str, Any]:
        with store.connect(current_settings().database_url) as conn:
            timeline = store.thread_timeline(conn, thread_id)
        if timeline is None:
            raise HTTPException(status_code=404, detail="unknown thread")
        for message in timeline["messages"]:
            message["artifacts"] = _artifact_files(app.state.artifacts_dir, str(message["id"]))
        return timeline

    @app.get("/v1/messages/{message_id}/log", dependencies=[guard])
    def get_log(message_id: str, after: int = Query(default=0, ge=0)) -> dict[str, Any]:
        with store.connect(current_settings().database_url) as conn:
            row = store.get_message(conn, message_id)
            if row is None:
                raise HTTPException(status_code=404, detail="unknown message")
            running = row["state"] == "leased"
            path = _run_log_path(app.state.artifacts_dir, conn, message_id)
        if path is None:
            return {"lines": [], "next": after, "running": running}
        selected = _read_log_lines(path, after)
        return {
            "lines": runlog.parse_lines(selected, start=after),
            "next": after + len(selected),
            "running": running,
        }

    @app.get("/v1/stats", dependencies=[guard])
    def get_stats(
        group: Literal["mailbox", "repo", "model"] = Query(...),
    ) -> dict[str, Any]:
        with store.connect(current_settings().database_url) as conn:
            return {"group": group, "rows": store.stats(conn, group)}

    app.mount("/ui", StaticFiles(directory=UI_DIR, html=True), name="ui")

    return app


def _allowed_hosts(settings: Settings) -> tuple[str, ...]:
    if settings.allowed_hosts:
        return settings.allowed_hosts
    return (f"127.0.0.1:{settings.bind_port}", f"localhost:{settings.bind_port}")


def _artifact_files(root: Path, message_id: str) -> list[str]:
    directory = root / message_id
    if not directory.is_dir():
        return []
    return sorted(path.name for path in directory.iterdir() if path.is_file())


def _run_log_path(root: Path, conn, message_id: str) -> Path | None:
    direct = root / message_id / "run.log"
    if direct.is_file():
        return direct
    reply_id = store.first_reply_id(conn, message_id)
    if reply_id:
        # The worker moves artifacts under the reply id once the answer exists.
        reply_log = root / reply_id / "run.log"
        if reply_log.is_file():
            return reply_log
    return None


def _read_log_lines(path: Path, after: int) -> list[str]:
    """Read at most MAX_LOG_BYTES of the tail, starting at file line `after`."""
    selected: list[str] = []
    size = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for index, line in enumerate(handle):
            if index < after:
                continue
            grown = size + len(line.encode("utf-8"))
            if selected and grown > MAX_LOG_BYTES:
                break
            selected.append(line)
            size = grown
    return selected


def _worktree_root(conn, row: dict[str, Any]) -> Path | None:
    meta = row.get("meta") or {}
    for key in ("worktree", "repo"):
        raw = meta.get(key)
        if raw:
            return Path(str(raw)).expanduser()
    if row.get("thread_id"):
        thread = store.get_thread(conn, row["thread_id"])
        if thread and thread.get("repo"):
            return Path(str(thread["repo"])).expanduser()
    return None


def _resolve_within(root: Path, relative: str) -> Path:
    root_resolved = root.resolve()
    target = (root_resolved / relative.lstrip("/")).resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise HTTPException(status_code=400, detail="path escapes the worktree")
    return target


def build_app() -> FastAPI:
    """Entry point for `uvicorn agentbus.api:build_app --factory`."""
    return create_app(Settings.from_env())
