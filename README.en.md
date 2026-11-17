# cc-genai-hook

[中文说明](README.md)

Generic Claude Code GenAI hook. It reads Claude Code transcripts through hooks and emits
OpenTelemetry GenAI spans to any OTLP HTTP/protobuf backend.

- No backend-specific code: configure an endpoint and arbitrary headers.
- No Claude Code native telemetry required: the hook uses its own config file.
- Deterministic trace/span IDs: repeated hook runs produce the same IDs.
- Content is redacted/truncated and can be disabled.

## How it works

- **ordered mode (default):** the `Stop` hook emits `chat` and `execute_tool`
  spans in transcript order under an `invoke_agent` turn root, with one shared
  step counter per turn (`#1`, `#2`, ...) and +1 ms synthetic timestamps.
- **realtime mode (optional):** `PostToolUse` / `PostToolUseFailure` command
  hooks emit tool spans as tools finish.
- **Subagents:** the parent `Stop` parses subagent transcripts and nests a
  `subagent` container under the delegation tool span, in the same trace, up to
  depth 5. `SubagentStop` is ignored. Subagent subtrees are built in ordered
  mode only.
- Idempotency: SQLite state keyed by `(session_id, message_id)` and
  `(session_id, tool_use_id)`, plus deterministic span IDs.

## Install

Requires Python 3.9+ (tested on 3.9.6).

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -U pip setuptools wheel
pip install -e .

cc-genai-hook install \
  --mode command \
  --endpoint https://otel.example.com \
  --header x-api-key="$OTEL_API_KEY" \
  --header "Authorization=Bearer $OTEL_TOKEN" \
  --user "$(whoami)"
```

Use a full `/v1/traces` URL with `--endpoint-kind full`:

```bash
cc-genai-hook install \
  --endpoint https://backend.example.com/v1/traces \
  --endpoint-kind full
```

Install writes:

- `~/.claude/genai-hook.json` (0600)
- `Stop` and `SessionEnd` entries in `~/.claude/settings.json` (with a backup)

It does **not** write `CLAUDE_CODE_ENABLE_TELEMETRY`, `OTEL_TRACES_EXPORTER`,
`OTEL_LOG_*`, or any other native telemetry variable.

## Verify

```bash
cc-genai-hook doctor
cc-genai-hook doctor --send-test
cc-genai-hook config show
```

Dry-run without sending:

```bash
CC_GENAI_HOOK_DRY_RUN=1 claude -p "hello"
cat ~/.claude/state/genai-hook/dry-run.jsonl
```

End-to-end against a local receiver:

```bash
# terminal 1
python3 examples/local_otlp_receiver.py --port 4318

# terminal 2
cc-genai-hook install \
  --endpoint http://127.0.0.1:4318 \
  --header x-api-key=test \
  --user "$(whoami)"
cc-genai-hook doctor --send-test
```

## Configuration

Default path: `~/.claude/genai-hook.json`. See `examples/genai-hook.example.json`.

Important fields:

| Field | Meaning |
| --- | --- |
| `exporter.endpoint` | OTLP base URL (or full URL with `endpoint_kind=full`) |
| `exporter.headers` | Arbitrary headers, e.g. `x-api-key`, `Authorization` |
| `exporter.compression` | `gzip`, `deflate`, `none` |
| `resource.attributes` | Extra resource attributes, e.g. `enduser.id` |
| `content.mode` | `last_turn`, `full`, `none` |
| `content.include_thinking` | Include reasoning blocks (default `false`) |
| `content.redact_patterns` | Regex replacements |
| `hook.dry_run` | Write spans to `dry-run.jsonl` instead of sending |
| `state.dir` | SQLite state directory |
| `state.ttl_days` | Delete sessions idle longer than this many days, with their segment/message/tool rows (default `14`, `0` disables). Runs after Stop/SessionEnd, at most once per 24h. A pruned session that is resumed later re-sends its history once; span IDs are unchanged, so backends can dedupe. |

Environment overrides:

- `CC_GENAI_HOOK_CONFIG`
- `CC_GENAI_HOOK_ENABLED`
- `CC_GENAI_HOOK_DRY_RUN`
- `CC_GENAI_HOOK_DEBUG`
- `CC_GENAI_HOOK_CONTENT_MODE`
- `OTEL_EXPORTER_OTLP_ENDPOINT` / `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`
- `OTEL_EXPORTER_OTLP_HEADERS` / `OTEL_EXPORTER_OTLP_TRACES_HEADERS`

## Commands

```text
cc-genai-hook install [--mode command] [--endpoint URL] [--header K=V]...
cc-genai-hook uninstall [--purge]
cc-genai-hook doctor [--send-test]
cc-genai-hook config show
cc-genai-hook config validate
cc-genai-hook seed --session-id ID --transcript PATH
cc-genai-hook hook stop|session_end|subagent_stop|post_tool_use|post_tool_use_failure|seed
```

`seed` marks an existing transcript as already emitted, avoiding a backfill burst.

## Development

```bash
PYTHONPATH=src python3 -m pytest -q
```

Design, milestones and implementation status: [`docs/design.md`](docs/design.md).

## Security

- Command hooks run with your user permissions.
- `~/.claude/genai-hook.json` contains credentials; keep it `0600`.
- Content attributes can include prompts, file contents, and tool outputs. Use
  `content.mode=none` or redaction patterns if that is not acceptable.
- The hook never writes to stdout; it exits 0 even on failure so it cannot block
  Claude Code. Check `~/.claude/state/genai-hook/hook.err.log` for failures.

## License

[MIT](LICENSE)
