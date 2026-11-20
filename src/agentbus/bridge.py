"""File bridge: lets an isolated VM agent use the queue without any network access.

The VM only ever reads and writes files inside a shared directory. This process, which
runs on the host next to the API, is the only side that speaks HTTP.

AGENTBUS_SHARE is the agent's own folder inside the share and has the same value in the
host and inside the VM, so there is no second guess about paths. Layout:

    <share>/cage-claude/
        outbox/              # VM drops <anything>.json here -> becomes a message
        sent/                # bridge moved accepted files here
        failed/              # bridge moved rejected files here (+ .error)
        inbox/<message_id>/  # bridge delivers a message here
            message.json
            patch.diff, checks.txt, run.log ...   (artifacts from the worker)
        read/                # the VM moves handled directories here (optional)

Delivery is idempotent: the outbox file name becomes the idempotency key, and delivered
messages are recorded on the host side, so a restart never duplicates anything.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

import httpx

from agentbus.config import BridgeSettings

MAX_INBOX_BYTES = 5 * 1024 * 1024


def log(message: str) -> None:
    print(f"[agentbus-bridge] {time.strftime('%H:%M:%S')} {message}", flush=True)


class Api:
    def __init__(self, url: str, token: str) -> None:
        self.http = httpx.Client(
            base_url=url, headers={"Authorization": f"Bearer {token}"}, timeout=60.0
        )

    def close(self) -> None:
        self.http.close()

    def post(self, path: str, json: dict[str, Any]):  # type: ignore[no-untyped-def]
        return self.http.post(path, json=json)

    def get(self, path: str, params: dict[str, Any] | None = None):  # type: ignore[no-untyped-def]
        return self.http.get(path, params=params)


class Bridge:
    def __init__(self, settings: BridgeSettings, api: Any) -> None:
        self.settings = settings
        self.api = api
        self.agent = settings.agent
        self.root = Path(settings.share_dir).expanduser()
        self.outbox = self.root / "outbox"
        self.sent = self.root / "sent"
        self.failed = self.root / "failed"
        self.inbox = self.root / "inbox"
        self.markers = Path(settings.state_dir).expanduser() / "bridge" / self.agent / "delivered"
        for path in (self.outbox, self.sent, self.failed, self.inbox, self.markers):
            path.mkdir(parents=True, exist_ok=True)

    # --- VM -> queue -----------------------------------------------------------

    def pump_outbox(self) -> int:
        sent = 0
        for path in sorted(self.outbox.glob("*.json")):
            if path.stat().st_size == 0:
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                self._reject(path, f"bad json: {exc}")
                continue
            if not isinstance(payload, dict):
                self._reject(path, "expected a json object")
                continue

            payload.setdefault("sender", self.agent)
            key = f"{self.agent}:{path.stem}"
            payload["idempotency_key"] = key
            try:
                response = self.api.post("/v1/messages", json=payload)
            except httpx.HTTPError as exc:
                log(f"{path.name}: api unavailable ({exc}), will retry")
                continue

            if response.status_code in (200, 201):
                body = response.json()
                log(
                    f"{path.name} -> {body.get('id')} {body.get('kind')} -> {body.get('recipient')}"
                )
                self._move(path, self.sent)
                sent += 1
            elif response.status_code >= 500:
                log(f"{path.name}: api error {response.status_code}, will retry")
            else:
                self._reject(path, f"{response.status_code}: {response.text[:500]}")
        return sent

    def _reject(self, path: Path, reason: str) -> None:
        log(f"{path.name} rejected: {reason}")
        target = self._move(path, self.failed)
        (target.with_suffix(".error")).write_text(reason + "\n", encoding="utf-8")

    def _move(self, path: Path, directory: Path) -> Path:
        target = _unique(directory / path.name)
        os.replace(path, target)
        return target

    # --- queue -> VM -----------------------------------------------------------

    def pump_inbox(self) -> int:
        try:
            response = self.api.get(f"/v1/inbox/{self.agent}")
        except httpx.HTTPError as exc:
            log(f"inbox fetch failed: {exc}")
            return 0
        if response.status_code != 200:
            log(f"inbox fetch failed: {response.status_code}")
            return 0

        delivered = 0
        for message in response.json().get("messages", []):
            message_id = str(message["id"])
            marker = self.markers / message_id
            if marker.exists():
                # The directory can disappear if the share was moved or cleaned:
                # a marker alone would keep the message queued forever.
                if (self.inbox / message_id).is_dir() or (self.root / "read" / message_id).is_dir():
                    continue
                marker.unlink()
            target = self.inbox / message_id
            staging = self.inbox / f".{message_id}.tmp"
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True)
            artifacts = (message.get("meta") or {}).get("artifacts") or []
            saved: list[str] = []
            for name in artifacts:
                data = self._artifact(message_id, str(name))
                if data is None:
                    continue
                (staging / str(name)).write_text(data, encoding="utf-8")
                saved.append(str(name))
            envelope = {
                "id": message_id,
                "thread_id": message.get("thread_id"),
                "sender": message.get("sender"),
                "recipient": message.get("recipient"),
                "kind": message.get("kind"),
                "state": message.get("state"),
                "parent_id": message.get("parent_id"),
                "created_at": str(message.get("created_at")),
                "attempt": message.get("attempt"),
                "body": message.get("body"),
                "meta": message.get("meta") or {},
                "artifacts": saved,
            }
            (staging / "message.json").write_text(
                json.dumps(envelope, ensure_ascii=False, indent=2, default=str) + "\n",
                encoding="utf-8",
            )
            os.replace(staging, target)
            (self.markers / message_id).write_text("", encoding="utf-8")
            log(f"delivered {message_id} ({message.get('kind')}) to inbox")
            delivered += 1
        return delivered

    def _artifact(self, message_id: str, name: str) -> str | None:
        try:
            response = self.api.get(f"/v1/messages/{message_id}/artifact", params={"name": name})
        except httpx.HTTPError as exc:
            log(f"artifact {name} failed: {exc}")
            return None
        if response.status_code != 200:
            return None
        payload = response.json()
        content = str(payload.get("content", ""))
        if len(content.encode("utf-8")) > MAX_INBOX_BYTES:
            return content[:MAX_INBOX_BYTES]
        return content

    # --- ack: read/ means handled ----------------------------------------------

    def pump_read(self) -> int:
        """Moving a delivered message into read/ closes it in the queue."""
        read = self.root / "read"
        if not read.is_dir():
            return 0
        acked = 0
        for folder in sorted(read.iterdir()):
            if not folder.is_dir() or folder.name.startswith("."):
                continue
            message_id = folder.name
            if (self.markers / f"{message_id}.acked").exists():
                continue
            try:
                response = self.api.post(
                    f"/v1/messages/{message_id}/state",
                    json={"state": "done", "note": "read in the VM"},
                )
            except httpx.HTTPError as exc:
                log(f"ack {message_id} failed: {exc}")
                continue
            if response.status_code in (200, 404):
                (self.markers / f"{message_id}.acked").write_text("", encoding="utf-8")
                acked += 1
            else:
                log(f"ack {message_id} failed: {response.status_code}")
        return acked

    def run_once(self) -> tuple[int, int, int]:
        return self.pump_outbox(), self.pump_inbox(), self.pump_read()


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    stamp = time.strftime("%Y%m%d-%H%M%S")
    candidate = path.with_name(f"{path.stem}.{stamp}{path.suffix}")
    counter = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}.{stamp}.{counter}{path.suffix}")
        counter += 1
    return candidate


def main() -> int:
    settings = BridgeSettings.from_env()
    api = Api(settings.url, settings.token)
    bridge = Bridge(settings, api)
    log(f"share={bridge.root} interval={settings.interval}s")
    try:
        while True:
            bridge.run_once()
            time.sleep(settings.interval)
    except KeyboardInterrupt:
        log("stopped")
    finally:
        api.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
