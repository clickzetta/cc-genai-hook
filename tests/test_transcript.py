from cc_genai_hook.transcript import (
    build_timeline,
    input_messages,
    is_prompt,
    is_real_prompt_row,
    message_payloads,
)


def _rows():
    return [
        {
            "type": "user",
            "promptId": "p1",
            "uuid": "u1",
            "origin": {"kind": "human"},
            "promptSource": "typed",
            "timestamp": "2026-11-04T10:00:00Z",
            "sessionId": "s1",
            "version": "2.1.311",
            "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "requestedModel": "claude-sonnet",
            "timestamp": "2026-11-04T10:00:01Z",
            "sessionId": "s1",
            "message": {
                "id": "m1",
                "role": "assistant",
                "model": "claude-sonnet-4",
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}],
            },
        },
        {
            "type": "assistant",
            "uuid": "a2",
            "timestamp": "2026-11-04T10:00:02Z",
            "sessionId": "s1",
            "message": {
                "id": "m1",
                "role": "assistant",
                "model": "claude-sonnet-4",
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "content": [{"type": "thinking", "thinking": "secret"}],
            },
        },
        {
            "type": "user",
            "promptId": "p1",
            "uuid": "u2",
            "timestamp": "2026-11-04T10:00:03Z",
            "sessionId": "s1",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [{"type": "text", "text": "file"}],
                    }
                ],
            },
        },
        {
            "type": "assistant",
            "uuid": "a3",
            "timestamp": "2026-11-04T10:00:04Z",
            "sessionId": "s1",
            "message": {
                "id": "m2",
                "role": "assistant",
                "model": "claude-sonnet-4",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 20, "output_tokens": 7},
                "content": [{"type": "text", "text": "done"}],
            },
        },
    ]


def test_build_timeline_merges_assistant_rows():
    timeline, tools = build_timeline(_rows())
    assistants = [item for item in timeline if item.kind == "assistant"]
    assert len(assistants) == 2
    assert assistants[0].message_id == "m1"
    assert [b["type"] for b in assistants[0].blocks] == ["tool_use", "thinking"]
    assert assistants[0].prompt_id == "p1"
    assert "t1" in tools
    assert tools["t1"].content[0]["text"] == "file"


def test_prompt_detection_excludes_tool_result_rows():
    rows = _rows()
    assert is_real_prompt_row(rows[0])
    assert not is_real_prompt_row(rows[3])
    timeline, _ = build_timeline(rows)
    assert is_prompt(timeline[0])
    assert not is_prompt(timeline[2])


def test_input_messages_last_turn_excludes_thinking_by_default():
    timeline, _ = build_timeline(_rows())
    msgs = input_messages(timeline, 3, "last_turn", include_thinking=False)
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool"]
    assert "secret" not in str(msgs)
    msgs_with_thinking = input_messages(timeline, 3, "last_turn", include_thinking=True)
    assert "secret" in str(msgs_with_thinking)


def test_message_payloads_use_semconv_parts():
    timeline, _ = build_timeline(_rows())
    assistant = [item for item in timeline if item.kind == "assistant"][0]
    messages = message_payloads(assistant, include_thinking=False)
    assert len(messages) == 1
    payload = messages[0]
    assert payload["role"] == "assistant"
    assert payload["finish_reason"] == "tool_use"
    assert payload["parts"][0]["type"] == "tool_call"
    assert payload["parts"][0]["name"] == "Read"


def test_tool_result_becomes_tool_message():
    timeline, _ = build_timeline(_rows())
    tool_item = timeline[2]
    messages = message_payloads(tool_item, include_thinking=False)
    assert messages == [
        {
            "role": "tool",
            "parts": [
                {
                    "type": "tool_call_response",
                    "id": "t1",
                    "response": "file",
                }
            ],
        }
    ]
