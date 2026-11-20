"""The file bridge: a VM with no network access talks to the queue through a directory."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from agentbus.bridge import Bridge
from agentbus.config import BridgeSettings

from .test_queue import send


def make_bridge(client: TestClient, share: Path, state: Path) -> Bridge:
    settings = BridgeSettings(
        share_dir=str(share / "cage-claude"),
        agent="cage-claude",
        url="http://testserver",
        token="test-token",
        state_dir=str(state),
        interval=0.0,
    )
    return Bridge(settings, client)


def write_outbox(share: Path, name: str, payload: dict) -> Path:
    outbox = share / "cage-claude" / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    path = outbox / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_outbox_file_becomes_a_message(client: TestClient, tmp_path: Path) -> None:
    bridge = make_bridge(client, tmp_path / "share", tmp_path / "state")
    write_outbox(
        tmp_path / "share",
        "0001.json",
        {
            "recipient": "local-opencode",
            "kind": "task",
            "body": "сделай X",
            "repo": "/tmp/repo",
            "meta": {"acceptance": ["X готов"]},
        },
    )

    sent, _ = bridge.run_once()

    assert sent == 1
    assert not list((tmp_path / "share" / "cage-claude" / "outbox").glob("*.json"))
    assert len(list((tmp_path / "share" / "cage-claude" / "sent").glob("*.json"))) == 1

    messages = client.get("/v1/inbox/local-opencode").json()["messages"]
    assert len(messages) == 1
    assert messages[0]["sender"] == "cage-claude"
    assert messages[0]["meta"]["acceptance"] == ["X готов"]


def test_outbox_is_idempotent_across_restarts(client: TestClient, tmp_path: Path) -> None:
    share = tmp_path / "share"
    write_outbox(share, "0001.json", {"recipient": "local-opencode", "kind": "task", "body": "X"})
    bridge = make_bridge(client, share, tmp_path / "state")
    bridge.run_once()

    # simulate a crash right after the API call, before the file was moved
    write_outbox(share, "0001.json", {"recipient": "local-opencode", "kind": "task", "body": "X"})
    bridge.run_once()

    assert len(client.get("/v1/inbox/local-opencode").json()["messages"]) == 1


def test_broken_files_are_rejected_not_retried(client: TestClient, tmp_path: Path) -> None:
    share = tmp_path / "share"
    outbox = share / "cage-claude" / "outbox"
    outbox.mkdir(parents=True)
    (outbox / "broken.json").write_text("{not json", encoding="utf-8")
    write_outbox(
        share,
        "bad-thread.json",
        {"recipient": "local-opencode", "kind": "task", "body": "X", "thread_id": "t_missing"},
    )

    bridge = make_bridge(client, share, tmp_path / "state")
    bridge.run_once()

    failed = sorted((share / "cage-claude" / "failed").glob("*.json"))
    assert [p.name for p in failed] == ["bad-thread.json", "broken.json"]
    assert (share / "cage-claude" / "failed" / "broken.error").is_file()
    assert client.get("/v1/inbox/local-opencode").json()["messages"] == []


def test_replies_are_delivered_with_artifacts(
    client: TestClient, tmp_path: Path, artifacts: str
) -> None:
    parent = send(client, body="сделай X")
    reply = client.post(
        f"/v1/messages/{parent['id']}/reply",
        json={
            "sender": "local-opencode",
            "kind": "result",
            "body": "готово",
            "meta": {"artifacts": ["patch.diff"], "diff_stat": " 1 file changed"},
        },
    ).json()

    artifact_dir = Path(artifacts) / reply["id"]
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "patch.diff").write_text("--- a\n+++ b\n", encoding="utf-8")

    share = tmp_path / "share"
    bridge = make_bridge(client, share, tmp_path / "state")
    _, delivered = bridge.run_once()

    assert delivered == 1
    folder = share / "cage-claude" / "inbox" / reply["id"]
    envelope = json.loads((folder / "message.json").read_text(encoding="utf-8"))
    assert envelope["body"] == "готово"
    assert envelope["parent_id"] == parent["id"]
    assert envelope["artifacts"] == ["patch.diff"]
    assert (folder / "patch.diff").read_text(encoding="utf-8").startswith("--- a")

    # a second pass must not redeliver
    _, again = bridge.run_once()
    assert again == 0


def test_reply_from_the_vm_closes_the_task(client: TestClient, tmp_path: Path) -> None:
    parent = send(client)
    thread_id = parent["thread_id"]

    share = tmp_path / "share"
    write_outbox(
        share,
        "0002.json",
        {
            "recipient": "local-opencode",
            "kind": "verdict",
            "body": "правь Y",
            "thread_id": thread_id,
            "parent_id": parent["id"],
        },
    )
    bridge = make_bridge(client, share, tmp_path / "state")
    bridge.run_once()

    assert client.get(f"/v1/messages/{parent['id']}").json()["state"] == "done"
    verdict = client.post("/v1/inbox/local-opencode/lease").json()
    assert verdict["kind"] == "verdict"
    assert verdict["body"] == "правь Y"


def test_share_layout_is_created(client: TestClient, tmp_path: Path) -> None:
    share = tmp_path / "share"
    make_bridge(client, share, tmp_path / "state")
    for name in ("outbox", "sent", "failed", "inbox"):
        assert (share / "cage-claude" / name).is_dir()
