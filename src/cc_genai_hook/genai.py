"""GenAI semantic-convention attribute mapping."""

from __future__ import annotations

import datetime
import json
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .config import ConfigError
from .transcript import TimelineItem, input_messages, message_payloads


def ts_ns(value: Optional[str]) -> int:
    """RFC3339 timestamp -> Unix nanoseconds."""

    if not value:
        return time.time_ns()
    try:
        text = value.replace("Z", "+00:00")
        return int(datetime.datetime.fromisoformat(text).timestamp() * 1e9)
    except Exception:
        return time.time_ns()


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


class ContentProcessor:
    """Redaction and truncation for content-bearing attributes."""

    def __init__(self, content: Dict[str, Any]):
        self.mode = str(content.get("mode") or "last_turn").lower()
        self.include_thinking = bool(content.get("include_thinking", False))
        self.max_input = int(content.get("max_input_chars") or 200000)
        self.max_message = int(content.get("max_message_chars") or 100000)
        self.max_tool = int(content.get("max_tool_chars") or 100000)
        self.patterns = []
        for pattern in content.get("redact_patterns") or []:
            try:
                self.patterns.append(re.compile(str(pattern)))
            except re.error as exc:
                raise ConfigError("invalid redact pattern %r: %s" % (pattern, exc))

    def clean(self, text: str, limit: int) -> Tuple[str, bool]:
        for pattern in self.patterns:
            text = pattern.sub("[REDACTED]", text)
        if len(text) <= limit:
            return text, False
        return text[:limit], True

    def messages_text(self, messages: Iterable[Dict[str, Any]]) -> str:
        lines: List[str] = []
        for message in messages:
            role = message.get("role", "unknown")
            parts = message.get("parts") or []
            chunks: List[str] = []
            for part in parts:
                ptype = part.get("type")
                if ptype in ("text", "reasoning", "transcription"):
                    chunks.append(str(part.get("content", "")))
                elif ptype == "tool_call":
                    chunks.append(
                        "tool_call %s(%s)"
                        % (part.get("name"), as_text(part.get("arguments")))
                    )
                elif ptype == "tool_call_response":
                    chunks.append(
                        "tool_call_response %s: %s"
                        % (part.get("id"), as_text(part.get("response")))
                    )
                elif ptype in ("blob", "uri", "file"):
                    chunks.append(
                        "%s %s"
                        % (ptype, part.get("uri") or part.get("mime_type") or "")
                    )
            lines.append("%s: %s" % (role, " ".join(chunks)))
        return "\n".join(lines)

    def format_messages(self, messages: List[Dict[str, Any]], fmt: str) -> str:
        if fmt == "text":
            return self.messages_text(messages)
        return as_text(messages)


def _usage_numbers(usage: Dict[str, Any]) -> Tuple[int, int, int, int]:
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_creation = int(usage.get("cache_creation_input_tokens") or 0)
    return input_tokens, output_tokens, cache_read, cache_creation


def _usage_attributes(usage: Dict[str, Any], compat: Dict[str, Any]) -> Dict[str, Any]:
    input_tokens, output_tokens, cache_read, cache_creation = _usage_numbers(usage)
    attrs: Dict[str, Any] = {
        # GenAI semconv: input_tokens includes cache reads/writes.
        "gen_ai.usage.input_tokens": input_tokens + cache_read + cache_creation,
        "gen_ai.usage.output_tokens": output_tokens,
        "gen_ai.usage.cache_read.input_tokens": cache_read,
    }
    cache_name = compat.get("cache_write_attribute") or "cache_creation"
    if cache_name == "cache_write":
        attrs["gen_ai.usage.cache_write.input_tokens"] = cache_creation
    else:
        attrs["gen_ai.usage.cache_creation.input_tokens"] = cache_creation
    return attrs


def _provider_attributes(compat: Dict[str, Any]) -> Dict[str, Any]:
    attrs: Dict[str, Any] = {}
    if compat.get("emit_provider_name", True):
        attrs["gen_ai.provider.name"] = "anthropic"
    if compat.get("emit_system_legacy", True):
        attrs["gen_ai.system"] = "anthropic"
    return attrs


def chat_attributes(
    item: TimelineItem,
    index: int,
    timeline: List[TimelineItem],
    session_id: str,
    content: ContentProcessor,
    compat: Dict[str, Any],
    step_index: Optional[int] = None,
    step_name: Optional[str] = None,
) -> Dict[str, Any]:
    attrs: Dict[str, Any] = {
        "gen_ai.operation.name": "chat",
        **_provider_attributes(compat),
        "gen_ai.request.model": item.requested_model or item.model or "",
        "gen_ai.response.model": item.model or "",
        "gen_ai.response.id": item.message_id or "",
        "gen_ai.response.finish_reasons": [item.stop_reason or "unknown"],
        "gen_ai.conversation.id": session_id,
        "claude_code.genai_hook.message.uuid": item.message_id or "",
        "claude_code.genai_hook.timing.source": "transcript",
    }
    attrs.update(_usage_attributes(item.usage, compat))
    if item.request_ref:
        attrs["claude_code.genai_hook.request_ref"] = item.request_ref
    if step_index is not None:
        attrs["claude_code.genai_hook.step_index"] = int(step_index)
    if step_name:
        attrs["claude_code.genai_hook.step_name"] = step_name

    if content.mode != "none":
        if content.mode == "full":
            input_msgs = input_messages(
                timeline, index, "full", content.include_thinking
            )
        else:
            input_msgs = input_messages(
                timeline, index, "last_turn", content.include_thinking
            )
        output_msgs = message_payloads(item, content.include_thinking)
        input_text, input_cut = content.clean(
            content.format_messages(input_msgs, compat.get("messages_format", "json")),
            content.max_input,
        )
        output_text, output_cut = content.clean(
            content.format_messages(output_msgs, compat.get("messages_format", "json")),
            content.max_message,
        )
        attrs["gen_ai.input.messages"] = input_text
        attrs["gen_ai.output.messages"] = output_text
        if input_cut:
            attrs["claude_code.genai_hook.input.messages.truncated"] = True
        if output_cut:
            attrs["claude_code.genai_hook.output.messages.truncated"] = True
    return attrs


def tool_attributes(
    tool_name: str,
    tool_use_id: str,
    tool_input: Any,
    tool_response: Any,
    error: Optional[str],
    is_interrupt: bool,
    duration_ms: Any,
    session_id: str,
    prompt_id: str,
    parent_segment: int,
    content: ContentProcessor,
    compat: Dict[str, Any],
    step_index: Optional[int] = None,
    step_name: Optional[str] = None,
    plan_step: Optional[int] = None,
) -> Dict[str, Any]:
    attrs: Dict[str, Any] = {
        "gen_ai.operation.name": "execute_tool",
        **_provider_attributes(compat),
        "gen_ai.tool.name": tool_name or "",
        "gen_ai.tool.call.id": tool_use_id or "",
        "gen_ai.conversation.id": session_id,
        "claude_code.genai_hook.tool.duration_ms": int(duration_ms or 0),
        "claude_code.genai_hook.tool.parent_segment": parent_segment,
    }
    if prompt_id:
        attrs["claude_code.genai_hook.turn.prompt_id"] = prompt_id
    if step_index is not None:
        attrs["claude_code.genai_hook.step_index"] = int(step_index)
    if step_name:
        attrs["claude_code.genai_hook.step_name"] = step_name
    if plan_step is not None:
        attrs["claude_code.genai_hook.plan_step"] = int(plan_step)

    if content.mode != "none":
        args_text, args_cut = content.clean(as_text(tool_input), content.max_tool)
        attrs["gen_ai.tool.call.arguments"] = args_text
        if args_cut:
            attrs["claude_code.genai_hook.tool.arguments.truncated"] = True

        if error is None and tool_response is not None:
            result_text, result_cut = content.clean(
                as_text(tool_response), content.max_tool
            )
            attrs["gen_ai.tool.call.result"] = result_text
            if result_cut:
                attrs["claude_code.genai_hook.tool.result.truncated"] = True

    if error is not None:
        attrs["error.type"] = "interrupted" if is_interrupt else "tool_error"
        attrs["claude_code.genai_hook.tool.success"] = False
        if is_interrupt:
            attrs["claude_code.genai_hook.tool.is_interrupt"] = True
        if content.mode != "none":
            error_text, error_cut = content.clean(
                as_text(error), content.max_tool
            )
            attrs["claude_code.genai_hook.tool.error"] = error_text
            if error_cut:
                attrs["claude_code.genai_hook.tool.error.truncated"] = True
    else:
        attrs["claude_code.genai_hook.tool.success"] = True
    return attrs


def root_attributes(
    messages: List[TimelineItem],
    prompt_item: Optional[TimelineItem],
    session_id: str,
    segment_seq: int,
    agent_name: str,
    content: ContentProcessor,
    compat: Dict[str, Any],
    agent_id: Optional[str] = None,
    step_count: Optional[int] = None,
) -> Dict[str, Any]:
    total_input = 0
    total_output = 0
    total_read = 0
    total_creation = 0
    for item in messages:
        input_tokens, output_tokens, cache_read, cache_creation = _usage_numbers(
            item.usage
        )
        total_input += input_tokens
        total_output += output_tokens
        total_read += cache_read
        total_creation += cache_creation

    attrs: Dict[str, Any] = {
        "gen_ai.operation.name": "invoke_agent",
        **_provider_attributes(compat),
        "gen_ai.agent.name": agent_name,
        "gen_ai.conversation.id": session_id,
        "gen_ai.response.finish_reasons": [
            messages[-1].stop_reason or "unknown"
        ]
        if messages
        else ["unknown"],
        "gen_ai.usage.input_tokens": total_input + total_read + total_creation,
        "gen_ai.usage.output_tokens": total_output,
        "gen_ai.usage.cache_read.input_tokens": total_read,
        "claude_code.genai_hook.synthetic": True,
        "claude_code.genai_hook.turn.prompt_id": (
            prompt_item.prompt_id if prompt_item and prompt_item.prompt_id else ""
        ),
        "claude_code.genai_hook.turn.segment": segment_seq,
    }
    cache_name = compat.get("cache_write_attribute") or "cache_creation"
    if cache_name == "cache_write":
        attrs["gen_ai.usage.cache_write.input_tokens"] = total_creation
    else:
        attrs["gen_ai.usage.cache_creation.input_tokens"] = total_creation
    if agent_id:
        attrs["gen_ai.agent.id"] = agent_id
    if step_count is not None:
        attrs["claude_code.genai_hook.step_count"] = int(step_count)

    if content.mode != "none":
        input_msgs = (
            message_payloads(prompt_item, content.include_thinking)
            if prompt_item
            else []
        )
        output_msgs = (
            message_payloads(messages[-1], content.include_thinking)
            if messages
            else []
        )
        input_text, input_cut = content.clean(
            content.format_messages(input_msgs, compat.get("messages_format", "json")),
            content.max_input,
        )
        output_text, output_cut = content.clean(
            content.format_messages(output_msgs, compat.get("messages_format", "json")),
            content.max_message,
        )
        attrs["gen_ai.input.messages"] = input_text
        attrs["gen_ai.output.messages"] = output_text
        if input_cut:
            attrs["claude_code.genai_hook.input.messages.truncated"] = True
        if output_cut:
            attrs["claude_code.genai_hook.output.messages.truncated"] = True
    return attrs
