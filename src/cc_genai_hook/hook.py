"""Command-mode hook entrypoint."""

from __future__ import annotations

import datetime
import json
import os
import sys
import time
import traceback
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .config import ConfigError, load_config
from .emitter import (
    build_tracer,
    emit_ordered_turn,
    emit_tool_span,
    emit_turn,
    flush,
)
from .genai import ContentProcessor, ts_ns
from .ids import trace_id
from .state import StateStore
from .transcript import (
    TimelineItem,
    build_timeline,
    is_prompt,
    latest_prompt,
    load_rows,
)


@dataclass
class HookResult:
    event: str
    emitted: int = 0
    skipped: int = 0
    flush_ok: bool = True
    error: Optional[str] = None
    segments: List[Tuple[str, int]] = field(default_factory=list)


def _log(config: Dict[str, Any], message: str, error: bool = False) -> None:
    try:
        state_dir = config.get("state", {}).get("dir") or os.path.expanduser(
            "~/.claude/state/genai-hook"
        )
        os.makedirs(state_dir, exist_ok=True)
        if not error and not config.get("hook", {}).get("debug"):
            return
        path = os.path.join(state_dir, "hook.err.log" if error else "hook.log")
        stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("%s %s\n" % (stamp, message))
    except Exception:
        pass


def _load_for_event(event: str) -> Dict[str, Any]:
    config = load_config(require_endpoint=False)
    if not config.get("enabled", True):
        return config
    if event == "seed":
        return config
    dry_run = bool(config.get("hook", {}).get("dry_run", False))
    if not dry_run and not config["exporter"].get("resolved_endpoint"):
        config = load_config(require_endpoint=True)
    return config


def _session_meta(timeline: List[TimelineItem]) -> Dict[str, Optional[str]]:
    version = None
    cwd = None
    for item in timeline:
        version = version or item.version
        cwd = cwd or item.cwd
        if version and cwd:
            break
    return {"version": version, "cwd": cwd}


def _resolve_prompt_id(payload: Dict[str, Any]) -> str:
    prompt_id = payload.get("prompt_id")
    if prompt_id:
        return str(prompt_id)
    transcript = str(payload.get("transcript_path") or "")
    if transcript and os.path.exists(transcript):
        try:
            timeline, _ = build_timeline(load_rows(transcript))
            for item in reversed(timeline):
                if item.kind == "user" and item.prompt_id and is_prompt(item):
                    return str(item.prompt_id)
        except Exception:
            pass
    return "session"


def handle_tool(
    payload: Dict[str, Any], event: str, config: Dict[str, Any]
) -> HookResult:
    session_id = str(payload.get("session_id") or "")
    tool_use_id = str(payload.get("tool_use_id") or "")
    if not session_id or not tool_use_id:
        result = HookResult(
            event=event, error="missing session_id or tool_use_id"
        )
        _log(config, result.error or "missing input", error=True)
        return result
    if not config.get("emit", {}).get("execute_tool", True):
        return HookResult(event=event, skipped=1)
    if config.get("hook", {}).get("tool_mode", "ordered") != "realtime":
        # Ordered mode emits tools at Stop, together with generations, so the
        # shared step counter can assign a stable order.
        return HookResult(event=event, skipped=1)

    prompt_id = _resolve_prompt_id(payload)
    transcript = str(payload.get("transcript_path") or "")
    state = StateStore(config["state"]["dir"])
    try:
        state.ensure_session(
            session_id,
            transcript or None,
        )
        if state.tool_emitted(session_id, tool_use_id):
            return HookResult(event=event, skipped=1)

        content = ContentProcessor(config["content"])
        compat = config.get("compat") or {}
        bundle = build_tracer(config)
        failed = event == "post_tool_use_failure"
        error = payload.get("error") if failed else None
        if failed and error is None:
            error = "tool error"
        tool_sid = emit_tool_span(
            bundle,
            session_id,
            prompt_id,
            tool_use_id,
            str(payload.get("tool_name") or ""),
            payload.get("tool_input"),
            payload.get("tool_response"),
            error,
            bool(payload.get("is_interrupt")),
            payload.get("duration_ms"),
            content,
            compat,
        )
        if not flush(bundle):
            _log(config, "flush failed; tool left unprocessed", error=True)
            return HookResult(
                event=event, flush_ok=False, error="flush failed"
            )
        state.mark_tool(session_id, tool_use_id, prompt_id, tool_sid)
        return HookResult(event=event, emitted=1)
    finally:
        state.close()


def handle_seed(payload: Dict[str, Any], config: Dict[str, Any]) -> HookResult:
    session_id = str(payload.get("session_id") or "")
    transcript = str(payload.get("transcript_path") or "")
    if not session_id or not transcript or not os.path.exists(transcript):
        return HookResult(event="seed", error="missing session_id or transcript_path")

    timeline, _ = build_timeline(load_rows(transcript))
    meta = _session_meta(timeline)
    state = StateStore(config["state"]["dir"])
    try:
        state.ensure_session(
            session_id, transcript, meta.get("version"), meta.get("cwd")
        )
        records = [
            (item.message_id, item.prompt_id or "session")
            for item in timeline
            if item.kind == "assistant" and item.message_id
        ]
        state.mark_all_messages(session_id, records)
        return HookResult(event="seed", emitted=len(records))
    finally:
        state.close()


def handle_stop(
    payload: Dict[str, Any], event: str, config: Dict[str, Any]
) -> HookResult:
    session_id = str(payload.get("session_id") or "")
    if event == "subagent_stop":
        transcript = str(
            payload.get("agent_transcript_path")
            or payload.get("transcript_path")
            or ""
        )
    else:
        transcript = str(payload.get("transcript_path") or "")

    if not session_id or not transcript or not os.path.exists(transcript):
        result = HookResult(event=event, error="missing session_id or transcript_path")
        _log(config, result.error or "missing input", error=True)
        return result

    timeline, tool_results = build_timeline(load_rows(transcript))
    if not timeline:
        return HookResult(event=event, error="empty transcript")

    meta = _session_meta(timeline)
    state = StateStore(config["state"]["dir"])
    try:
        state.ensure_session(
            session_id, transcript, meta.get("version"), meta.get("cwd")
        )
        already = state.emitted_message_ids(session_id)
        already_tools = state.emitted_tool_ids(session_id)
        tool_mode = str(config.get("hook", {}).get("tool_mode") or "ordered")

        groups: "OrderedDict[str, List[TimelineItem]]" = OrderedDict()
        skipped = 0
        for item in timeline:
            if item.kind != "assistant" or not item.message_id:
                continue
            if item.message_id in already:
                skipped += 1
                continue
            prompt_id = item.prompt_id or "session"
            groups.setdefault(prompt_id, []).append(item)

        if not groups:
            return HookResult(event=event, skipped=skipped)

        message_indices = {id(item): idx for idx, item in enumerate(timeline)}
        content = ContentProcessor(config["content"])
        compat = config.get("compat") or {}
        bundle = build_tracer(config)
        agent_name = config.get("hook", {}).get("agent_name", "claude-code")
        agent_id = None
        if event == "subagent_stop":
            agent_name = str(payload.get("agent_type") or (agent_name + "-subagent"))
            agent_id = str(payload.get("agent_id") or "") or None

        records: List[Tuple[str, str, int]] = []
        tool_records: List[Tuple[str, str, int]] = []
        segments: List[Tuple[str, int]] = []
        committed = False
        try:
            for prompt_id, messages in groups.items():
                first_index = message_indices[id(messages[0])]
                prompt_item = latest_prompt(timeline, first_index, prompt_id)
                if prompt_item is None:
                    prompt_item = latest_prompt(timeline, first_index)
                started_ns = (
                    ts_ns(prompt_item.ts)
                    if prompt_item is not None
                    else ts_ns(messages[0].ts)
                )
                tid = trace_id(session_id, prompt_id)
                existing, _root_sid = state.begin_segment(
                    session_id, prompt_id, tid, started_ns
                )
                segments.append((prompt_id, existing))
                if tool_mode == "ordered":
                    chat_records, new_tool_records = emit_ordered_turn(
                        bundle,
                        session_id,
                        prompt_id,
                        existing,
                        prompt_item,
                        messages,
                        tool_results,
                        timeline,
                        message_indices,
                        content,
                        compat,
                        agent_name=agent_name,
                        agent_id=agent_id,
                        emitted_tool_ids=already_tools,
                        emitted_message_ids=already,
                        transcript_path=transcript,
                    )
                    records.extend(chat_records)
                    tool_records.extend(new_tool_records)
                else:
                    records.extend(
                        emit_turn(
                            bundle,
                            session_id,
                            prompt_id,
                            existing,
                            prompt_item,
                            messages,
                            timeline,
                            message_indices,
                            content,
                            compat,
                            agent_name=agent_name,
                            agent_id=agent_id,
                        )
                    )

            flush_ok = flush(bundle)
            if not flush_ok:
                for prompt_id, seq in segments:
                    state.abort_segment(session_id, prompt_id, seq)
                _log(config, "flush failed; messages left unprocessed", error=True)
                return HookResult(
                    event=event,
                    emitted=0,
                    skipped=skipped,
                    flush_ok=False,
                    error="flush failed",
                    segments=segments,
                )

            state.mark_messages(session_id, records)
            if tool_records:
                state.mark_tools(session_id, tool_records)
            ended = time.time_ns()
            for prompt_id, seq in segments:
                state.finish_segment(session_id, prompt_id, seq, ended)
            committed = True
            return HookResult(
                event=event,
                emitted=len(records),
                skipped=skipped,
                flush_ok=True,
                segments=segments,
            )
        finally:
            # If we did not commit, remove reserved segments so a retry can
            # reuse the same sequence number.
            if not committed:
                for prompt_id, seq in segments:
                    state.abort_segment(session_id, prompt_id, seq)
    finally:
        state.close()


def prune_state(config: Dict[str, Any]) -> None:
    """Apply the state TTL (throttled); never raises."""

    try:
        ttl_days = config.get("state", {}).get("ttl_days", 0)
        if not ttl_days or ttl_days <= 0:
            return
        state = StateStore(config["state"]["dir"])
        try:
            removed = state.maybe_prune(ttl_days)
        finally:
            state.close()
        if removed is not None:
            _log(config, "ttl prune: removed %d session(s)" % removed)
    except Exception:
        _log(config, "ttl prune failed: %s" % traceback.format_exc(), error=True)


def run(event: str) -> int:
    raw = ""
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}

    try:
        config = _load_for_event(event)
        _log(config, "event=%s session=%s" % (event, payload.get("session_id")))
        if not config.get("enabled", True):
            return 0
        if event == "seed":
            handle_seed(payload, config)
            return 0
        if event == "subagent_stop":
            # Subagent transcripts are emitted as subtrees by the parent
            # turn's Stop hook, never as standalone traces.
            _log(config, "subagent stop ignored (handled by parent turn)")
            return 0
        if event in ("post_tool_use", "post_tool_use_failure"):
            result = handle_tool(payload, event, config)
            _log(
                config,
                "done event=%s emitted=%d skipped=%d flush_ok=%s error=%s"
                % (
                    event,
                    result.emitted,
                    result.skipped,
                    result.flush_ok,
                    result.error,
                ),
                error=bool(result.error),
            )
            return 0
        if event in ("stop", "session_end"):
            result = handle_stop(payload, event, config)
            _log(
                config,
                "done event=%s emitted=%d skipped=%d flush_ok=%s error=%s"
                % (
                    event,
                    result.emitted,
                    result.skipped,
                    result.flush_ok,
                    result.error,
                ),
                error=bool(result.error),
            )
            prune_state(config)
            return 0
        _log(config, "unknown event %r" % event, error=True)
        return 0
    except ConfigError as exc:
        _log({}, "config error: %s" % exc, error=True)
        return 0
    except Exception:
        _log({}, "ERROR: %s" % traceback.format_exc(), error=True)
        return 0
