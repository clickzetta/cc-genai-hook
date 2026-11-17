# cc-genai-hook

[English](README.en.md)

通用的 Claude Code GenAI hook。它通过 hooks 读取 Claude Code transcript，生成
OpenTelemetry GenAI span，并导出到任意 OTLP HTTP/protobuf 后端。

- 不绑定具体后端：只需配置 endpoint 和任意 header。
- 不需要 Claude Code 原生 telemetry：hook 使用独立的配置文件。
- 确定性 trace/span ID：重复执行 hook 得到相同的 ID。
- 内容会脱敏、截断，也可以关闭。

## 工作原理

- **ordered 模式（默认）：** `Stop` hook 按 transcript 顺序，在 `invoke_agent`
  turn root 下生成 `chat` 和 `execute_tool` span。每个 turn 共享一个 step
  counter（`#1`、`#2`……），并使用 +1 ms 的合成时间戳。
- **realtime 模式（可选）：** `PostToolUse` / `PostToolUseFailure` command
  hooks 在工具结束时发送 tool span。
- **子代理：** 父会话的 `Stop` 解析 subagent transcript，在 delegation tool span
  下嵌套 `subagent` 容器，与父 turn 在同一个 trace 中，深度上限 5。
  `SubagentStop` 会被忽略。子代理子树只在 ordered 模式下构建。
- 幂等：SQLite 状态以 `(session_id, message_id)` 和 `(session_id, tool_use_id)`
  为键，另有确定性 span ID 兜底。

## 安装

要求 Python 3.9+（已在 3.9.6 验证）。

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

使用完整的 `/v1/traces` URL 时加 `--endpoint-kind full`：

```bash
cc-genai-hook install \
  --endpoint https://backend.example.com/v1/traces \
  --endpoint-kind full
```

安装会写入：

- `~/.claude/genai-hook.json`（0600）
- `~/.claude/settings.json` 中的 `Stop` 和 `SessionEnd` 条目（会先备份）

安装**不会**写入 `CLAUDE_CODE_ENABLE_TELEMETRY`、`OTEL_TRACES_EXPORTER`、
`OTEL_LOG_*` 或任何其他原生 telemetry 变量。

## 验证

```bash
cc-genai-hook doctor
cc-genai-hook doctor --send-test
cc-genai-hook config show
```

不发送数据的 dry-run：

```bash
CC_GENAI_HOOK_DRY_RUN=1 claude -p "hello"
cat ~/.claude/state/genai-hook/dry-run.jsonl
```

用本地 receiver 做端到端验证：

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

## 配置

默认路径：`~/.claude/genai-hook.json`。示例见 `examples/genai-hook.example.json`。

主要字段：

| 字段 | 含义 |
| --- | --- |
| `exporter.endpoint` | OTLP base URL（`endpoint_kind=full` 时为完整 URL） |
| `exporter.headers` | 任意 header，例如 `x-api-key`、`Authorization` |
| `exporter.compression` | `gzip`、`deflate`、`none` |
| `resource.attributes` | 额外的 resource 属性，例如 `enduser.id` |
| `content.mode` | `last_turn`、`full`、`none` |
| `content.include_thinking` | 是否包含 reasoning 内容（默认 `false`） |
| `content.redact_patterns` | 正则替换规则 |
| `hook.dry_run` | 把 span 写入 `dry-run.jsonl`，不发送 |
| `state.dir` | SQLite 状态目录 |
| `state.ttl_days` | 删除闲置超过该天数的会话及其 segment/message/tool 记录（默认 `14`，`0` 关闭）。在 Stop/SessionEnd 之后执行，每 24 小时最多一次。被清理的会话之后如果被 resume，会重新发送一次历史消息；span ID 不变，后端可以去重。 |

环境变量覆盖：

- `CC_GENAI_HOOK_CONFIG`
- `CC_GENAI_HOOK_ENABLED`
- `CC_GENAI_HOOK_DRY_RUN`
- `CC_GENAI_HOOK_DEBUG`
- `CC_GENAI_HOOK_CONTENT_MODE`
- `OTEL_EXPORTER_OTLP_ENDPOINT` / `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`
- `OTEL_EXPORTER_OTLP_HEADERS` / `OTEL_EXPORTER_OTLP_TRACES_HEADERS`

## 命令

```text
cc-genai-hook install [--mode command] [--endpoint URL] [--header K=V]...
cc-genai-hook uninstall [--purge]
cc-genai-hook doctor [--send-test]
cc-genai-hook config show
cc-genai-hook config validate
cc-genai-hook seed --session-id ID --transcript PATH
cc-genai-hook hook stop|session_end|subagent_stop|post_tool_use|post_tool_use_failure|seed
```

`seed` 把已有 transcript 标记为已发送，避免一次性补发大量历史消息。

## 开发

```bash
PYTHONPATH=src python3 -m pytest -q
```

设计、里程碑与实现状态：[`docs/design.md`](docs/design.md)。

## 安全

- Command hooks 以当前用户权限运行。
- `~/.claude/genai-hook.json` 包含凭证，请保持 `0600` 权限。
- 内容属性可能包含 prompt、文件内容和工具输出。如果不能接受，请使用
  `content.mode=none` 或脱敏规则。
- Hook 从不写 stdout，失败时也以 0 退出，因此不会阻塞 Claude Code。失败信息见
  `~/.claude/state/genai-hook/hook.err.log`。

## License

[MIT](LICENSE)
