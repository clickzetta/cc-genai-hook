"""Configuration loading and endpoint/header resolution.

The hook is backend-agnostic: it only understands an OTLP endpoint and a set
of arbitrary headers.  Backend-specific values (for example ``x-api-key`` or
``Authorization``) belong in the config file or environment, never in the code.
"""

from __future__ import annotations

import copy
import json
import os
import re
from typing import Any, Dict, Mapping, Optional

DEFAULT_CONFIG_PATH = "~/.claude/genai-hook.json"

DEFAULTS: Dict[str, Any] = {
    "version": 1,
    "enabled": True,
    "exporter": {
        "protocol": "http/protobuf",
        "endpoint": None,
        "endpoint_kind": "base",
        "headers": {},
        "compression": "gzip",
        "timeout_ms": 30000,
        "batch": {
            "max_size": 512,
            "schedule_delay_ms": 500,
        },
    },
    "resource": {
        "service.name": "claude-code",
        "attributes": {},
    },
    "content": {
        "mode": "last_turn",
        "include_thinking": False,
        "max_input_chars": 200000,
        "max_message_chars": 100000,
        "max_tool_chars": 100000,
        "redact_patterns": [],
    },
    "compat": {
        "messages_format": "json",
        "cache_write_attribute": "cache_creation",
        "emit_provider_name": True,
        "emit_system_legacy": True,
    },
    "emit": {
        "invoke_agent": True,
        "chat": True,
        "execute_tool": True,
        "subagents": True,
    },
    "hook": {
        "sample_rate": 1.0,
        "dry_run": False,
        "debug": False,
        "agent_name": "claude-code",
        "tool_mode": "ordered",
    },
    "state": {
        "dir": "~/.claude/state/genai-hook",
        "ttl_days": 14,
    },
}

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_BOOL_TRUE = {"1", "true", "yes", "on"}


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _BOOL_TRUE


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    return default if value is None else _as_bool(value)


def expand_env(value: str) -> str:
    """Expand ``${VAR}`` references, failing fast on missing variables."""

    def repl(match: re.Match) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise ConfigError(
                "environment variable %s is referenced by the config but not set" % name
            )
        return os.environ[name]

    return _ENV_REF.sub(repl, value)


def parse_otel_headers(text: Optional[str]) -> Dict[str, str]:
    """Parse the OTel ``key1=value1,key2=value2`` header form."""

    headers: Dict[str, str] = {}
    if not text:
        return headers
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ConfigError("invalid OTLP header %r (expected key=value)" % part)
        key, value = part.split("=", 1)
        key = key.strip()
        if not key:
            raise ConfigError("invalid OTLP header %r (empty key)" % part)
        headers[key] = value.strip()
    return headers


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(dict(base))
    for key, value in (override or {}).items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, Mapping)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _read_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ConfigError("cannot read config %s: %s" % (path, exc))
    except ValueError as exc:
        raise ConfigError("invalid JSON in config %s: %s" % (path, exc))
    if not isinstance(data, dict):
        raise ConfigError("config %s must contain a JSON object" % path)
    return data


def default_config_path() -> str:
    return os.path.expanduser(
        os.environ.get("CC_GENAI_HOOK_CONFIG") or DEFAULT_CONFIG_PATH
    )


def resolve_endpoint(cfg: Mapping[str, Any], env: Mapping[str, str] = os.environ) -> str:
    """Resolve the trace endpoint following the OTel precedence rules."""

    exporter = cfg.get("exporter") or {}
    traces_env = env.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    generic_env = env.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    configured = exporter.get("traces_endpoint") or exporter.get("endpoint")

    endpoint = traces_env or configured or generic_env
    if not endpoint:
        raise ConfigError(
            "no OTLP endpoint configured: set exporter.endpoint in the config "
            "or OTEL_EXPORTER_OTLP_(TRACES_)ENDPOINT"
        )

    endpoint = str(endpoint).strip().rstrip("/")
    if not endpoint.startswith(("http://", "https://")):
        raise ConfigError("OTLP endpoint must start with http:// or https://")

    full = bool(traces_env) or exporter.get("endpoint_kind") == "full"
    if endpoint.endswith("/v1/traces"):
        full = True
    return endpoint if full else endpoint + "/v1/traces"


def resolve_headers(
    cfg: Mapping[str, Any], env: Mapping[str, str] = os.environ
) -> Dict[str, str]:
    """Resolve headers: signal env > generic env > config."""

    exporter = cfg.get("exporter") or {}
    headers: Dict[str, str] = {}
    for key, value in (exporter.get("headers") or {}).items():
        headers[str(key)] = expand_env(str(value))
    headers.update(parse_otel_headers(env.get("OTEL_EXPORTER_OTLP_HEADERS")))
    headers.update(parse_otel_headers(env.get("OTEL_EXPORTER_OTLP_TRACES_HEADERS")))
    return {str(key): expand_env(str(value)) for key, value in headers.items()}


def resolve_resource_attributes(
    cfg: Mapping[str, Any], env: Mapping[str, str] = os.environ
) -> Dict[str, str]:
    resource = cfg.get("resource") or {}
    attrs: Dict[str, str] = {}
    for key, value in (resource.get("attributes") or {}).items():
        attrs[str(key)] = expand_env(str(value))
    attrs.update(parse_otel_headers(env.get("OTEL_RESOURCE_ATTRIBUTES")))
    service_name = resource.get("service.name") or DEFAULTS["resource"]["service.name"]
    attrs["service.name"] = str(service_name)
    return attrs


def load_config(path: Optional[str] = None, require_endpoint: bool = False) -> Dict[str, Any]:
    """Load config, merge defaults, apply environment overrides."""

    config_path = os.path.expanduser(path) if path else default_config_path()
    cfg = _deep_merge(DEFAULTS, _read_json(config_path))

    # Environment overrides.
    cfg["enabled"] = _env_bool("CC_GENAI_HOOK_ENABLED", bool(cfg.get("enabled", True)))
    cfg.setdefault("hook", {})
    cfg["hook"]["dry_run"] = _env_bool(
        "CC_GENAI_HOOK_DRY_RUN", bool(cfg["hook"].get("dry_run", False))
    )
    cfg["hook"]["debug"] = _env_bool(
        "CC_GENAI_HOOK_DEBUG", bool(cfg["hook"].get("debug", False))
    )
    content_mode = os.environ.get("CC_GENAI_HOOK_CONTENT_MODE")
    if content_mode:
        cfg.setdefault("content", {})["mode"] = content_mode.strip().lower()
    tool_mode = os.environ.get("CC_GENAI_HOOK_TOOL_MODE")
    if tool_mode:
        cfg.setdefault("hook", {})["tool_mode"] = tool_mode.strip().lower()

    cfg.setdefault("hook", {})
    tool_mode = str(cfg["hook"].get("tool_mode") or "ordered").lower()
    if tool_mode not in ("ordered", "realtime"):
        raise ConfigError("hook.tool_mode must be ordered or realtime")
    cfg["hook"]["tool_mode"] = tool_mode

    protocol = os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL")
    if protocol:
        cfg.setdefault("exporter", {})["protocol"] = protocol.strip()
    compression = os.environ.get("OTEL_EXPORTER_OTLP_COMPRESSION")
    if compression:
        cfg.setdefault("exporter", {})["compression"] = compression.strip().lower()

    exporter = cfg.setdefault("exporter", {})
    protocol = str(exporter.get("protocol") or "http/protobuf").lower()
    if protocol not in ("http/protobuf",):
        raise ConfigError(
            "unsupported OTLP protocol %r; M1 supports http/protobuf only" % protocol
        )
    exporter["protocol"] = protocol

    compression = str(exporter.get("compression") or "none").lower()
    if compression not in ("none", "gzip", "deflate"):
        raise ConfigError("unsupported compression %r" % compression)
    exporter["compression"] = compression

    mode = str((cfg.get("content") or {}).get("mode") or "last_turn").lower()
    if mode not in ("last_turn", "full", "none"):
        raise ConfigError("content.mode must be last_turn, full or none")
    cfg["content"]["mode"] = mode

    cfg["exporter"]["resolved_endpoint"] = (
        resolve_endpoint(cfg) if require_endpoint or exporter.get("endpoint") else None
    )
    try:
        cfg["exporter"]["resolved_headers"] = resolve_headers(cfg)
        cfg["resource"]["resolved_attributes"] = resolve_resource_attributes(cfg)
    except ConfigError:
        if require_endpoint:
            raise
        cfg["exporter"]["resolved_headers"] = {}
        cfg["resource"]["resolved_attributes"] = {}

    if require_endpoint and not cfg["exporter"].get("resolved_endpoint"):
        # Resolve again so the missing-endpoint error is raised consistently.
        cfg["exporter"]["resolved_endpoint"] = resolve_endpoint(cfg)

    cfg["_config_path"] = config_path
    cfg["state"]["dir"] = os.path.expanduser(
        str(cfg.get("state", {}).get("dir") or DEFAULTS["state"]["dir"])
    )
    ttl_days = cfg["state"].get("ttl_days", DEFAULTS["state"]["ttl_days"])
    if isinstance(ttl_days, bool) or not isinstance(ttl_days, (int, float)) or ttl_days < 0:
        raise ConfigError("state.ttl_days must be a number >= 0 (0 disables cleanup)")
    cfg["state"]["ttl_days"] = ttl_days
    return cfg


def redact_headers(headers: Mapping[str, str]) -> Dict[str, str]:
    return {key: "<redacted>" for key in headers}


def write_config(path: str, cfg: Mapping[str, Any]) -> None:
    """Write config atomically with 0600 permissions."""

    target = os.path.expanduser(path)
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)


def headers_from_pairs(pairs: Any) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    for pair in pairs or []:
        if isinstance(pair, (tuple, list)) and len(pair) == 2:
            key, value = pair
        elif isinstance(pair, str):
            if "=" not in pair:
                raise ConfigError("--header expects key=value, got %r" % pair)
            key, value = pair.split("=", 1)
        else:
            raise ConfigError("invalid header %r" % (pair,))
        key = str(key).strip()
        if not key:
            raise ConfigError("header key must not be empty")
        headers[key] = str(value)
    return headers
