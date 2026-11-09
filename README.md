# cc-genai-hook

通用的 Claude Code GenAI hook。它通过 Claude Code hooks 读取会话 transcript，生成符合
OpenTelemetry GenAI semantic conventions 的 span，并导出到**任意 OTLP HTTP/protobuf 后端**。

- 不绑定具体后端：只配置 OTLP endpoint 和任意 header。
- 不依赖 Claude Code 原生 trace：使用独立配置文件，不需要开启原生 telemetry。
- 确定性 trace/span ID：重复 hook 重放得到相同的 ID。
- 内容默认脱敏、可截断、可关闭。
- 支持 command 基线模式和 sidecar 实时模式（sidecar 在 M4 实现）。

## 当前状态

**M1（已完成）**

- `command` 模式：`Stop`、`SessionEnd` command hooks
- transcript 解析：`promptId` 分组、assistant 多行合并、tool_result 索引
- `invoke_agent` segment root + `chat` spans
- 确定性 trace_id / span_id
- SQLite 幂等状态，重复 Stop/SessionEnd 不重复发送
- 通用 OTLP endpoint / headers 配置
- `install`、`uninstall`、`doctor`、`config`、`seed`、`hook` CLI
- dry-run JSONL、本地 OTLP receiver 示例

**M2（已完成）**

- **ordered 模式（默认）**：Stop 时按 transcript 顺序统一生成 chat + execute_tool
  - 每个 turn 内共享 step counter：`#1`、`#2`、`#3`…
  - 下一段活动从上一段结束 +1ms 开始，保证时间轴严格串行
  - `claude_code.genai_hook.step_index` / `step_name` / `plan_step`
- **realtime 模式（可选）**：`PostToolUse` / `PostToolUseFailure` 实时发 tool span
- `execute_tool` spans：工具名、调用 ID、入参、返回、错误、耗时
- 确定性 tool span ID + `tool_use_id` 幂等
- `error.type`、`is_interrupt`、工具失败状态
- 工具 span 与 chat 一样挂在 turn root 下

**M3（已完成）**

- 父会话 `Stop` hook 统一解析 subagent transcript，不单独发 subagent trace
- 通过 `toolUseResult.agentId` 识别 Agent/Task delegation
- 查找 `subagents/agent-<agentId>.jsonl`
- delegation tool span 下创建 `subagent` 容器
- 子代理内部步骤从 `#1` 重新编号，挂在容器下
- 父 tool span 的 end 扩展到 subagent end，保证区间包含
- 递归深度上限 5
- `SubagentStop` 被忽略，避免重复 trace
- 子代理子树只在 **ordered** 模式构建；realtime 模式只发 delegation tool span，不展开子代理

**M4 / M5（计划中）**

- M4：**sidecar 实时模式**
  - sidecar/HTTP hooks、open segment span、实时 `PostToolUse`
- M5：durable outbox、采样、完整脱敏、TTL 清理

## 工作原理

```text
Claude Code
  │
  ├─ Stop / SessionEnd command hook
  │      │
  │      ▼
  │  cc-genai-hook hook stop
  │      │ 读 transcript JSONL
  │      │ 按 promptId 分组 assistant 消息
  │      │ 生成 invoke_agent / chat spans
  │      │
  ├─ PostToolUse / PostToolUseFailure command hook
  │      │
  │      ▼
  │  cc-genai-hook hook post_tool_use
  │      │ 生成 execute_tool span
  │      │
  │      ▼
  │  OTLP HTTP/protobuf
  │      │
  │      ▼
  └─ 任意 OTLP 后端（SignSight / SigNoz / Jaeger / Tempo / Collector ...）
```

trace 拓扑：

```text
invoke_agent（turn root）
├── chat（每条 assistant 消息）
├── execute_tool（每次工具调用）
└── execute_tool（Agent/Task delegation）
    └── subagent（容器）
        ├── chat
        ├── execute_tool
        └── subagent response
```

M2 默认使用 **ordered 模式**：Stop 时按 transcript 顺序统一生成 chat 和
execute_tool，共享一个 step counter，并用 `+1ms` 合成时间戳保证严格顺序。
也可以切换到 **realtime 模式**：用 PostToolUse/Failure command hooks 实时
发送 tool span，工具 span 使用确定性 ID 并挂到 turn root。

M3 由父会话 Stop hook 解析 subagent transcript，把 `subagent` 容器挂在
delegation tool span 下，子代理内部使用自己的 step counter。M4 的 sidecar
会提供跨事件 open span，从而支持多 segment turn 的精确实时挂载。

## 安装

要求 Python 3.9+（已在 3.9.6 验证）。

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -U pip setuptools wheel
pip install -e .
```

### 通用 OTLP 后端

```bash
cc-genai-hook install \
  --mode command \
  --endpoint https://otel.example.com \
  --header x-api-key="$OTEL_API_KEY" \
  --header "Authorization=Bearer $OTEL_TOKEN" \
  --user "$(whoami)"
```

### 自定义 header

后端需要自定义认证时，`--header` 可以重复：

```bash
cc-genai-hook install \
  --mode command \
  --endpoint https://otel.example.com \
  --header x-api-key="$OTEL_API_KEY" \
  --header x-tenant-id="$OTEL_TENANT_ID" \
  --user "$(whoami)"
```

### 完整 `/v1/traces` 路径

```bash
cc-genai-hook install \
  --mode command \
  --endpoint https://backend.example.com/v1/traces \
  --endpoint-kind full \
  --header "Authorization=Bearer $OTEL_TOKEN"
```

安装器会写入：

```text
~/.claude/genai-hook.json         # 0600
~/.claude/settings.json           # 默认合并 Stop / SessionEnd；realtime 模式再加 PostToolUse / PostToolUseFailure，并自动备份
```

安装器**不会**写入 `CLAUDE_CODE_ENABLE_TELEMETRY`、`OTEL_TRACES_EXPORTER`、
`OTEL_LOG_*` 等 Claude Code 原生 telemetry 变量。

## 配置

默认路径：`~/.claude/genai-hook.json`。完整示例见
[`examples/genai-hook.example.json`](examples/genai-hook.example.json)。

```json
{
  "version": 1,
  "enabled": true,
  "exporter": {
    "protocol": "http/protobuf",
    "endpoint": "https://otel.example.com",
    "endpoint_kind": "base",
    "headers": {
      "x-api-key": "${OTEL_API_KEY}",
      "Authorization": "Bearer ${OTEL_TOKEN}"
    },
    "compression": "gzip",
    "timeout_ms": 30000,
    "batch": {
      "max_size": 512,
      "schedule_delay_ms": 500
    }
  },
  "resource": {
    "service.name": "claude-code",
    "attributes": {
      "enduser.id": "${USER}"
    }
  },
  "content": {
    "mode": "last_turn",
    "include_thinking": false,
    "max_input_chars": 200000,
    "max_message_chars": 100000,
    "max_tool_chars": 100000,
    "redact_patterns": []
  },
  "compat": {
    "messages_format": "json",
    "cache_write_attribute": "cache_creation",
    "emit_provider_name": true,
    "emit_system_legacy": true
  },
  "hook": {
    "sample_rate": 1.0,
    "dry_run": false,
    "debug": false,
    "agent_name": "claude-code",
    "tool_mode": "ordered"
  },
  "state": {
    "dir": "~/.claude/state/genai-hook",
    "ttl_days": 14
  }
}
```

### Endpoint 规则

| 配置 | 行为 |
| --- | --- |
| `exporter.endpoint` + `endpoint_kind=base` | 自动追加 `/v1/traces` |
| `exporter.endpoint` + `endpoint_kind=full` | 按原样使用 |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | 作为 base，自动追加 `/v1/traces` |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | 按原样使用，优先级最高 |

### Header 规则

优先级：

```text
OTEL_EXPORTER_OTLP_TRACES_HEADERS
  > OTEL_EXPORTER_OTLP_HEADERS
  > config.exporter.headers
```

支持 `${ENV_VAR}` 展开。复杂 header 建议直接写 JSON 配置，不要塞进 `OTEL_EXPORTER_OTLP_HEADERS`。

### 环境变量

| 变量 | 作用 |
| --- | --- |
| `CC_GENAI_HOOK_CONFIG` | 配置文件路径 |
| `CC_GENAI_HOOK_ENABLED` | 总开关 |
| `CC_GENAI_HOOK_DRY_RUN` | 只写 `dry-run.jsonl`，不发送 |
| `CC_GENAI_HOOK_DEBUG` | debug 日志 |
| `CC_GENAI_HOOK_CONTENT_MODE` | `last_turn` / `full` / `none` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | 通用 OTLP base endpoint |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | traces 专用 endpoint |
| `OTEL_EXPORTER_OTLP_HEADERS` | 通用 OTLP header |
| `OTEL_EXPORTER_OTLP_TRACES_HEADERS` | traces 专用 header |
| `OTEL_RESOURCE_ATTRIBUTES` | 合并进 resource |

## 验证

```bash
cc-genai-hook doctor
cc-genai-hook doctor --send-test
cc-genai-hook config show
cc-genai-hook config validate
```

dry-run：

```bash
CC_GENAI_HOOK_DRY_RUN=1 claude -p "hello"
cat ~/.claude/state/genai-hook/dry-run.jsonl
```

本地 OTLP receiver 端到端验证：

```bash
# 终端 1
python3 examples/local_otlp_receiver.py --port 4318

# 终端 2
cc-genai-hook install \
  --endpoint http://127.0.0.1:4318 \
  --header x-api-key=test \
  --user "$(whoami)"
cc-genai-hook doctor --send-test
```

receiver 会打印每个 span 的 `trace_id`、`span_id`、`parent_span_id`、属性和事件。

## CLI

```text
cc-genai-hook install [--mode command|sidecar|auto]
                      [--endpoint URL] [--endpoint-kind base|full]
                      [--header KEY=VALUE]... [--user NAME]
                      [--content-mode last_turn|full|none]
                      [--tool-mode ordered|realtime]
                      [--dry-run]
cc-genai-hook uninstall [--purge]
cc-genai-hook doctor [--send-test]
cc-genai-hook config show [--redact]
cc-genai-hook config validate
cc-genai-hook seed --session-id ID --transcript PATH
cc-genai-hook hook stop|session_end|subagent_stop|post_tool_use|post_tool_use_failure|seed
```

默认 `--tool-mode ordered`：Stop 时统一生成 chat/tool，保证顺序。  
`--tool-mode realtime` 会额外注册 `PostToolUse` / `PostToolUseFailure`，工具 span 实时发送。

`subagent_stop` 事件保留用于兼容，但会被忽略：子代理 trace 由父会话 `Stop`
统一解析 `subagents/agent-<id>.jsonl` 生成。

当前只实现了 `--mode command`。`--mode sidecar` 会在 M4 提供。

### seed

安装后第一次 `Stop` 会把现有 transcript 的历史 assistant 消息全部补发。如果不想回灌历史：

```bash
cc-genai-hook seed --session-id <session-id> --transcript <transcript-path>
```

## Span 与 gen_ai 字段

### `invoke_agent`

- `gen_ai.operation.name=invoke_agent`
- `gen_ai.provider.name=anthropic`
- `gen_ai.agent.name=claude-code`
- `gen_ai.conversation.id`
- `gen_ai.input.messages` / `gen_ai.output.messages`
- `gen_ai.usage.input_tokens` / `output_tokens` / `cache_read` / `cache_creation`
- `gen_ai.response.finish_reasons`
- `claude_code.genai_hook.synthetic=true`
- `claude_code.genai_hook.turn.prompt_id`
- `claude_code.genai_hook.turn.segment`

### `chat`

- `gen_ai.operation.name=chat`
- `gen_ai.provider.name=anthropic`
- `gen_ai.request.model` / `gen_ai.response.model`
- `gen_ai.response.id`
- `gen_ai.response.finish_reasons`
- `gen_ai.conversation.id`
- `gen_ai.usage.*`
- `gen_ai.input.messages` / `gen_ai.output.messages`
- `claude_code.genai_hook.message.uuid`
- `claude_code.genai_hook.request_ref`
- `claude_code.genai_hook.timing.source=transcript`

### Messages 格式

`gen_ai.input.messages` / `gen_ai.output.messages` 严格使用 GenAI semconv 的
`role + parts` 结构，而不是 Anthropic 原始的 `role + content`：

```json
[
  {
    "role": "user",
    "parts": [{"type": "text", "content": "read the file"}]
  },
  {
    "role": "assistant",
    "parts": [
      {
        "type": "tool_call",
        "id": "call_1",
        "name": "Read",
        "arguments": {"file_path": "/tmp/x"}
      }
    ],
    "finish_reason": "tool_use"
  },
  {
    "role": "tool",
    "parts": [
      {
        "type": "tool_call_response",
        "id": "call_1",
        "response": "file contents"
      }
    ]
  }
]
```

Claude Code transcript 里 `tool_result` 位于 user 消息中；归一化后会拆成独立的
`role=tool` 消息，并使用 `tool_call_response` part。图片使用 `blob` 或 `uri`
part；未知 block 使用 `anthropic.<type>` 的 GenericPart。

### `execute_tool`

- `gen_ai.operation.name=execute_tool`
- `gen_ai.provider.name=anthropic`
- `gen_ai.tool.name`
- `gen_ai.tool.call.id`
- `gen_ai.tool.call.arguments`
- `gen_ai.tool.call.result`
- `gen_ai.conversation.id`
- `error.type`：失败时为 `tool_error` 或 `interrupted`
- `claude_code.genai_hook.tool.duration_ms`
- `claude_code.genai_hook.tool.success`
- `claude_code.genai_hook.tool.is_interrupt`
- `claude_code.genai_hook.tool.error`
- `claude_code.genai_hook.tool.parent_segment=0`

工具内容同样受 `content.mode`、`max_tool_chars`、`redact_patterns` 控制；
截断时写 `claude_code.genai_hook.tool.*.truncated=true`。

### `subagent`

- span name：`subagent`
- `gen_ai.operation.name=invoke_agent`
- `gen_ai.agent.id` / `gen_ai.agent.name`
- `gen_ai.conversation.id`
- `claude_code.genai_hook.subagent=true`
- `claude_code.genai_hook.parent.tool_use_id`
- `claude_code.genai_hook.step_count`
- parent = delegation tool span
- 子步骤的 parent = subagent container，本地 step counter 从 `#1` 开始

### 顺序属性

ordered 模式下，一个 turn 内的 chat 和 tool 共享 step counter：

- `claude_code.genai_hook.step_index`：从 1 开始的逻辑步骤号
- `claude_code.genai_hook.step_name`：如 `plan (1 tool) #1`、`tool: read #2`、`response #3`
- `claude_code.genai_hook.plan_step`：tool span 所属的 generation 步骤
- `claude_code.genai_hook.step_count`：root span 上的总步骤数

时间戳规则：下一段活动从上一段结束 +1ms 开始，并尽量 clamp 到真实首条
timestamp，保证 trace 时间轴严格串行。

### 内容处理

- `content.mode=last_turn`：只发当前 turn。
- `content.mode=full`：发完整会话历史。
- `content.mode=none`：只发结构，不发内容。
- `include_thinking=false`：默认不发 thinking。
- `redact_patterns`：正则替换敏感内容。
- 截断后写 `claude_code.genai_hook.*.truncated=true`。

## 状态与幂等

- 状态目录：`~/.claude/state/genai-hook/`
- SQLite 表：`sessions`、`segments`、`messages`、`tools`
- 以 `(session_id, message_id)` 为幂等键，重复 Stop/SessionEnd 不会重复发 chat。
- 以 `(session_id, tool_use_id)` 为幂等键，重复 PostToolUse/PostToolUseFailure 不会重复发 tool。
- 确定性 span_id 作为第二层保护：即使状态丢失，重放也会得到相同 ID。
- `segments` 在导出成功后才标记完成；flush 失败不会把消息/tool 标记为已处理。

## 安全与隐私

- Command hooks 以当前用户权限运行。
- `~/.claude/genai-hook.json` 包含凭证，权限 `0600`。
- Hook 失败不会阻塞 Claude Code：入口捕获异常并 `exit 0`。
- 失败日志：`~/.claude/state/genai-hook/hook.err.log`。
- 内容可能包含 prompt、文件内容和工具输出；请用 `content.mode` 和 `redact_patterns` 控制。
- 不开启 Claude Code 原生 trace，避免与合成 trace 重复计数。

## 目录结构

```text
cc-genai-hook/
├── pyproject.toml
├── README.md                 # 中文
├── README.en.md              # English
├── docs/
│   └── design.md             # 设计文档
├── examples/
│   ├── genai-hook.example.json
│   ├── settings.command.json
│   └── local_otlp_receiver.py
├── src/cc_genai_hook/
└── tests/
```

## Agent Skill

安装和诊断步骤也提供为 Agent Skill：

```text
skills/cc-genai-hook-install/SKILL.md
```

它只描述通用的 cc-genai-hook 安装、配置和诊断，不绑定任何具体后端。

## 开发

```bash
PYTHONPATH=src python3 -m pytest -q
```

当前测试：34 passed。

## Roadmap

| 阶段 | 内容 |
| --- | --- |
| M1 | ✅ command 模式、Stop/SessionEnd、chat/root spans、SQLite 幂等 |
| M2 | ✅ command 模式丰富内容：ordered step 顺序、PostToolUse/Failure（realtime）、execute_tool、工具入参/返回/错误/耗时 |
| M3 | ✅ 子代理 trace：父 Stop 解析 subagent transcript、subagent 容器、本地 step 顺序、同一 trace |
| M4 | ⏳ sidecar/HTTP hooks、实时 open span、实时 PostToolUse |
| M5 | ⏳ durable outbox、采样、完整脱敏、TTL 清理 |
| M6 | ⏳ 灰度上线、后端看板、文档 |

详细设计见 [`docs/design.md`](docs/design.md)。

## English

[English README](README.en.md)

## License

[MIT](LICENSE)
