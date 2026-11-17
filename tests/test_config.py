import json
import os

import pytest

from cc_genai_hook.config import (
    ConfigError,
    expand_env,
    load_config,
    parse_otel_headers,
    resolve_endpoint,
    resolve_headers,
)


def test_parse_otel_headers():
    assert parse_otel_headers("a=1,b=2") == {"a": "1", "b": "2"}
    assert parse_otel_headers("") == {}
    with pytest.raises(ConfigError):
        parse_otel_headers("broken")


def test_expand_env(monkeypatch):
    monkeypatch.setenv("TOKEN", "abc")
    assert expand_env("Bearer ${TOKEN}") == "Bearer abc"
    with pytest.raises(ConfigError):
        expand_env("${MISSING}")


def test_endpoint_base_appends_v1_traces():
    cfg = {"exporter": {"endpoint": "https://otel.example.com", "endpoint_kind": "base"}}
    assert resolve_endpoint(cfg, env={}) == "https://otel.example.com/v1/traces"


def test_endpoint_full_is_used_as_is():
    cfg = {"exporter": {"endpoint": "https://otel.example.com/v1/traces", "endpoint_kind": "full"}}
    assert resolve_endpoint(cfg, env={}) == "https://otel.example.com/v1/traces"


def test_signal_specific_endpoint_wins_and_is_full():
    cfg = {"exporter": {"endpoint": "https://ignored.example.com", "endpoint_kind": "base"}}
    env = {"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "https://backend.example.com/v1/traces"}
    assert resolve_endpoint(cfg, env=env) == "https://backend.example.com/v1/traces"


def test_header_precedence(monkeypatch):
    monkeypatch.setenv("GENERIC", "generic")
    monkeypatch.setenv("SIGNAL", "signal")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "a=${GENERIC}")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "a=${SIGNAL},b=2")
    cfg = {"exporter": {"headers": {"a": "config", "c": "3"}}}
    assert resolve_headers(cfg) == {"a": "signal", "b": "2", "c": "3"}


def test_load_config_merges_file_and_defaults(tmp_path, monkeypatch):
    config = {
        "exporter": {"endpoint": "https://otel.example.com"},
        "content": {"mode": "none"},
        "state": {"dir": str(tmp_path / "state")},
    }
    path = tmp_path / "genai-hook.json"
    path.write_text(json.dumps(config))
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    cfg = load_config(str(path))
    assert cfg["exporter"]["resolved_endpoint"] == "https://otel.example.com/v1/traces"
    assert cfg["content"]["mode"] == "none"
    assert cfg["state"]["dir"].endswith("state")


@pytest.mark.parametrize("value", [-1, "14", True, None])
def test_invalid_ttl_days_rejected(tmp_path, value):
    path = tmp_path / "genai-hook.json"
    path.write_text(json.dumps({"state": {"dir": str(tmp_path), "ttl_days": value}}))
    with pytest.raises(ConfigError):
        load_config(str(path))


def test_ttl_days_default_and_zero(tmp_path):
    path = tmp_path / "genai-hook.json"
    path.write_text(json.dumps({"state": {"dir": str(tmp_path)}}))
    assert load_config(str(path))["state"]["ttl_days"] == 14
    path.write_text(json.dumps({"state": {"dir": str(tmp_path), "ttl_days": 0}}))
    assert load_config(str(path))["state"]["ttl_days"] == 0
