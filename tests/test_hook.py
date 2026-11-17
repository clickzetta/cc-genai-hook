import json

import pytest

from cc_genai_hook.config import load_config
from cc_genai_hook.hook import handle_stop
from cc_genai_hook.state import StateStore


def _write_transcript(path):
    rows = [
        {
            "type": "user",
            "promptId": "p1",
            "uuid": "u1",
            "origin": {"kind": "human"},
            "promptSource": "typed",
            "timestamp": "2026-11-04T10:00:00Z",
            "sessionId": "s1",
            "version": "2.1.311",
            "cwd": "/tmp",
            "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "requestedModel": "claude-sonnet",
            "timestamp": "2026-11-04T10:00:01Z",
            "sessionId": "s1",
            "version": "2.1.311",
            "cwd": "/tmp",
            "message": {
                "id": "m1",
                "role": "assistant",
                "model": "claude-sonnet-4",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "content": [{"type": "text", "text": "hello"}],
            },
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")


def test_handle_stop_dry_run_and_dedupe(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    _write_transcript(transcript)
    config_path = tmp_path / "genai-hook.json"
    config_path.write_text(
        json.dumps(
            {
                "exporter": {"endpoint": "http://127.0.0.1:4318"},
                "hook": {"dry_run": True},
                "state": {"dir": str(tmp_path / "state")},
            }
        )
    )
    cfg = load_config(str(config_path))
    payload = {"session_id": "s1", "transcript_path": str(transcript)}

    first = handle_stop(payload, "stop", cfg)
    assert first.flush_ok is True
    assert first.emitted == 1
    assert first.skipped == 0

    dry_run = tmp_path / "state" / "dry-run.jsonl"
    lines = [json.loads(line) for line in dry_run.read_text().splitlines() if line]
    assert [span["name"] for span in lines] == ["chat", "invoke_agent"]
    chat = next(span for span in lines if span["name"] == "chat")
    assert chat["attributes"]["gen_ai.operation.name"] == "chat"
    assert chat["attributes"]["gen_ai.conversation.id"] == "s1"
    assert chat["attributes"]["gen_ai.input.messages"]
    assert chat["parent_id"] == next(
        span for span in lines if span["name"] == "invoke_agent"
    )["context"]["span_id"]

    second = handle_stop(payload, "stop", cfg)
    assert second.emitted == 0
    assert second.skipped == 1
    assert len(dry_run.read_text().splitlines()) == len(lines)


def test_failed_emit_aborts_reserved_segment(tmp_path, monkeypatch):
    transcript = tmp_path / "transcript.jsonl"
    _write_transcript(transcript)
    config_path = tmp_path / "genai-hook.json"
    config_path.write_text(
        json.dumps(
            {
                "exporter": {"endpoint": "http://127.0.0.1:4318"},
                "hook": {"dry_run": True},
                "state": {"dir": str(tmp_path / "state")},
            }
        )
    )
    cfg = load_config(str(config_path))

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("cc_genai_hook.hook.emit_ordered_turn", boom)
    with pytest.raises(RuntimeError):
        handle_stop(
            {"session_id": "s1", "transcript_path": str(transcript)},
            "stop",
            cfg,
        )

    store = StateStore(str(tmp_path / "state"))
    try:
        assert store.counts() == (1, 0, 0)
    finally:
        store.close()


def test_prune_state_removes_expired_sessions(tmp_path):
    from cc_genai_hook.hook import prune_state

    config_path = tmp_path / "genai-hook.json"
    config_path.write_text(json.dumps({"state": {"dir": str(tmp_path / "state")}}))
    cfg = load_config(str(config_path))
    store = StateStore(cfg["state"]["dir"])
    try:
        store.ensure_session("old")
        store.ensure_session("new")
        with store._conn:
            store._conn.execute(
                "UPDATE sessions SET updated_at='2000-01-01T00:00:00+00:00' WHERE session_id='old'"
            )
    finally:
        store.close()

    prune_state(cfg)

    store = StateStore(cfg["state"]["dir"])
    try:
        assert store.counts()[0] == 1
    finally:
        store.close()
