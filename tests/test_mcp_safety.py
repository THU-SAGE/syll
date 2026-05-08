"""Phase 1b: MCP safety surface — command_hash / preview / boot_validate.

These tests don't need a real MCP server; they exercise the consent-token,
schema-validators, and boot_validate behavior with synthetic configs.
"""

from __future__ import annotations

import pytest

from syll.agent.mcp import (
    MCPHashMismatchError,
    MCPManager,
    _normalize_schema_for_openai,
    command_hash,
    command_preview,
    stdio_params_changed,
)
from syll.config.schema import (
    MCPConfig,
    MCPHttpParams,
    MCPServerConfig,
    MCPStdioParams,
)

# ── command_hash: stable across irrelevant fields ────────────────────────


def _stdio(**kw) -> MCPServerConfig:
    return MCPServerConfig(
        transport="stdio",
        stdio=MCPStdioParams(
            command=kw.get("command", "echo"),
            args=kw.get("args", []),
            env=kw.get("env", {}),
            cwd=kw.get("cwd"),
        ),
        enabled=kw.get("enabled", False),
        confirmed_command_hash=kw.get("confirmed_command_hash"),
        enabled_tools=kw.get("enabled_tools", ["*"]),
        propagate_to_subagents=kw.get("propagate_to_subagents", True),
        tool_timeout_seconds=kw.get("tool_timeout_seconds", 60),
    )


def test_command_hash_stable_when_irrelevant_fields_change():
    a = _stdio(command="npx", args=["-y", "@x/y@1.0"], enabled=False)
    b = _stdio(command="npx", args=["-y", "@x/y@1.0"], enabled=True,
               propagate_to_subagents=False, tool_timeout_seconds=30,
               enabled_tools=["foo"])
    assert command_hash(a) == command_hash(b)


def test_command_hash_changes_when_command_changes():
    a = _stdio(command="npx", args=["-y", "@x/y"])
    b = _stdio(command="node", args=["-y", "@x/y"])
    assert command_hash(a) != command_hash(b)


def test_command_hash_changes_when_args_change():
    a = _stdio(command="npx", args=["-y", "@x/y@1.0"])
    b = _stdio(command="npx", args=["-y", "@x/y@2.0"])
    assert command_hash(a) != command_hash(b)


def test_command_hash_changes_when_env_changes():
    a = _stdio(command="node", env={"OPENAI_API_KEY": "k1"})
    b = _stdio(command="node", env={"OPENAI_API_KEY": "k2"})
    assert command_hash(a) != command_hash(b)


def test_command_hash_changes_when_cwd_changes():
    a = _stdio(command="node", cwd="/Users/bo/work")
    b = _stdio(command="node", cwd="/tmp")
    assert command_hash(a) != command_hash(b)


def test_command_hash_differs_between_transports():
    s = _stdio(command="node")
    h = MCPServerConfig(
        transport="streamableHttp",
        http=MCPHttpParams(url="http://x.example/mcp"),
    )
    assert command_hash(s) != command_hash(h)


def test_command_hash_format():
    h = command_hash(_stdio(command="echo"))
    assert h.startswith("sha256:")
    assert len(h) == len("sha256:") + 24


# ── command_preview: secrets must not leak ───────────────────────────────


def test_command_preview_lists_env_keys_not_values():
    cfg = _stdio(
        command="node",
        args=["./bridge.js"],
        env={"OPENAI_API_KEY": "sk-secret-deadbeef", "MY_TOKEN": "private"},
    )
    p = command_preview(cfg)
    # Keys are visible (so the user knows what they're consenting to).
    assert "OPENAI_API_KEY" in p
    assert "MY_TOKEN" in p
    # But VALUES never appear.
    assert "sk-secret-deadbeef" not in p
    assert "private" not in p


def test_command_preview_lists_header_keys_not_values():
    cfg = MCPServerConfig(
        transport="streamableHttp",
        http=MCPHttpParams(
            url="https://api.example/mcp",
            headers={"Authorization": "Bearer secret123"},
        ),
    )
    p = command_preview(cfg)
    assert "Authorization" in p
    assert "secret123" not in p


# ── stdio_params_changed ────────────────────────────────────────────────


def test_stdio_params_changed_detects_real_diff():
    a = _stdio(command="npx", args=["-y", "@x/y@1.0"])
    b = _stdio(command="npx", args=["-y", "@x/y@1.1"])
    assert stdio_params_changed(b, a) is True


def test_stdio_params_changed_ignores_propagate_flag():
    a = _stdio(command="npx", args=["-y", "@x/y"], propagate_to_subagents=True)
    b = _stdio(command="npx", args=["-y", "@x/y"], propagate_to_subagents=False)
    assert stdio_params_changed(b, a) is False


def test_stdio_params_changed_when_old_is_none():
    """Fresh inserts always count as changed (forces consent flow)."""
    new = _stdio(command="npx", args=["-y", "@x/y"])
    assert stdio_params_changed(new, None) is True


# ── boot_validate ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_boot_validate_passes_for_disabled_server():
    """Disabled servers don't need a hash — nothing to launch."""
    cfg = MCPConfig(
        servers={
            "fs": _stdio(command="echo", enabled=False),
        }
    )
    mgr = MCPManager(cfg)
    await mgr.boot_validate()  # must not raise


@pytest.mark.asyncio
async def test_boot_validate_passes_for_correct_hash():
    server = _stdio(command="echo", args=["hi"], enabled=True)
    server.confirmed_command_hash = command_hash(server)
    cfg = MCPConfig(servers={"fs": server})
    mgr = MCPManager(cfg)
    await mgr.boot_validate()  # must not raise


@pytest.mark.asyncio
async def test_boot_validate_rejects_tampered_hash():
    """Direct edit of ~/.syll/config.json that flips enabled=true without
    recomputing the hash must fail boot."""
    server = _stdio(command="echo", args=["hi"], enabled=True)
    server.confirmed_command_hash = "sha256:000000000000000000000000"
    cfg = MCPConfig(servers={"fs": server})
    mgr = MCPManager(cfg)
    with pytest.raises(MCPHashMismatchError) as exc:
        await mgr.boot_validate()
    assert "fs" in str(exc.value)
    assert "confirmed_command_hash" in str(exc.value)


@pytest.mark.asyncio
async def test_boot_validate_rejects_missing_hash_for_enabled_stdio():
    server = _stdio(command="echo", enabled=True)
    server.confirmed_command_hash = None
    cfg = MCPConfig(servers={"fs": server})
    mgr = MCPManager(cfg)
    with pytest.raises(MCPHashMismatchError):
        await mgr.boot_validate()


@pytest.mark.asyncio
async def test_boot_validate_skips_http_servers():
    """Hash consent is a stdio-RCE protection. HTTP servers don't run on the
    user's host, so we don't gate them behind hash confirmation at boot."""
    cfg = MCPConfig(
        servers={
            "remote": MCPServerConfig(
                transport="streamableHttp",
                http=MCPHttpParams(url="https://api.example/mcp"),
                enabled=True,
            ),
        }
    )
    mgr = MCPManager(cfg)
    await mgr.boot_validate()  # must not raise


# ── Schema normalization ────────────────────────────────────────────────


def test_normalize_flattens_nullable_union():
    schema = {"type": ["string", "null"]}
    out = _normalize_schema_for_openai(schema)
    assert out["type"] == "string"
    assert out["nullable"] is True


def test_normalize_recurses_into_nested_schemas():
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": ["string", "null"]},
            "items": {
                "type": "array",
                "items": {"type": ["integer", "null"]},
            },
            "either": {
                "anyOf": [
                    {"type": ["string", "null"]},
                    {"type": "boolean"},
                ]
            },
        },
    }
    out = _normalize_schema_for_openai(schema)
    assert out["properties"]["name"]["type"] == "string"
    assert out["properties"]["name"]["nullable"] is True
    assert out["properties"]["items"]["items"]["type"] == "integer"
    assert out["properties"]["either"]["anyOf"][0]["type"] == "string"


def test_normalize_leaves_simple_schemas_intact():
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    out = _normalize_schema_for_openai(schema)
    assert out["properties"]["x"]["type"] == "string"
    assert "nullable" not in out["properties"]["x"]
