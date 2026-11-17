---
name: cc-genai-hook-install
description: |
  Install, configure and troubleshoot cc-genai-hook, which reads Claude Code
  transcripts through command hooks, builds gen_ai invoke_agent / chat /
  execute_tool / subagent spans, and exports them to any OTLP HTTP/protobuf backend.
  Triggers: cc-genai-hook, claude code trace, claude code observability,
  claude code 可观测, agent observability, otel, gen_ai, tool span, subagent trace,
  trace export.
---

# Install and troubleshoot cc-genai-hook

> **For the AI agent running this skill:**
> You are installing and troubleshooting **cc-genai-hook**. It handles **Claude Code**
> only: command hooks read the transcript, the hook builds its own OTel spans, and it
> sends them to the OTLP HTTP/protobuf backend the user specifies.
>
> Follow the steps in order. At every **ASK THE USER**, stop and ask. Never guess an
> endpoint or header. **Never echo API keys or other secrets.** The installer writes
> headers to a `0600` config file.

---

## Step 0 — Scope

cc-genai-hook supports:

- Claude Code CLI / IDE / Desktop sessions
- Any OTLP HTTP/protobuf backend
- Any headers (for example `x-api-key`, `Authorization`, `x-tenant-id`)

It does not support:

- Hermes / OpenClaw / Opencode
- gRPC OTLP (convert to HTTP/protobuf first)

---

## Step 1 — Check the environment

```bash
test -d "$HOME/.claude" && echo "CLAUDE_CODE=yes"
claude --version 2>/dev/null || true
python3 --version
```

Requirements:

- Claude Code is installed and supports command hooks; `2.1.300+` recommended
- Python `3.9+` (`3.11/3.12` recommended)
- The current user can write `~/.claude/settings.json`

If `~/.claude` does not exist, **ASK THE USER** to confirm that Claude Code is installed.

---

## Step 2 — Get the OTLP endpoint and headers

You need from the user:

1. **Endpoint**
   - The OTLP HTTP endpoint of the target backend
   - Usually a base URL, for example `https://otel.example.com`
   - cc-genai-hook appends `/v1/traces`
   - If the user gives a full `/v1/traces` URL, install with `--endpoint-kind full`

2. **Headers**
   - The authentication headers the backend requires
   - For example:
     - `x-api-key: <key>`
     - `Authorization: Bearer <token>`
     - `x-tenant-id: <tenant>`
   - There can be more than one

### 2a. Check for an existing config

```bash
test -f "$HOME/.claude/genai-hook.json" && \
  python3 -c "
import json, os
p=os.path.expanduser('~/.claude/genai-hook.json')
c=json.load(open(p))
e=c.get('exporter',{})
print('endpoint:', e.get('endpoint'))
print('headers:', list((e.get('headers') or {}).keys()))
"
```

If a config exists, **ASK THE USER**:

> I found an existing cc-genai-hook config. Do you want to reuse it, or update it
> with a new endpoint / headers?

Do not print header values.

### 2b. If there is no config

**ASK THE USER**:

> I need the **endpoint** of your OTLP backend and the **headers** it requires
> (names and values), for example `x-api-key=...` or `Authorization=Bearer ...`.
> Please send them; I will not echo any secrets.

Wait for the answer. Do not make values up.

### 2c. Validate

- The endpoint must start with `http://` or `https://`
- If the endpoint ends with `/v1/traces`, use `--endpoint-kind full`
- Headers must not be empty

Store them in shell variables:

```bash
ENDPOINT="<user endpoint>"
API_KEY="<user key>"
```

---

## Step 3 — Install cc-genai-hook

### 3a. Check the CLI

```bash
command -v cc-genai-hook && cc-genai-hook --version
```

If a working version exists:

```bash
CCG="$(command -v cc-genai-hook)"
```

Otherwise install it to a stable directory. **Do not** put the venv in a temporary
directory, because the hook commands reference it.

```bash
CCGH_HOME="$HOME/.local/share/cc-genai-hook"
mkdir -p "$CCGH_HOME"

if [ -d "$CCGH_HOME/src/.git" ]; then
  git -C "$CCGH_HOME/src" pull --ff-only
else
  git clone https://github.com/clickzetta/cc-genai-hook.git "$CCGH_HOME/src"
fi

python3 -m venv "$CCGH_HOME/venv"
"$CCGH_HOME/venv/bin/pip" install -U pip setuptools wheel
"$CCGH_HOME/venv/bin/pip" install "$CCGH_HOME/src"
CCG="$CCGH_HOME/venv/bin/cc-genai-hook"
"$CCG" --version
```

### 3b. Install the hooks

The default is **ordered mode**: Stop emits chat / execute_tool / subagent spans
together, in strict order.

```bash
"$CCG" install \
  --mode command \
  --endpoint "$ENDPOINT" \
  --header "x-api-key=$API_KEY" \
  --user "$(whoami)"
```

If the endpoint is already a full `/v1/traces` URL:

```bash
"$CCG" install \
  --mode command \
  --endpoint "$ENDPOINT" \
  --endpoint-kind full \
  --header "x-api-key=$API_KEY" \
  --user "$(whoami)"
```

Multiple headers:

```bash
"$CCG" install \
  --mode command \
  --endpoint "$ENDPOINT" \
  --header "x-api-key=$API_KEY" \
  --header "x-tenant-id=$TENANT_ID" \
  --user "$(whoami)"
```

### 3c. Optional: realtime mode

If tool spans should be sent as soon as each tool finishes:

```bash
"$CCG" install \
  --mode command \
  --tool-mode realtime \
  --endpoint "$ENDPOINT" \
  --header "x-api-key=$API_KEY" \
  --user "$(whoami)"
```

- `ordered` (default): everything is sent at Stop, in strict order
- `realtime`: also registers PostToolUse / PostToolUseFailure, so tool spans are sent immediately
- realtime mode does not expand subagent subtrees

### 3d. What the installer does

- Writes `~/.claude/genai-hook.json` (`0600`)
- Backs up and merges `~/.claude/settings.json`
- Adds these hooks by default:
  - `Stop`
  - `SessionEnd`
- Realtime mode also adds:
  - `PostToolUse`
  - `PostToolUseFailure`
- Does **not** write any Claude Code native telemetry variables:
  - `CLAUDE_CODE_ENABLE_TELEMETRY`
  - `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA`
  - `OTEL_TRACES_EXPORTER`
  - `OTEL_LOG_*`

---

## Step 4 — Verify the setup

```bash
"$CCG" doctor
"$CCG" doctor --send-test
"$CCG" config show
```

Expected:

- `endpoint` is `$ENDPOINT/v1/traces`
- `protocol` is `http/protobuf`
- `headers` shows names only
- `hooks` includes at least `Stop` and `SessionEnd`
- `doctor --send-test` prints `test span sent`

If it fails, check:

1. Whether the endpoint is reachable
2. The header names and values
3. Whether `/v1/traces` was included by mistake
4. Whether `--endpoint-kind full` is needed
5. Proxy / firewall / DNS

Local dry-run:

```bash
CC_GENAI_HOOK_DRY_RUN=1 claude -p "hello"
cat "$HOME/.claude/state/genai-hook/dry-run.jsonl"
```

Dry-run only writes a local file and sends nothing.

---

## Step 5 — Trace structure

```text
invoke_agent (turn root)
├── chat
├── execute_tool
└── execute_tool (Agent/Task delegation)
    └── subagent (container)
        ├── chat
        ├── execute_tool
        └── subagent response
```

Key attributes:

- `gen_ai.operation.name`
- `gen_ai.request.model` / `gen_ai.response.model`
- `gen_ai.usage.*`
- `gen_ai.conversation.id`
- `gen_ai.tool.name` / `gen_ai.tool.call.id`
- `gen_ai.tool.call.arguments` / `gen_ai.tool.call.result`
- `gen_ai.input.messages` / `gen_ai.output.messages`
- `gen_ai.agent.id` / `gen_ai.agent.name` (subagent)
- `claude_code.genai_hook.step_index` / `step_name` / `plan_step` / `step_count`

---

## Step 6 — Troubleshooting

### Q1. `cc-genai-hook: command not found`

Use the absolute path:

```bash
"$HOME/.local/share/cc-genai-hook/venv/bin/cc-genai-hook" doctor
```

### Q2. Endpoint returns 404

- Do not include `/v1/traces` in a base endpoint
- For a full path, use `--endpoint-kind full`
- Do not guess the collector domain from the web UI domain

### Q3. 401 / 403

- Wrong header name or value
- Wrong API key type
- Check whether the backend expects `Authorization` instead of `x-api-key`

### Q4. No subagent subtree

- Confirm `tool_mode=ordered`
- Confirm the Agent/Task tool result contains `toolUseResult.agentId`
- Confirm `subagents/agent-<id>.jsonl` exists
- Realtime mode does not expand subagents

### Q5. Too much content / sensitive data

Edit `~/.claude/genai-hook.json`:

```json
{
  "content": {
    "mode": "last_turn",
    "include_thinking": false,
    "redact_patterns": ["(?i)bearer\\s+\\S+"]
  }
}
```

- `mode=none`: send structure only
- `mode=last_turn`: send the current turn only
- Each new hook process reads the updated config

### Q6. Python / pip dependencies

Use a stable venv:

```bash
CCGH_HOME="$HOME/.local/share/cc-genai-hook"
mkdir -p "$CCGH_HOME"
git clone https://github.com/clickzetta/cc-genai-hook.git "$CCGH_HOME/src"
python3 -m venv "$CCGH_HOME/venv"
"$CCGH_HOME/venv/bin/pip" install -U pip setuptools wheel
"$CCGH_HOME/venv/bin/pip" install "$CCGH_HOME/src"
```

### Q7. `--mode sidecar` fails

Sidecar mode is not implemented. Only this mode works:

```bash
--mode command
```

---

## Step 7 — Upgrade / uninstall

Upgrade:

```bash
CCGH_HOME="$HOME/.local/share/cc-genai-hook"
git -C "$CCGH_HOME/src" pull --ff-only
"$CCGH_HOME/venv/bin/pip" install -U "$CCGH_HOME/src"
"$CCG" install --mode command \
  --endpoint "$ENDPOINT" \
  --header "x-api-key=$API_KEY" \
  --user "$(whoami)"
```

Remove the hooks:

```bash
"$CCG" uninstall
```

Also delete the config and state:

```bash
"$CCG" uninstall --purge
```

---

## Step 8 — Report the changes

Tell the user:

- The endpoint domain
- tool_mode: `ordered` / `realtime`
- Files written:
  - `~/.claude/genai-hook.json`
  - `~/.claude/settings.json`
- The hooks installed
- The `doctor` / `doctor --send-test` results
- A reminder to check the backend's trace explorer

---

## Don'ts

- Do not echo API keys or other secrets
- Do not use gRPC OTLP
- Do not write Claude Code native telemetry variables
- Do not overwrite `~/.claude/settings.json` without a backup
- Do not treat `--mode sidecar` as implemented
- Do not build a subagent subtree when `toolUseResult.agentId` is missing
