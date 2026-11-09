# Claude Code GenAI Hook 设计文档

- **状态**：M1 实现中，全部关键决策已确认（2026-11-04）
- **日期**：2026-11-04
- **范围**：通过 Claude Code hooks 生成符合 gen_ai 语义的 trace，并导出到任意 OTLP HTTP/protobuf 后端
- **相关文件**：`cc-genai-hook/`（实现代码、安装器、测试和示例）
- **目标读者**：Claude Code hook 维护者、可观测性平台接入方、SRE

---

## 0. 决策摘要（2026-11-04）

| # | 决策 | 结论 |
| --- | --- | --- |
| 1 | 部署模式 | **同时实现 command 和 sidecar 两种模式** |
| 2 | Trace 边界 | **per-turn trace**，`prompt_id` 作为 turn key |
| 3 | `execute_tool` 父节点 | **挂在 segment root 下** |
| 5 | 子代理 trace | **与父 turn 同一个 trace** |
| 6 | 原生 trace | **不开启**，不依赖原生 trace |
| 4 | 内容默认值 | **`last_turn`**，`include_thinking=false`，默认脱敏 |
| 7 | 首批后端 | **通用 OTLP + 任意 header**；SignSight 仅作为示例 |
| 8 | 采样/保留 | **`sample_rate=1.0`，TTL 14 天** |
| 9 | 命名空间 | **`claude_code.genai_hook.*`** |
| 10 | 安装方式 | **`ensure` 默认；launchd/systemd 可选** |

---

## 1. 术语

| 术语 | 含义 |
| --- | --- |
| Hook | Claude Code 生命周期回调，支持 command / http / mcp_tool / prompt / agent 五种 handler |
| Sidecar / hookd | 本方案中的本地常驻进程，接收 hook 事件、维护状态、导出 span |
| Segment | 一个 `invoke_agent` span 覆盖的执行区间；一个 user turn 可能包含多个 segment |
| Turn | 以 `promptId` 标识的一轮用户请求 |
| Synthetic trace | 由 hook 从 transcript / hook payload 重建的 trace，不是 Claude Code 原生 trace |
| OTLP | OpenTelemetry Protocol；本方案默认 HTTP/protobuf，endpoint 通常以 `/v1/traces` 结尾 |

---

## 2. 背景与目标

### 2.1 背景

Claude Code 2.1.300+ 原生 OTel trace 已经包含部分 gen_ai 属性：

- `gen_ai.provider.name`
- `gen_ai.conversation.id`
- `gen_ai.request.model`
- `gen_ai.response.finish_reasons`
- `gen_ai.tool.name`
- `gen_ai.tool.call.id`
- `gen_ai.operation.name`（部分 span）

但仍缺少或无法直接获得：

- `gen_ai.usage.input_tokens` / `output_tokens` / cache 字段的 semconv 命名
- `gen_ai.input.messages` / `gen_ai.output.messages`
- `gen_ai.tool.call.arguments` / `gen_ai.tool.call.result`
- `llm_request` span 上的 `gen_ai.operation.name=chat`
- 完整的工具入参/返回和消息内容

Claude Code hooks 可以在 `Stop`、`PostToolUse`、`SubagentStop` 等时机拿到 transcript 路径、tool input/output、session id 等数据。因此可以用 hooks **重建**一份 gen_ai 语义的 trace。

### 2.2 目标

1. 输出符合 OpenTelemetry GenAI semantic conventions 的 `invoke_agent` / `chat` / `execute_tool` span。
2. 工具 span 近实时导出，不等到会话结束。
3. 支持任意 OTLP HTTP/protobuf 后端，endpoint 和 headers 可配置。
4. 与具体后端（SignSight、SigNoz、Jaeger、Tempo、Datadog 等）解耦。
5. 跨 hook 进程保持确定性 trace_id / span_id 和正确的父子关系。
6. 幂等、可重试、endpoint 故障不丢数据。
7. 内容默认安全可控，支持截断、脱敏、关闭内容。
8. 不要求修改 Claude Code 源码，不依赖原生 trace 开启。

### 2.3 非目标

1. 不提供与原生 trace 完全一致的时序、TTFT、API 重试、权限等待、流式事件。
2. 不替代 Claude Code 原生 OTel metrics/logs。
3. 不保证与原生 `claude_code.*` span 在同一 trace 内。
4. **v1 不开启 Claude Code 原生 trace**；不依赖 `OTEL_TRACES_EXPORTER` / `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA`，hook trace 是唯一 trace 来源。
5. 不在 v1 支持 gRPC OTLP；需要时通过本地 Collector 转换。
6. 不自动管理后端的数据保留、采样、告警策略。

### 2.4 为什么选择 hooks

| 方案 | 优点 | 缺点 |
| --- | --- | --- |
| 修改 Claude Code 源码 | 最完整 | 源码不可得、不可维护 |
| 原生 OTel + Collector | 稳定、时序准确 | 缺 messages/tool arguments/result，Collector 无法凭空生成 |
| 原生 detailed beta | 内容可进 span | 非生产可用、schema 不稳定、高 volume |
| **Hooks 重建** | 能拿到 transcript 和工具输入输出，不改源码 | 事后/近似、无原生 trace context |
| 自定义 plugin | 可扩展 | 不能给原生 span 加属性，需自己发 OTLP |

结论：hooks 适合做 **gen_ai 语义重建**，不适合做 **原生链路追踪**。本方案定位为前者，并与原生 trace 保持可选共存。

---

## 3. 设计原则

1. **Backend-agnostic**：代码中不出现任何后端专有名称；endpoint/header 全部来自配置。
2. **Standard-first**：优先输出标准 `gen_ai.*`；自定义属性统一放在 `claude_code.genai_hook.*` 命名空间。
3. **Never block Claude**：所有 hook 设置短 timeout + `onFailure: "continue"`；sidecar 故障不影响 Claude 执行。
4. **Deterministic & idempotent**：同一事件重放产生同样的 trace_id/span_id；状态和唯一键防重复。
5. **Config separate from native telemetry**：hook 配置放独立文件，不写 Claude Code 的 `env`，避免覆盖原生 OTel 配置。
6. **Content safe by default**：默认只发当前 turn，默认不发 thinking，支持正则脱敏和截断。
7. **Fail visible**：endpoint、依赖、flush 失败要可观测，不静默丢数据。

---

## 4. 总体架构

### 4.1 架构图

```text
Claude Code
  │
  ├─ SessionStart        (command) ──> ensure hookd 启动
  ├─ UserPromptSubmit    (http)    ──┐
  ├─ PostToolUse         (http)    ──┤
  ├─ PostToolUseFailure  (http)    ──┤
  ├─ Stop                (http)    ──┼──> cc_genai_hookd
  ├─ SubagentStart       (http)    ──┤       │
  ├─ SubagentStop        (http)    ──┤       ├─ SQLite state / outbox
  └─ SessionEnd          (http)    ──┘       ├─ OTel SDK / OTLP protobuf
                                             └─> OTLP backend
```

### 4.2 组件

| 组件 | 职责 |
| --- | --- |
| Hook 配置 | 注册 Claude Code 事件，指向本地 hookd 或直接执行 command hook |
| `cc_genai_hookd` | HTTP 服务，接收事件、构建 span、维护状态、导出 |
| SQLite | 会话/segment/message/tool/agent/outbox 状态 |
| Transcript parser | 解析 `transcript_path` / `agent_transcript_path` |
| Span builder | 把 hook/transcript 数据映射成 gen_ai 属性 |
| Exporter | OTel SDK BatchSpanProcessor 或 SQLite outbox + OTLP POST |

### 4.3 两种部署模式

| 模式 | 说明 | 适用 |
| --- | --- | --- |
| **Sidecar（实时模式）** | HTTP hooks + 常驻进程；可跨事件保持 open span | 需要近实时、正确 segment、低延迟 |
| **Command + spool（基线模式）** | 每个事件 command hook 追加事件到 spool；Stop/SessionEnd 时批量构建 | 无 daemon、环境禁止常驻进程、先验证 |

**已确认决策：两种模式都实现，都是一等公民。** 它们共用同一套 ID 规则、状态模型、transcript parser 和属性映射，安装器通过 `--mode command|sidecar|auto` 选择。Command 模式不是临时降级，而是无 daemon 环境下的正式基线；Sidecar 是实时增强。

---

## 5. 配置模型

### 5.1 配置文件

路径：`~/.claude/genai-hook.json`，权限 `0600`。

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
    "deployment.environment": "dev",
    "attributes": {
      "team": "platform"
    }
  },

  "content": {
    "mode": "last_turn",
    "include_thinking": false,
    "max_input_chars": 200000,
    "max_message_chars": 100000,
    "max_tool_chars": 100000,
    "redact_patterns": [
      "(?i)bearer\\s+\\S+",
      "(?i)api[_-]?key\\s*=\\s*\\S+"
    ]
  },

  "compat": {
    "messages_format": "json",
    "cache_write_attribute": "cache_creation",
    "emit_provider_name": true,
    "emit_system_legacy": true
  },

  "emit": {
    "invoke_agent": true,
    "chat": true,
    "execute_tool": true,
    "subagents": true
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

### 5.2 Endpoint 解析

遵循 OTel 规范：

| 优先级 | 来源 | 是否拼 `/v1/traces` |
| --- | --- | --- |
| 1 | `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | 否，按原样使用 |
| 2 | `exporter.traces_endpoint` | 否 |
| 3 | `OTEL_EXPORTER_OTLP_ENDPOINT` | 是 |
| 4 | `exporter.endpoint` + `endpoint_kind=base` | 是 |
| 5 | `exporter.endpoint` + `endpoint_kind=full` | 否 |

```python
def resolve_endpoint(cfg):
    ep = (
        os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
        or cfg["exporter"].get("traces_endpoint")
        or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
        or cfg["exporter"].get("endpoint")
    )
    if not ep:
        raise ConfigError("no OTLP endpoint configured")

    ep = ep.rstrip("/")
    full = (
        os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
        or cfg["exporter"].get("endpoint_kind") == "full"
    )
    return ep if full else ep + "/v1/traces"
```

### 5.3 Header 解析

支持：

- 配置文件中的 `headers` map；
- `OTEL_EXPORTER_OTLP_HEADERS`；
- `OTEL_EXPORTER_OTLP_TRACES_HEADERS`；
- `${ENV_VAR}` 展开。

优先级：signal-specific env > generic env > config。

```python
def resolve_headers(cfg):
    headers = {}
    headers.update(cfg["exporter"].get("headers") or {})
    headers.update(parse_otel_headers(os.getenv("OTEL_EXPORTER_OTLP_HEADERS")))
    headers.update(parse_otel_headers(os.getenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS")))
    return {k: expand_env(v) for k, v in headers.items()}
```

标准 env header 格式：

```text
key1=value1,key2=value2
```

复杂值（含逗号、等号）建议直接写 JSON 配置。

### 5.4 配置示例

**通用 OTLP Collector：**

```json
{
  "exporter": {
    "endpoint": "http://collector:4318",
    "headers": {
      "Authorization": "Bearer ${OTEL_TOKEN}"
    }
  }
}
```

**自定义 header（任意后端）：**

```json
{
  "exporter": {
    "endpoint": "https://otel.example.com",
    "headers": {
      "x-api-key": "${OTEL_API_KEY}",
      "x-tenant-id": "${OTEL_TENANT_ID}"
    }
  }
}
```

**完整路径后端：**

```json
{
  "exporter": {
    "endpoint": "https://backend.example.com/v1/traces",
    "endpoint_kind": "full"
  }
}
```

### 5.5 环境变量覆盖

| 变量 | 作用 |
| --- | --- |
| `CC_GENAI_HOOK_CONFIG` | 配置文件路径 |
| `CC_GENAI_HOOK_ENABLED` | 总开关 |
| `CC_GENAI_HOOK_DRY_RUN` | 只记录，不发送 |
| `CC_GENAI_HOOK_DEBUG` | debug 日志 |
| `CC_GENAI_HOOK_CONTENT_MODE` | `full` / `last_turn` / `none` |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `http/protobuf` |
| `OTEL_EXPORTER_OTLP_COMPRESSION` | `gzip` |
| `OTEL_RESOURCE_ATTRIBUTES` | 合并进 resource |

### 5.6 与原生 telemetry 的关系（已确认）

- v1 **不开启** Claude Code 原生 trace：不设置 `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA`，也不设置 `OTEL_TRACES_EXPORTER=otlp`。
- Hook 的 endpoint/header 放在独立配置文件 `~/.claude/genai-hook.json`，**不写入** `settings.json` 的 `env`，避免影响原生 metrics/logs/traces。
- 原生 metrics/logs 不在本方案范围内；如果用户需要，单独配置，并明确它们与 hook trace 是两套数据。
- 如果 managed settings 已经开启原生 trace，安装器应提示重复风险；建议二选一，或给 synthetic span 打标后在查询层去重。

---

## 6. Hook 事件与 Span 映射

### 6.1 事件映射

| Hook 事件 | 动作 | 关键输入 |
| --- | --- | --- |
| `SessionStart` | 确保 hookd 运行；创建 session；可选 seed | `session_id`, `source`, `model` |
| `UserPromptSubmit` | 创建 turn/segment，启动 `invoke_agent` | `prompt_id`, `prompt`, `session_id` |
| `PostToolUse` | 立即发 `execute_tool` | `tool_name`, `tool_input`, `tool_response`, `tool_use_id`, `duration_ms` |
| `PostToolUseFailure` | 发 `execute_tool`，status=ERROR | 上面的字段 + `error`, `is_interrupt` |
| `Stop` | 解析 transcript，发 `chat`；结束 segment root | `transcript_path`, `last_assistant_message`, `stop_hook_active` |
| `SubagentStart` | 记录 agent_id/type 与父 turn 映射 | `agent_id`, `agent_type` |
| `SubagentStop` | 解析 `agent_transcript_path`，发子代理 span | `agent_id`, `agent_type`, `agent_transcript_path` |
| `SessionEnd` | 关闭 open segment，flush outbox | `reason` |

不使用的 hook：

- `PreToolUse`：`PostToolUse.duration_ms` 已足够计算工具执行开始时间。
- `PermissionRequest`：权限等待时序不是本方案目标；后续可作为可选扩展。

### 6.2 Hook 配置示例

```json
{
  "allowedHttpHookUrls": ["http://127.0.0.1:4319/*"],
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.claude/hooks/cc_genai_hook.py ensure",
            "timeout": 10
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "http",
            "url": "http://127.0.0.1:4319/hooks/user_prompt_submit",
            "timeout": 5,
            "onFailure": "continue"
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "http",
            "url": "http://127.0.0.1:4319/hooks/post_tool_use",
            "timeout": 5,
            "onFailure": "continue"
          }
        ]
      }
    ],
    "PostToolUseFailure": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "http",
            "url": "http://127.0.0.1:4319/hooks/post_tool_use_failure",
            "timeout": 5,
            "onFailure": "continue"
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "http",
            "url": "http://127.0.0.1:4319/hooks/stop",
            "timeout": 10,
            "onFailure": "continue"
          }
        ]
      }
    ],
    "SubagentStart": [
      {
        "hooks": [
          {
            "type": "http",
            "url": "http://127.0.0.1:4319/hooks/subagent_start",
            "timeout": 5,
            "onFailure": "continue"
          }
        ]
      }
    ],
    "SubagentStop": [
      {
        "hooks": [
          {
            "type": "http",
            "url": "http://127.0.0.1:4319/hooks/subagent_stop",
            "timeout": 10,
            "onFailure": "continue"
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "http",
            "url": "http://127.0.0.1:4319/hooks/session_end",
            "timeout": 2,
            "onFailure": "continue"
          }
        ]
      }
    ]
  }
}
```

### 6.3 Hook 约束

- `SessionStart` 不支持 HTTP hook，只能用 command / mcp_tool。
- `SessionEnd` hooks 共享约 1.5 秒预算；HTTP handler 只入队并立即返回，flush 在 hookd 后台继续。
- 所有 HTTP hook 设置 `onFailure: "continue"`，hookd 故障不阻塞 Claude。
- `allowedHttpHookUrls` 必须是 hookd 的地址。
- 如果 sidecar 使用 token，通过 `headers` + `allowedEnvVars` 注入，不写死。

---

## 7. Span 设计

### 7.1 Trace 拓扑

```text
trace_id = hash(session_id + prompt_id)
│
└── invoke_agent (segment root, span_id = hash(session_id + prompt_id + seq))
    ├── chat (span_id = hash(message.id))
    └── execute_tool (span_id = hash(tool_use_id))
```

**已确认决策：`execute_tool` 挂在 segment root 下，不挂 `chat`。**

原因：

- `PostToolUse` 没有 assistant `message.id`；
- transcript 在 PostToolUse 时可能尚未写入对应 assistant 行；
- 挂 root 更稳定，也符合 GenAI semconv 的常见拓扑；
- 不需要为了找父节点去扫描可能滞后的 transcript。

`invoke_agent → chat → execute_tool` 不作为 v1 的目标；如果后续有明确需求，可以作为可选扩展（批量模式或 transcript 查找），不改变默认拓扑。

**M2 command 模式限制**：`PostToolUse` 在 Stop 之前触发，无法可靠知道当前 segment seq，因此 M2 的 tool span 固定挂到 segment 0 root。M4 sidecar 会维护 open segment，把 tool span 挂到当前 segment。

### 7.2 Segment 策略

**已确认决策：per-turn trace；一个 user turn 可能因 Stop hook 继续而分成多个 segment。**

```text
turn_key    = prompt_id
trace_id    = hash(session_id + prompt_id)
segment_seq = 0, 1, 2, ...
root_span   = hash("invoke_agent", session_id + prompt_id + seq)
```

- `UserPromptSubmit`：创建 segment 0，启动 root。
- `Stop`：结束当前 segment。
- 后续 `PostToolUse` / `Stop` 且没有 open segment：创建 seq+1。
- `SessionEnd`：关闭所有 open segment。

好处：

- 父 span 不会早于子 span 结束；
- Stop 继续后新 span 不会挂到已结束的 root；
- 同一 turn 不会重复发 root。

### 7.2.1 逻辑顺序（M2 ordered 模式）

`hook.tool_mode=ordered` 是 M2 默认模式。Stop 时按 transcript 顺序统一生成
chat 和 execute_tool：

```text
user prompt
  ↓
chat #1:   start = prev_end + 1ms，end = assistant 最后一行 timestamp
tool #2:   start = chat #1 end + 1ms，end = tool_result timestamp
chat #3:   start = tool #2 end + 1ms，end = assistant 最后一行 timestamp
...
invoke_agent root: start = user_ts，end = 所有 step 的最大 end
```

规则：

- 一个 turn 内 chat/tool 共享 step counter（从 1 开始）。
- 下一段活动从上一段结束 + 1ms 开始；若真实首条 timestamp 更早，则 clamp 到真实值。
- `claude_code.genai_hook.step_index`、`step_name`、`plan_step` 记录逻辑顺序。
- root 上的 `step_count` 记录总步骤数。
- chat/tool 都是 root 的子 span，彼此是 sibling。
- `hook.tool_mode=realtime` 保留 PostToolUse/Failure 实时发送 tool span；此时顺序由真实时间戳决定，不保证严格串行。

### 7.3 确定性 ID

```text
trace_id = sha256("cc:trace:{session_id}:{turn_key}")[:16]   # 32 hex
span_id  = sha256("cc:span:{kind}:{stable_key}")[:8]         # 16 hex
```

| Span | stable_key |
| --- | --- |
| `invoke_agent` | `session_id + prompt_id + segment_seq` |
| `chat` | `message.id` |
| `execute_tool` | `tool_use_id` |
| 子代理 `invoke_agent` | `agent_id + segment_seq` |

要求：

- 16 字节 trace_id、8 字节 span_id，不能全 0；
- 同一事件重放必须得到同样 ID；
- kind 前缀避免碰撞。

### 7.4 Python 实现

```python
import contextvars
import hashlib
from dataclasses import dataclass
from opentelemetry.sdk.trace.id_generator import IdGenerator

@dataclass
class Ids:
    trace_id: int
    span_id: int

_CURRENT = contextvars.ContextVar("cc_genai_ids")

def _hash_int(seed: str, nbytes: int) -> int:
    b = hashlib.sha256(seed.encode("utf-8")).digest()[:nbytes]
    if not any(b):
        b = bytes([1]) + b[1:]
    return int.from_bytes(b, "big")

def trace_id(session_id: str, turn_key: str) -> int:
    return _hash_int(f"cc:trace:{session_id}:{turn_key}", 16)

def span_id(kind: str, key: str) -> int:
    return _hash_int(f"cc:span:{kind}:{key}", 8)

class DeterministicIdGenerator(IdGenerator):
    def generate_trace_id(self) -> int:
        return _CURRENT.get().trace_id

    def generate_span_id(self) -> int:
        return _CURRENT.get().span_id
```

父 context：

```python
from opentelemetry import trace
from opentelemetry.trace import SpanContext, TraceFlags

def parent_ctx(tid: int, parent_sid: int):
    return trace.set_span_in_context(
        trace.NonRecordingSpan(
            SpanContext(
                trace_id=tid,
                span_id=parent_sid,
                is_remote=False,
                trace_flags=TraceFlags(TraceFlags.SAMPLED),
            )
        )
    )
```

### 7.5 属性映射

#### Resource

```text
service.name                    = claude-code
service.version                 = transcript.version
telemetry.source                = claude-code-genai-hook
enduser.id                      = 配置的 user
host.name / os.type / host.arch = 本机
claude_code.session.id          = session_id
```

#### `invoke_agent`

| 属性 | 来源 |
| --- | --- |
| `gen_ai.operation.name` | `"invoke_agent"` |
| `gen_ai.provider.name` | `"anthropic"` |
| `gen_ai.agent.name` | `claude-code` 或子代理 `agent_type` |
| `gen_ai.agent.id` | 子代理 `agent_id` |
| `gen_ai.conversation.id` | `session_id` |
| `gen_ai.input.messages` | prompt / turn input |
| `gen_ai.output.messages` | 最后一个 assistant 消息 |
| `gen_ai.usage.input_tokens` | sum(input + cache_read + cache_creation) |
| `gen_ai.usage.output_tokens` | sum(output) |
| `gen_ai.response.finish_reasons` | 最后一个 `stop_reason` |
| `claude_code.genai_hook.turn.prompt_id` | `prompt_id` |
| `claude_code.genai_hook.turn.segment` | `seq` |
| `claude_code.genai_hook.synthetic` | `true` |

#### `chat`

| 属性 | 来源 |
| --- | --- |
| `gen_ai.operation.name` | `"chat"` |
| `gen_ai.provider.name` | `"anthropic"` |
| `gen_ai.request.model` | `requestedModel` / `model` |
| `gen_ai.response.model` | `model` |
| `gen_ai.response.id` | `message.id`（或 `requestRef`，需确认语义） |
| `gen_ai.response.finish_reasons` | `[stop_reason]` |
| `gen_ai.usage.input_tokens` | `input_tokens + cache_read + cache_creation` |
| `gen_ai.usage.output_tokens` | `output_tokens` |
| `gen_ai.usage.cache_read.input_tokens` | `cache_read_input_tokens` |
| `gen_ai.usage.cache_creation.input_tokens` | `cache_creation_input_tokens` |
| `gen_ai.conversation.id` | `session_id` |
| `gen_ai.input.messages` | 当前 turn 历史 / 全量（可配置） |
| `gen_ai.output.messages` | assistant content |
| `claude_code.genai_hook.message.uuid` | `message.id` |
| `claude_code.genai_hook.request_ref` | `requestRef` |
| `claude_code.genai_hook.timing.source` | `"transcript"` |

#### `execute_tool`

| 属性 | 来源 |
| --- | --- |
| `gen_ai.operation.name` | `"execute_tool"` |
| `gen_ai.provider.name` | `"anthropic"` |
| `gen_ai.tool.name` | `tool_name` |
| `gen_ai.tool.call.id` | `tool_use_id` |
| `gen_ai.tool.call.arguments` | JSON(`tool_input`) |
| `gen_ai.tool.call.result` | JSON(`tool_response`) |
| `gen_ai.conversation.id` | `session_id` |
| `error.type` | 失败时的错误分类 |
| `claude_code.genai_hook.tool.duration_ms` | `duration_ms` |
| `claude_code.genai_hook.tool.is_interrupt` | `is_interrupt` |
| `claude_code.genai_hook.content.truncated` | 截断标记 |

#### Messages 格式

`gen_ai.input.messages` / `gen_ai.output.messages` 严格使用 GenAI semconv：

```json
[
  {"role": "user", "parts": [{"type": "text", "content": "..."}]},
  {"role": "assistant", "parts": [
    {"type": "tool_call", "id": "...", "name": "Read", "arguments": {...}}
  ], "finish_reason": "tool_use"},
  {"role": "tool", "parts": [
    {"type": "tool_call_response", "id": "...", "response": "..."}
  ]}
]
```

- Anthropic transcript 的 `tool_result` 位于 user 消息中，归一化后拆成 `role=tool`。
- `text` / `reasoning` 使用 `content` 字段。
- 图片使用 `blob` 或 `uri` part。
- 未知 block 使用 `anthropic.<type>` GenericPart。

### 7.6 内容格式与兼容性

```json
{
  "compat": {
    "messages_format": "json",
    "cache_write_attribute": "cache_creation",
    "emit_provider_name": true,
    "emit_system_legacy": true
  }
}
```

| 字段 | 取值 | 说明 |
| --- | --- | --- |
| `messages_format` | `json` / `text` | messages 用 JSON 字符串还是纯文本 |
| `cache_write_attribute` | `cache_creation` / `cache_write` | 兼容新旧 semconv |
| `emit_provider_name` | bool | 是否写 `gen_ai.provider.name` |
| `emit_system_legacy` | bool | 是否同时写 `gen_ai.system` |

### 7.7 截断与脱敏

- 按 `max_input_chars` / `max_message_chars` / `max_tool_chars` 截断。
- 截断后写 `claude_code.genai_hook.content.truncated=true` 和原始长度。
- `redact_patterns` 对 messages、tool arguments、tool result 统一替换。
- 默认 `include_thinking=false`。
- 图片只保留 `media_type`，不发送 base64。

---

## 8. Transcript 解析

### 8.1 使用的字段

| transcript 字段 | 用途 |
| --- | --- |
| `promptId`（user row） | turn key，与 hook `prompt_id` 对齐 |
| `uuid` | fallback turn key |
| `origin.kind` / `promptSource` | 判断真实用户 prompt |
| `message.id` | chat span_id、`gen_ai.response.id` |
| `message.model` | `gen_ai.response.model` |
| `requestedModel` | `gen_ai.request.model` |
| `message.stop_reason` | `gen_ai.response.finish_reasons` |
| `message.usage` | usage 字段 |
| `requestRef` | `claude_code.genai_hook.request_ref`（可选） |
| `apiBlockIndex` | content block 排序 |
| `isSidechain` | 子代理消息 |
| `version` / `gitBranch` / `cwd` | resource 属性 |
| `tool_use` block | tool name/input/id |
| `tool_result` block | tool result/error |

### 8.2 turn 识别

- `turn_key = promptId`。
- 只把 `origin.kind == "human"` 或 `promptSource` 存在的 user row 当作真实 prompt。
- 不要把 `<system-reminder>`、tool_result、meta 消息当作新 turn。

### 8.3 assistant 合并

- Claude Code 一条 assistant message 会写成多行（每个 content block 一行）。
- 按 `message.id` 合并。
- 用 `apiBlockIndex` 排序 block。
- 合并时更新 `last_ts`；如果首行 `stop_reason` / `usage` 为空，用后续行补上。

### 8.4 tool result

- user row 里的 `tool_result` block 按 `tool_use_id` 建索引。
- 工具 span 优先用 `PostToolUse.tool_response`；transcript 作为兜底。
- 失败工具用 `PostToolUseFailure.error` / `is_interrupt`。

### 8.5 transcript 滞后

- `transcript_path` 异步写入，Stop 时可能缺少最后一条消息。
- 策略：
  1. Stop 时解析，缺失消息不标记 processed；
  2. 下一次 Stop / SessionEnd 再补；
  3. `last_assistant_message` 可用于补 `output.messages` 文本，但拿不到 usage/model；
  4. 不要因为缺失就标记全部已处理。

### 8.6 子代理

- `SubagentStop` 的 `transcript_path` 是主会话；子代理用 `agent_transcript_path`。
- `SubagentStart` / `PostToolUse(Agent)` 记录 `agent_id -> Agent tool_use_id`。
- 子代理 root（已确认：同一 trace）：
  - 映射存在：parent = Agent 的 `execute_tool` span；
  - 映射不存在：parent = 当前父 turn 的 segment root，并尽可能加 link；
  - 只有完全找不到父上下文时，才退化为独立 trace 并记录 warning。
- 属性：`gen_ai.agent.id`、`gen_ai.agent.name`、`gen_ai.conversation.id`。

---

## 9. 状态与幂等

### 9.1 SQLite schema

```sql
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS sessions (
  session_id      TEXT PRIMARY KEY,
  transcript_path TEXT,
  version         TEXT,
  cwd             TEXT,
  created_at      TEXT,
  updated_at      TEXT
);

CREATE TABLE IF NOT EXISTS segments (
  session_id      TEXT,
  prompt_id       TEXT,
  seq             INTEGER,
  trace_id        BLOB,
  root_span_id    BLOB,
  started_ns      INTEGER,
  ended_ns        INTEGER,
  open            INTEGER DEFAULT 1,
  recovered       INTEGER DEFAULT 0,
  PRIMARY KEY (session_id, prompt_id, seq)
);

CREATE TABLE IF NOT EXISTS messages (
  session_id      TEXT,
  message_id      TEXT,
  prompt_id       TEXT,
  span_id         BLOB,
  emitted_at      TEXT,
  PRIMARY KEY (session_id, message_id)
);

CREATE TABLE IF NOT EXISTS tools (
  session_id      TEXT,
  tool_use_id     TEXT,
  prompt_id       TEXT,
  span_id         BLOB,
  emitted_at      TEXT,
  PRIMARY KEY (session_id, tool_use_id)
);

CREATE TABLE IF NOT EXISTS agents (
  session_id      TEXT,
  agent_id        TEXT,
  agent_type      TEXT,
  tool_use_id     TEXT,
  prompt_id       TEXT,
  transcript_path TEXT,
  PRIMARY KEY (session_id, agent_id)
);

CREATE TABLE IF NOT EXISTS outbox (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  payload         BLOB NOT NULL,
  created_at      TEXT,
  attempts        INTEGER DEFAULT 0,
  next_retry_at   TEXT,
  sent_at         TEXT
);
```

### 9.2 幂等键

- `messages(session_id, message_id)`：Stop 重复触发不重复发 chat。
- `tools(session_id, tool_use_id)`：PostToolUse 重放不重复发 tool。
- `segments(session_id, prompt_id, seq)`：不重复开 root。
- 确定性 span_id 作为第二层保护：即使状态丢失，后端也可按 span_id 去重。

### 9.3 并发

- Sidecar 单进程串行处理事件；SQLite 仅用于持久化。
- Command 基线模式使用 SQLite WAL + `BEGIN IMMEDIATE`。
- 每个请求获取短事务，避免长事务阻塞。

### 9.4 崩溃恢复

- 启动时扫描 `segments.open=1`，用 `last_event_ns` 或当前时间结束；
- 标记 `recovered=1`；
- 不重复发送已 `sent_at` 的 outbox 记录；
- 未发送记录继续重试。

---

## 10. 导出与可靠性

### 10.1 简单版：OTel SDK

```python
provider = TracerProvider(
    resource=Resource.create({...}),
    id_generator=DeterministicIdGenerator(),
)
provider.add_span_processor(
    BatchSpanProcessor(
        OTLPSpanExporter(
            endpoint=cfg["exporter"]["endpoint"],
            headers=cfg["exporter"]["headers"],
            timeout=cfg["exporter"]["timeout_ms"] / 1000,
            compression=cfg["exporter"]["compression"],
        ),
        max_export_batch_size=cfg["exporter"]["batch"]["max_size"],
        schedule_delay_millis=cfg["exporter"]["batch"]["schedule_delay_ms"],
    )
)
```

优点：代码少。  
缺点：队列在内存，进程崩溃会丢已结束但未导出的 span。

### 10.2 生产版：SQLite outbox

1. span 结束时序列化为 OTLP protobuf，写入 `outbox`；
2. 发送线程读 outbox，POST `/v1/traces`；
3. 2xx 标记 `sent_at`；
4. 失败按指数退避重试；
5. 确定性 ID 保证重发幂等。

### 10.3 失败处理

| 场景 | 处理 |
| --- | --- |
| endpoint 挂掉 | outbox 保留，指数退避；不标记 processed |
| endpoint 5xx | 重试；记录 attempts |
| endpoint 4xx | 记录错误；不无限重试；告警 |
| hookd 未启动 | `SessionStart` ensure；HTTP hook `onFailure: continue` |
| 队列满 | 丢弃低优先级内容，保留 root/tool；记录指标 |
| SessionEnd 预算不足 | handler 入队后立即返回；后台 flush |
| 依赖缺失 | `doctor` 检查；hook 失败不阻塞 Claude |

---

## 11. 子代理

### 11.1 生命周期

```text
SubagentStart
  → 记录 agent_id, agent_type
  → 可选：建立 parent turn 映射

PostToolUse(Agent)
  → tool_response.agentId
  → 记录 agent_id -> Agent tool_use_id

SubagentStop
  → 读取 agent_transcript_path
  → 生成子代理 invoke_agent / chat
```

### 11.2 Trace 策略（M3 已实现）

**已确认决策：子代理与父 turn 在同一个 trace，由父 Stop 统一构建。**

```text
invoke_agent (parent turn / segment)
└── execute_tool (Agent tool)
    └── subagent (container)
        ├── chat
        ├── execute_tool
        └── subagent response
```

实现规则：

1. 父会话 `Stop` 解析父 transcript；`SubagentStop` 被忽略，不单独发 trace。
2. 通过 tool result 的 `toolUseResult.agentId` 识别 Agent/Task delegation。
3. 查找 `subagents/agent-<agentId>.jsonl`，读取 `.meta.json` 的 `agentType` / `description`。
4. 在 delegation `execute_tool` span 下创建 `subagent` 容器：
   - parent = Agent tool span；
   - `gen_ai.operation.name=invoke_agent`；
   - `gen_ai.agent.id` / `gen_ai.agent.name`；
   - `claude_code.genai_hook.subagent=true`；
   - `claude_code.genai_hook.parent.tool_use_id`。
5. 子代理内部使用独立 step counter，从 `#1` 重新编号。
6. 父 tool span 的 end 扩展到 subagent end，保证父区间包含子区间。
7. 递归子代理，深度上限 5；所有 span 复用父 turn 的 trace_id。

**模式限制**：子代理子树只在 `hook.tool_mode=ordered` 下构建；`realtime` 模式
的 PostToolUse 路径只发 delegation tool span，不展开子代理 transcript。

补充属性：

- `gen_ai.conversation.id` 优先使用父 session id；
- 子代理自己的 session id 放在 `claude_code.genai_hook.agent.session_id`；
- `gen_ai.agent.id` / `gen_ai.agent.name` 来自 `agent_id` / `agent_type`。

### 11.3 注意

- 不要把主会话 `transcript_path` 当子代理 transcript。
- `agent_type` 为空时，不要用空字符串做 matcher 精确匹配。
- 子代理的 usage 不要重复计入父 turn 总量；父 turn 只统计主代理。

---

## 12. 隐私与安全

### 12.1 内容控制

```json
{
  "content": {
    "mode": "last_turn",
    "include_thinking": false,
    "redact_patterns": ["..."],
    "max_input_chars": 200000,
    "max_message_chars": 100000,
    "max_tool_chars": 100000
  }
}
```

- `mode=none`：只发结构，不发内容。
- `mode=last_turn`：只发当前 turn。
- `mode=full`：发完整历史（谨慎）。
- 默认不发 thinking。
- 工具结果按类型/大小截断。

### 12.2 密钥与配置

- `~/.claude/genai-hook.json` 权限 `0600`。
- header 值支持 `${ENV}`。
- 日志只打印 header 名，不打印值。
- 不把 API key 写进 `settings.json` 的 `env`。
- sidecar 绑定 `127.0.0.1`；可选 token header。

### 12.3 Claude Code 信任模型

- 交互式会话在 workspace trust 前不会运行 settings hooks。
- `-p` / SDK 会话默认信任目录；不可信仓库要 review `.claude/settings.json` 或加 `--settings '{"disableAllHooks": true}'`。
- managed settings 可限制 hooks；`allowedHttpHookUrls` 需管理员配合。
- Hook 以用户完整权限运行。

---

## 13. 兼容性

### 13.1 协议

| 协议 | 支持 | 说明 |
| --- | --- | --- |
| `http/protobuf` | ✅ 默认 | 通用性最好 |
| `http/json` | ⚠️ 可选 | 需要 JSON encoder；部分后端只支持 JSON |
| `grpc` | ❌ v1 不支持 | 用本地 OTel Collector 转换 |
| `console` | ✅ debug | 本地验证 |

### 13.2 后端

| 后端 | endpoint 形态 | header 示例 |
| --- | --- | --- |
| 通用 OTLP | base 或 full | `x-api-key` / `Authorization` |
| SigNoz | base 或 full | `signoz-access-token` |
| Jaeger | base | 无 |
| Tempo | base | 无 / `Authorization` |
| Datadog | full | `dd-api-key` |
| Honeycomb | base | `x-honeycomb-team` |
| New Relic | base | `api-key` |

具体 header 由后端决定，hook 不感知。

### 13.3 semconv 版本

- `gen_ai.provider.name`（新）与 `gen_ai.system`（旧）可同时输出。
- cache write 字段：`cache_creation` 或 `cache_write`，由 `compat.cache_write_attribute` 控制。
- messages 格式：JSON 字符串或纯文本，由 `compat.messages_format` 控制。

---

## 14. 测试与验收

### 14.1 单元测试

- 确定性 ID：同输入同输出、非 0、不碰撞。
- transcript 解析：多行 assistant 合并、tool_result、`promptId`、sidechain。
- 属性映射：每个 gen_ai 字段。
- 截断/脱敏。
- SQLite 幂等：重复事件只落一行。
- endpoint/header 解析：base/full、env 覆盖、`${ENV}` 展开。

### 14.2 集成测试

- 本地 OTLP receiver 捕获 span，断言 trace tree。
- 重放真实 session transcript，断言 chat/tool 数量。
- `SubagentStop` 使用 `agent_transcript_path`。
- 并发 PostToolUse + Stop。
- daemon 重启恢复 open segment。
- 两种后端配置（generic + SignSight）各跑一遍。

### 14.3 故障测试

- endpoint 500 / 超时：outbox 重试，最终成功。
- hookd 挂掉：Claude 不被阻塞；恢复后补发。
- transcript 缺最后一条：下次 Stop 补齐。
- SessionEnd 1.5s 预算：flush 后台完成。
- 坏 JSON / 缺字段：不崩溃，记录错误。

### 14.4 验收标准

1. 同一 session 重放两次，span 数量一致，无重复。
2. 所有 gen_ai 字段按映射表存在。
3. token 总量与原生 metrics 一致（若同时开启，需明确不双计）。
4. 工具 span 在工具结束后 < 1s 到达。
5. 子代理 span 可追溯到 Agent 工具调用。
6. hook p95 延迟 < 50ms；daemon 内存稳定。
7. endpoint 故障不丢数据；恢复后自动补发。
8. SignSight（或其他后端）能按 `gen_ai.conversation.id` 找到整个会话。
9. **command 和 sidecar 两种模式都通过上述验收**，且输出属性一致。
10. 原生 trace 保持关闭时，hook trace 仍完整可用；开启原生 trace 时能识别重复风险。

---

## 15. 上线与迁移

### 15.1 里程碑

| 阶段 | 内容 |
| --- | --- |
| M1 ✅ | **Command 基线模式**：确定性 ID + state + transcript parser + Stop 批量发 chat/root |
| M2 ✅ | **Command 丰富内容模式**：PostToolUse/Failure command hooks、execute_tool spans、工具入参/返回/错误/耗时；不引入 sidecar |
| M3 ✅ | **子代理 trace（无 sidecar）**：父 Stop 解析 subagent transcript、delegation tool span 下创建 subagent 容器、本地 step 顺序、同一 trace、递归深度限制 |
| M4 ⏳ | **Sidecar 实时模式**：hookd + HTTP hooks、open segment span、实时 PostToolUse |
| M5 ⏳ | outbox 重试、脱敏、采样、doctor、测试 |
| M6 ⏳ | 灰度上线、后端看板、文档 |

交付顺序：M1 先交付可独立运行的 command 模式；M2 在 command 模式下补齐 `execute_tool` 和工具内容；M3 由父 Stop 统一构建子代理子树，仍然不引入 sidecar；M4 再引入 sidecar 提供跨事件 open span 和实时工具 span。两种模式共用 core、配置和属性映射。

### 15.2 灰度

1. **Shadow**：`dry_run=true`，只写本地文件/本地 collector。
2. **试点**：1-2 个用户，`content.mode=last_turn`。
3. **灰度**：按用户/仓库打开，加 `sample_rate`。
4. **默认**：加 kill switch `CC_GENAI_HOOK_ENABLED=0`。
5. **监控**：events received、spans emitted、export failures、queue depth、duplicate drops。

### 15.3 M1/M2/M3 实现状态（2026-11-08）

代码位于 `cc-genai-hook/`。

M1 已完成：

- ✅ `command` 模式：`Stop` + `SessionEnd` hooks
- ✅ transcript 解析、`promptId` turn 分组、assistant 行合并
- ✅ `invoke_agent` segment root + `chat` spans
- ✅ 确定性 trace/span ID
- ✅ SQLite 幂等状态
- ✅ 通用 OTLP endpoint/headers 配置
- ✅ `install` / `uninstall` / `doctor` / `config` / `seed` / `hook` CLI
- ✅ dry-run JSONL 和本地 OTLP receiver 验证

M2 已完成：

- ✅ **ordered 模式（默认）**：Stop 时按 transcript 顺序统一生成 chat + execute_tool
- ✅ turn 内共享 step counter，`+1ms` 合成时间戳，`step_index` / `step_name` / `plan_step` / `step_count`
- ✅ chat/tool 都是 root 的 sibling，父区间包含子区间
- ✅ **realtime 模式（可选）**：`PostToolUse` / `PostToolUseFailure` command hooks
- ✅ `execute_tool` spans：工具名、call ID、入参、返回、错误、耗时
- ✅ 确定性 tool span ID + `tool_use_id` 幂等
- ✅ `error.type` / `is_interrupt` / `tool.success`
- ✅ tool span 挂到当前 turn 的 root
- ✅ 34 个单元/集成测试通过

M3 已完成（无 sidecar）：

- ✅ 父 Stop 统一解析 subagent transcript，`SubagentStop` 被忽略
- ✅ 通过 `toolUseResult.agentId` 识别 Agent/Task delegation
- ✅ 查找 `subagents/agent-<agentId>.jsonl`，读取 `.meta.json`
- ✅ delegation tool span 下创建 `subagent` 容器
- ✅ 子代理内部使用独立 step counter，从 `#1` 重新编号
- ✅ 父 tool span 的 end 扩展到 subagent end，保证区间包含
- ✅ 递归子代理，深度上限 5
- ✅ 所有 span 复用父 turn 的 trace_id

M4/M5 暂不包含：

- ⏳ sidecar/HTTP hooks、实时 open span、实时 PostToolUse（M4）
- ⏳ durable outbox、采样、完整脱敏、TTL 清理（M5）

### 15.4 从现有脚本迁移

| 旧实现 | 通用化后 |
| --- | --- |
| `legacy_genai.py` | `cc_genai_hook.py` / `cc_genai_hookd` |
| `legacy_install.sh` | `cc_genai_install.sh` |
| `LEGACY_*` | `CC_GENAI_*` |
| `legacy.*` 属性 | `claude_code.genai_hook.*` |
| `x-api-key` | `exporter.headers` |
| 写 `settings.json` 的 `env` | 写 `~/.claude/genai-hook.json` |
| 硬编码 endpoint | CLI/配置参数 |

迁移步骤：

1. 抽出 `config.py`，支持配置文件 + `OTEL_*`。
2. 用 header map + `${ENV}` 替换 `parse_kv`。
3. 用 `resolve_endpoint` 替换固定拼接。
4. 统一自定义属性命名。
5. 安装脚本改为 `--endpoint` + 可重复 `--header`。
6. 默认不写 `env`；需要原生 telemetry 时显式加参数。

---

## 16. 风险与开放问题

### 16.1 风险

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 不是真实 trace | 无法做精确延迟/权限/重试分析 | 明确标注 synthetic；需要时用原生 OTel |
| transcript schema 变化 | 解析失败/字段缺失 | 版本探测 + fixture 测试 + 降级 |
| hook 重复触发 | 重复 span/成本翻倍 | 唯一键 + 确定性 ID |
| transcript 滞后 | 漏最后一条 | 不标记 processed；下次补 |
| daemon 不可用 | 事件丢失 | ensure + onFailure continue + outbox |
| 内容隐私 | prompt/工具结果外发 | 脱敏/截断/content mode |
| 双重计数 | 同时开原生 metrics/traces | 只保留一套 trace；指标去重/标注 |
| HTTP hook 策略 | managed 环境被禁用 | `allowedHttpHookUrls`；切回 command 基线模式 |
| SessionEnd 预算 | flush 不完整 | 入队后异步发送 |
| 后端兼容性 | 字段/格式不认 | `compat` 配置；Collector 转换 |

### 16.2 决策记录

#### 已确认（2026-11-04）

| # | 决策 | 结论 |
| --- | --- | --- |
| 1 | 部署模式 | **同时实现 command 和 sidecar 两种模式**；共用 core/config/ID/state 接口，安装器 `--mode command\|sidecar\|auto` |
| 2 | Trace 边界 | **per-turn trace**；`prompt_id` 作为 turn key，`trace_id = hash(session_id + prompt_id)` |
| 3 | `execute_tool` 父节点 | **segment root**；不挂 `chat`，不为了父节点扫描滞后 transcript |
| 5 | 子代理 trace | **与父 turn 同一个 trace**；优先挂 Agent `execute_tool`，缺失时挂当前 segment root，独立 trace 仅作异常回退 |
| 6 | 原生 trace | **不开启**；不设置 `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA` / `OTEL_TRACES_EXPORTER=otlp`，hook trace 是唯一 trace 来源 |

#### 已确认（第二批）

| # | 决策 | 结论 |
| --- | --- | --- |
| 4 | 内容默认值 | **`last_turn`**；`include_thinking=false`，默认开启正则脱敏 |
| 7 | 首批后端 | **通用 OTLP + 任意 header**；SignSight 仅作为示例配置 |
| 8 | 采样/保留 | **`sample_rate=1.0`**；state/outbox TTL **14 天** |
| 9 | 命名空间 | **`claude_code.genai_hook.*`** |
| 10 | 安装方式 | **`ensure` 默认**；launchd/systemd 作为可选 `--service` |

### 16.3 原生 telemetry 策略（已确认）

- v1 **不开启** Claude Code 原生 trace，不依赖 `OTEL_TRACES_EXPORTER` / `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA`。
- Hook 配置独立于 `~/.claude/settings.json` 的 `env`，安装器默认不写任何原生 telemetry 变量。
- 如果用户或 managed settings 已经开启了原生 trace，需要显式提示重复风险，并建议二选一或给 synthetic span 打标后再去重。
- 原生 metrics/logs 不在本方案范围内；需要时由用户单独配置，不与 hook 的 endpoint/header 混用。

---

## 17. 附录

### 17.1 示例 CLI

```bash
bash cc_genai_install.sh \
  --endpoint https://otel.example.com \
  --header x-api-key="$API_KEY" \
  --header "Authorization=Bearer $TOKEN" \
  --user "$(whoami)"
```

### 17.2 示例 doctor

```bash
python3 cc_genai_hook.py doctor
```

输出：

```text
config:     ~/.claude/genai-hook.json (0600)
endpoint:   https://otel.example.com/v1/traces
protocol:   http/protobuf
headers:    x-api-key, Authorization   # 只显示名字
deps:       opentelemetry-sdk OK
hookd:      http://127.0.0.1:4319/health OK
state:      ~/.claude/state/genai-hook
```

### 17.3 参考

- [Claude Code Hooks reference](https://code.claude.com/docs/en/hooks)
- [Claude Code Monitoring / OpenTelemetry](https://code.claude.com/docs/en/monitoring-usage)
- [OpenTelemetry GenAI semantic conventions](https://github.com/open-telemetry/semantic-conventions-genai)
- [OTLP specification](https://opentelemetry.io/docs/specs/otlp/)

---

## 18. Review checklist

- [x] 目标/非目标是否准确
- [x] 两种部署模式：command + sidecar 都实现
- [x] per-turn trace / segment 策略
- [x] `execute_tool` 父节点：segment root
- [x] 内容默认值：`last_turn`，thinking 关闭，默认脱敏
- [x] 子代理 trace：与父 turn 同一个 trace
- [x] endpoint/header 配置模型：独立配置文件 + 任意 header
- [x] 原生 telemetry：不开启
- [ ] SQLite/outbox 可靠性方案
- [ ] 迁移计划和里程碑
- [x] 命名空间 `claude_code.genai_hook.*`
- [x] 首批支持的后端：通用 OTLP + 任意 header
