"""The file bridge: a VM with no network access talks to the queue through a directory."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentbus.bridge import Bridge, MultiBridge
from agentbus.config import BridgeSettings

from .test_queue import send


def make_settings(share_root: Path, state: Path) -> BridgeSettings:
    return BridgeSettings(
        share_root=str(share_root),
        url="http://testserver",
        token="test-token",
        state_dir=str(state),
        interval=0.0,
    )


def make_bridge(
    client: TestClient, share: Path, state: Path, mailbox: str = "cage-claude"
) -> Bridge:
    return Bridge(make_settings(share, state), client, share / mailbox, mailbox)


def make_multi(
    client: TestClient,
    share: Path,
    state: Path,
    clock: Callable[[], float] | None = None,
) -> MultiBridge:
    settings = make_settings(share, state)
    if clock is None:
        return MultiBridge(settings, client)
    return MultiBridge(settings, client, clock=clock)


def write_mailbox_file(share: Path, mailbox: str, name: str, payload: dict) -> Path:
    outbox = share / mailbox / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    path = outbox / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def write_outbox(share: Path, name: str, payload: dict) -> Path:
    return write_mailbox_file(share, "cage-claude", name, payload)


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

    sent, _, _ = bridge.run_once()

    assert sent == 1
    assert not list((tmp_path / "share" / "cage-claude" / "outbox").glob("*.json"))
    sent_files = [
        path
        for path in (tmp_path / "share" / "cage-claude" / "sent").glob("*.json")
        if not path.name.endswith(".receipt.json")
    ]
    assert len(sent_files) == 1

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
    _, delivered, _ = bridge.run_once()

    assert delivered == 1
    folder = share / "cage-claude" / "inbox" / reply["id"]
    envelope = json.loads((folder / "message.json").read_text(encoding="utf-8"))
    assert envelope["body"] == "готово"
    assert envelope["parent_id"] == parent["id"]
    assert envelope["artifacts"] == ["patch.diff"]
    assert (folder / "patch.diff").read_text(encoding="utf-8").startswith("--- a")

    # a second pass must not redeliver
    _, again, _ = bridge.run_once()
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


def test_lost_delivery_is_repeated(client: TestClient, tmp_path: Path) -> None:
    message = send(client, recipient="cage-claude", sender="local-opencode", kind="result")
    share = tmp_path / "share"
    bridge = make_bridge(client, share, tmp_path / "state")
    _, delivered, _ = bridge.run_once()
    assert delivered == 1

    import shutil

    shutil.rmtree(share / "cage-claude" / "inbox" / message["id"])

    _, again, _ = bridge.run_once()
    assert again == 1
    assert (share / "cage-claude" / "inbox" / message["id"] / "message.json").is_file()


def test_read_closes_the_message(client: TestClient, tmp_path: Path) -> None:
    message = send(client, recipient="cage-claude", sender="local-opencode", kind="result")
    share = tmp_path / "share"
    bridge = make_bridge(client, share, tmp_path / "state")
    bridge.run_once()
    assert client.get(f"/v1/messages/{message['id']}").json()["state"] == "queued"

    folder = share / "cage-claude" / "inbox" / message["id"]
    target = share / "cage-claude" / "read" / message["id"]
    target.parent.mkdir(parents=True, exist_ok=True)
    folder.rename(target)

    _, _, acked = bridge.run_once()
    assert acked == 1
    assert client.get(f"/v1/messages/{message['id']}").json()["state"] == "done"
    assert (
        client.get(f"/v1/messages/{message['id']}").json()["meta"]["state_note"] == "read in the VM"
    )

    _, _, again = bridge.run_once()
    assert again == 0


def test_accept_decision_becomes_the_state_note(client: TestClient, tmp_path: Path) -> None:
    message = send(client, recipient="cage-claude", sender="local-opencode", kind="result")
    share = tmp_path / "share"
    bridge = make_bridge(client, share, tmp_path / "state")
    bridge.run_once()

    target = share / "cage-claude" / "read" / message["id"]
    target.parent.mkdir(parents=True, exist_ok=True)
    (share / "cage-claude" / "inbox" / message["id"]).rename(target)
    (target / "decision.json").write_text(
        json.dumps({"decision": "accept", "note": "всё ок"}, ensure_ascii=False),
        encoding="utf-8",
    )

    _, _, acked = bridge.run_once()

    assert acked == 1
    assert (
        client.get(f"/v1/messages/{message['id']}").json()["meta"]["state_note"] == "accept: всё ок"
    )


def test_accept_without_note_sets_the_short_state_note(client: TestClient, tmp_path: Path) -> None:
    message = send(client, recipient="cage-claude", sender="local-opencode", kind="result")
    share = tmp_path / "share"
    bridge = make_bridge(client, share, tmp_path / "state")
    bridge.run_once()

    target = share / "cage-claude" / "read" / message["id"]
    target.parent.mkdir(parents=True, exist_ok=True)
    (share / "cage-claude" / "inbox" / message["id"]).rename(target)
    (target / "decision.json").write_text(
        json.dumps({"decision": "accept", "note": ""}), encoding="utf-8"
    )

    _, _, acked = bridge.run_once()

    assert acked == 1
    assert client.get(f"/v1/messages/{message['id']}").json()["meta"]["state_note"] == "accept"


def test_share_layout_is_created(client: TestClient, tmp_path: Path) -> None:
    share = tmp_path / "share"
    make_bridge(client, share, tmp_path / "state")
    for name in ("outbox", "sent", "failed", "inbox"):
        assert (share / "cage-claude" / name).is_dir()


# --- multi-mailbox bridge ---------------------------------------------------


def test_two_mailboxes_send_and_receive_separately(client: TestClient, tmp_path: Path) -> None:
    share = tmp_path / "share"
    write_mailbox_file(
        share, "alpha", "0001.json", {"recipient": "local-opencode", "kind": "task", "body": "A"}
    )
    write_mailbox_file(
        share, "beta", "0001.json", {"recipient": "local-opencode", "kind": "task", "body": "B"}
    )

    bridge = make_multi(client, share, tmp_path / "state")
    sent, _, _ = bridge.run_once()

    assert sent == 2
    messages = client.get("/v1/inbox/local-opencode").json()["messages"]
    assert sorted(message["sender"] for message in messages) == ["alpha", "beta"]

    alpha_task = next(message for message in messages if message["sender"] == "alpha")
    reply = client.post(
        f"/v1/messages/{alpha_task['id']}/reply",
        json={"sender": "local-opencode", "kind": "result", "body": "готово"},
    ).json()

    _, delivered, _ = bridge.run_once()

    assert delivered == 1
    assert (share / "alpha" / "inbox" / reply["id"] / "message.json").is_file()
    assert list((share / "beta" / "inbox").iterdir()) == []


def test_new_mailbox_is_picked_up_on_the_next_tick(client: TestClient, tmp_path: Path) -> None:
    share = tmp_path / "share"
    write_mailbox_file(
        share, "alpha", "0001.json", {"recipient": "local-opencode", "kind": "task", "body": "A"}
    )
    bridge = make_multi(client, share, tmp_path / "state")
    bridge.run_once()

    write_mailbox_file(
        share, "gamma", "0001.json", {"recipient": "local-opencode", "kind": "task", "body": "G"}
    )
    sent, _, _ = bridge.run_once()

    assert sent == 1
    senders = [m["sender"] for m in client.get("/v1/inbox/local-opencode").json()["messages"]]
    assert "gamma" in senders
    assert (share / "gamma" / "sent" / "0001.json").is_file()


def test_invalid_mailbox_names_are_ignored(
    client: TestClient, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    share = tmp_path / "share"
    for mailbox in (".hidden", "Bad_Name", "x"):
        write_mailbox_file(
            share, mailbox, "0001.json", {"recipient": "local-opencode", "kind": "task", "body": 1}
        )

    bridge = make_multi(client, share, tmp_path / "state")
    bridge.run_once()
    bridge.run_once()

    for mailbox in (".hidden", "Bad_Name", "x"):
        assert (share / mailbox / "outbox" / "0001.json").is_file()
        for name in ("sent", "failed", "inbox"):
            assert not (share / mailbox / name).exists()
    assert client.get("/v1/inbox/local-opencode").json()["messages"] == []

    invalid = [line for line in capsys.readouterr().out.splitlines() if "invalid name" in line]
    assert len(invalid) == 2
    assert any("'Bad_Name'" in line for line in invalid)
    assert any("'x'" in line for line in invalid)


def test_sender_is_forced_to_the_mailbox_name(client: TestClient, tmp_path: Path) -> None:
    share = tmp_path / "share"
    write_mailbox_file(
        share,
        "alpha",
        "0001.json",
        {"sender": "beta", "recipient": "local-opencode", "kind": "task", "body": "X"},
    )

    make_multi(client, share, tmp_path / "state").run_once()

    messages = client.get("/v1/inbox/local-opencode").json()["messages"]
    assert [message["sender"] for message in messages] == ["alpha"]


def test_receipt_is_written_next_to_the_sent_file(client: TestClient, tmp_path: Path) -> None:
    share = tmp_path / "share"
    write_mailbox_file(
        share, "alpha", "0001.json", {"recipient": "local-opencode", "kind": "task", "body": "X"}
    )

    sent, _, _ = make_multi(client, share, tmp_path / "state").run_once()

    assert sent == 1
    message = client.get("/v1/inbox/local-opencode").json()["messages"][0]
    receipt = json.loads((share / "alpha" / "sent" / "0001.receipt.json").read_text())
    assert receipt["id"] == message["id"]
    assert receipt["thread_id"] == message["thread_id"]
    assert receipt["file"] == "0001.json"
    assert receipt["kind"] == "task"
    assert receipt["recipient"] == "local-opencode"


def mailbox_lines(capsys: pytest.CaptureFixture[str], mailbox: str) -> list[str]:
    return [line for line in capsys.readouterr().out.splitlines() if f"mailbox {mailbox}" in line]


def test_broken_mailbox_errors_are_throttled_and_recovery_is_logged(
    client: TestClient, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    share = tmp_path / "share"
    beta = share / "beta"
    beta.mkdir(parents=True)
    (beta / "inbox").write_text("not a directory", encoding="utf-8")

    now = [1000.0]
    bridge = make_multi(client, share, tmp_path / "state", clock=lambda: now[0])

    bridge.run_once()
    bridge.run_once()
    bridge.run_once()

    first = mailbox_lines(capsys, "beta")
    assert len(first) == 1, "the same error must not be logged on every tick"
    assert "(repeated" not in first[0]

    now[0] += 301
    bridge.run_once()

    repeated = mailbox_lines(capsys, "beta")
    assert len(repeated) == 1
    assert repeated[0].endswith("(repeated 2 times)")

    (beta / "inbox").unlink()
    bridge.run_once()

    recovered = mailbox_lines(capsys, "beta")
    assert len(recovered) == 1
    assert recovered[0].endswith("mailbox beta: recovered")


def test_broken_mailbox_does_not_stop_the_others(client: TestClient, tmp_path: Path) -> None:
    share = tmp_path / "share"
    beta = share / "beta"
    beta.mkdir(parents=True)
    (beta / "inbox").write_text("not a directory", encoding="utf-8")
    write_mailbox_file(
        share, "alpha", "0001.json", {"recipient": "local-opencode", "kind": "task", "body": "A"}
    )

    sent, _, _ = make_multi(client, share, tmp_path / "state").run_once()

    assert sent == 1
    messages = client.get("/v1/inbox/local-opencode").json()["messages"]
    assert [message["sender"] for message in messages] == ["alpha"]


def test_bridge_settings_resolve_the_share_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AGENTBUS_TOKEN", "test-token")
    monkeypatch.setenv("AGENTBUS_SHARE_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("AGENTBUS_SHARE", str(tmp_path / "ignored" / "box"))
    assert BridgeSettings.from_env().share_root == str(tmp_path / "root")

    monkeypatch.delenv("AGENTBUS_SHARE_ROOT")
    monkeypatch.setenv("AGENTBUS_SHARE", str(tmp_path / "share" / "cage-claude"))
    assert BridgeSettings.from_env().share_root == str(tmp_path / "share")

    monkeypatch.delenv("AGENTBUS_SHARE")
    with pytest.raises(RuntimeError):
        BridgeSettings.from_env()
