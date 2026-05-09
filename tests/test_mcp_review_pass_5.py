"""Phase 1b review-pass-5 regressions.

Five findings, one test each (or close to it):
  C1 — Path-aware key conversion preserves env / header / server-name keys.
  H2 — apply_server(metadata-only) does NOT spawn a second subprocess.
  H3 — validate_params accepts None for nullable fields (raw schema, not
       OpenAI-normalized).
  M4 — Transport-open is wrapped by the connect timeout.
  L5 — Collision suffixing keeps the registered name within 64 chars.
"""

from __future__ import annotations

import sys
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import pytest

from syll.agent.mcp import (
    MCPConnectionError,
    MCPManager,
    MCPSession,
    MCPTool,
    canonical_name,
)
from syll.config.loader import convert_keys, convert_to_camel
from syll.config.schema import (
    MCPConfig,
    MCPHttpParams,
    MCPServerConfig,
    MCPStdioParams,
)

# ── C1: path-aware key conversion ────────────────────────────────────────


def test_convert_to_camel_preserves_env_var_names():
    data = {
        "mcp": {
            "servers": {
                "fs": {
                    "transport": "stdio",
                    "stdio": {
                        "env": {"OPENAI_API_KEY": "sk-x", "MY_TOKEN": "y"},
                    },
                }
            }
        }
    }
    out = convert_to_camel(data)
    env = out["mcp"]["servers"]["fs"]["stdio"]["env"]
    assert env == {"OPENAI_API_KEY": "sk-x", "MY_TOKEN": "y"}, env


def test_convert_keys_preserves_http_header_names():
    data = {
        "mcp": {
            "servers": {
                "remote": {
                    "transport": "streamableHttp",
                    "http": {
                        "headers": {
                            "Authorization": "Bearer abc",
                            "x-api-key": "xyz",
                        },
                    },
                }
            }
        }
    }
    out = convert_keys(data)
    hdrs = out["mcp"]["servers"]["remote"]["http"]["headers"]
    assert hdrs == {"Authorization": "Bearer abc", "x-api-key": "xyz"}, hdrs


def test_round_trip_preserves_env_keys_and_command_hash():
    """Save/load roundtrip via the FULL pipeline must NOT alter env keys.

    Path-aware key conversion only kicks in when the dict is anchored at
    the real config path (`mcp.servers.<name>.stdio.env`), so we exercise
    that path here, mirroring `save_config` / `load_config`.
    """
    from syll.agent.mcp import command_hash

    cfg = MCPServerConfig(
        transport="stdio",
        stdio=MCPStdioParams(
            command="node",
            args=["./bridge.js"],
            env={"OPENAI_API_KEY": "sk-x", "MY_SECRET": "y"},
        ),
        enabled=True,
    )
    h_before = command_hash(cfg)

    # Mimic the real save: full root config → convert_to_camel → JSON.
    root = {"mcp": {"servers": {"fs": cfg.model_dump()}}}
    saved = convert_to_camel(root)
    # Mimic the real load: convert_keys → MCPServerConfig.model_validate.
    loaded_root = convert_keys(saved)
    cfg_after = MCPServerConfig.model_validate(
        loaded_root["mcp"]["servers"]["fs"]
    )
    h_after = command_hash(cfg_after)

    assert cfg_after.stdio.env == cfg.stdio.env, (
        f"env keys drifted: {cfg_after.stdio.env}"
    )
    assert h_before == h_after, (
        f"hash drift across roundtrip: {h_before} → {h_after}"
    )


def test_round_trip_preserves_server_name_keys():
    """Server names live in the dict KEYS under `mcp.servers`. The naming
    rule allows lowercase only, but we still preserve verbatim so a future
    relaxation can't silently rename ids."""
    data = {
        "mcp": {
            "servers": {
                "my_server_42": {
                    "transport": "stdio",
                    "stdio": {"command": "echo"},
                }
            }
        }
    }
    out = convert_to_camel(data)
    assert "my_server_42" in out["mcp"]["servers"], out["mcp"]["servers"]


# ── H2: apply_server metadata-only path ──────────────────────────────────


def _echo_server(*, enabled_tools: list[str] | None = None) -> MCPServerConfig:
    return MCPServerConfig(
        transport="stdio",
        stdio=MCPStdioParams(
            command=sys.executable,
            args=["-m", "tests.fixtures.echo_mcp_server"],
        ),
        enabled=True,
        enabled_tools=enabled_tools if enabled_tools is not None else ["*"],
    )


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


@pytest.mark.timeout(60)
async def test_apply_server_metadata_only_keeps_same_session(repo_root, monkeypatch):
    """Changing only enabled_tools must NOT spawn a second subprocess.

    Pre-fix bug: apply_server tore down nothing (because launch params
    matched), then called _connect_server unconditionally → second
    subprocess + collision-suffixed names like mcp__echo__echo_2.
    """
    monkeypatch.chdir(repo_root)
    mgr = MCPManager(MCPConfig())

    cfg = _echo_server(enabled_tools=["*"])
    await mgr.apply_server("echo", cfg, strict=True)
    assert "echo" in mgr._sessions
    session_id_before = id(mgr._sessions["echo"])
    names_before = {t.name for t in mgr.iter_enabled_tools()}
    assert names_before == {"mcp__echo__echo", "mcp__echo__add"}

    # Change ONLY enabled_tools — launch params unchanged.
    cfg2 = _echo_server(enabled_tools=["echo"])
    try:
        await mgr.apply_server("echo", cfg2, strict=True)
        # Same Python session object — no reconnect happened.
        assert id(mgr._sessions["echo"]) == session_id_before, (
            "metadata-only change spawned a NEW session — old session leaks"
        )
        # Owned names refreshed to the narrower set.
        names_after = {t.name for t in mgr.iter_enabled_tools()}
        assert names_after == {"mcp__echo__echo"}, names_after
        # No collision-suffixed names.
        assert "mcp__echo__echo_2" not in mgr._owned_names
    finally:
        await mgr.stop()


@pytest.mark.timeout(60)
async def test_apply_server_propagate_change_keeps_same_session(repo_root, monkeypatch):
    """Toggling propagate_to_subagents is metadata-only too."""
    monkeypatch.chdir(repo_root)
    mgr = MCPManager(MCPConfig())

    cfg = _echo_server()
    cfg.propagate_to_subagents = True
    await mgr.apply_server("echo", cfg, strict=True)
    sid = id(mgr._sessions["echo"])

    cfg2 = _echo_server()
    cfg2.propagate_to_subagents = False
    try:
        await mgr.apply_server("echo", cfg2, strict=True)
        assert id(mgr._sessions["echo"]) == sid
        # Live session config now reflects the new flag (used by
        # iter_propagating_tools).
        assert mgr._sessions["echo"].cfg.propagate_to_subagents is False
        # iter_propagating_tools should now return [].
        assert mgr.iter_propagating_tools() == []
    finally:
        await mgr.stop()


# ── H3: validate_params against raw schema ───────────────────────────────


def test_validate_params_accepts_null_for_nullable_union():
    """Pre-fix bug: validate_params used the OpenAI-normalized schema, where
    `type: ["string","null"]` had been flattened to `type: "string",
    nullable: true`. jsonschema doesn't understand `nullable` and rejected
    None. Validation must use the RAW schema so nullable unions accept None."""
    from types import SimpleNamespace

    fake_session = SimpleNamespace(name="fake", workspace_path=None)
    fake_raw_tool = SimpleNamespace(
        name="thing",
        description="",
        inputSchema={
            "type": "object",
            "properties": {
                "label": {"type": ["string", "null"]},
            },
            "required": ["label"],
        },
    )
    tool = MCPTool(fake_session, fake_raw_tool)

    # None is allowed by the raw union schema.
    assert tool.validate_params({"label": None}) == [], (
        "validate_params must accept None for nullable union — review-pass-5 H3"
    )
    # A normal string is also allowed.
    assert tool.validate_params({"label": "hi"}) == []
    # Missing required → error.
    assert tool.validate_params({}) != []
    # Wrong type → error.
    assert tool.validate_params({"label": 42}) != []


# ── M4: transport-open under connect timeout ─────────────────────────────


@pytest.mark.timeout(20)
async def test_open_transport_failure_returns_within_connect_timeout():
    """An unreachable HTTP MCP endpoint must not hang past CONNECT_TIMEOUT.

    Pre-fix bug: only initialize() and list_tools() were under wait_for; the
    streamable-HTTP open itself could block longer. The failure must surface
    as MCPConnectionError and not as a local call-signature TypeError.
    """
    import time

    cfg = MCPServerConfig(
        transport="streamableHttp",
        # Non-routable IP → connection stalls on most systems.
        http=MCPHttpParams(url="http://10.255.255.1:65000/mcp"),
        enabled=True,
    )
    s = MCPSession("unreachable", cfg)
    t0 = time.monotonic()
    try:
        with pytest.raises(MCPConnectionError) as exc:
            await s.connect()
        assert "TypeError" not in str(exc.value)
    finally:
        elapsed = time.monotonic() - t0
        # CONNECT_TIMEOUT is 10s; allow ~4s of slack for cleanup paths.
        assert elapsed < 14.0, f"connect took {elapsed:.1f}s, expected <14"
        try:
            await s.disconnect()
        except BaseException:
            pass


async def test_streamable_http_headers_use_async_client(monkeypatch):
    """streamable_http_client no longer accepts a `headers=` kwarg in MCP SDK
    1.27. Headers must be installed on the provided HTTP client."""
    import syll.agent.mcp as mcp_mod

    seen: dict = {}

    @asynccontextmanager
    async def fake_streamable_http_client(url, *, http_client=None, terminate_on_close=True):
        seen["url"] = url
        seen["terminate_on_close"] = terminate_on_close
        seen["headers"] = dict(http_client.headers) if http_client else {}
        yield ("read", "write", lambda: "session-id")

    monkeypatch.setattr(
        mcp_mod,
        "streamable_http_client",
        fake_streamable_http_client,
    )

    cfg = MCPServerConfig(
        transport="streamableHttp",
        http=MCPHttpParams(
            url="https://mcp.example/mcp",
            headers={"Authorization": "Bearer secret", "x-api-key": "abc"},
        ),
        enabled=True,
    )
    session = MCPSession("remote", cfg)
    session._stack = AsyncExitStack()
    await session._stack.__aenter__()
    try:
        streams = await session._open_transport()
        assert streams == ("read", "write")
        assert seen["url"] == "https://mcp.example/mcp"
        # httpx normalizes header lookup case-insensitively; dict casing is
        # lower-case internally, but the values must be preserved.
        assert seen["headers"]["authorization"] == "Bearer secret"
        assert seen["headers"]["x-api-key"] == "abc"
    finally:
        await session._stack.aclose()
        session._stack = None


# ── L5: collision-suffix re-clamp to 64 ──────────────────────────────────


def test_collision_suffix_keeps_within_64_chars():
    """When canonical_name is already 64-char-clamped (with hash), a
    collision must not produce a 66+ char registered name."""
    mgr = MCPManager(MCPConfig())
    server = "fs"
    long_tool = "a" * 70

    base = canonical_name(server, long_tool)
    assert len(base) == 64

    # First call returns the base.
    n1 = mgr._allocate_name(server, long_tool)
    assert n1 == base
    mgr._owned_names.add(n1)

    # Force a collision: a different raw tool that hashes to the same base
    # is hard to construct, so simulate it by allocating with the same
    # raw_tool — the second allocation must produce a fresh distinct name
    # while staying ≤ 64 chars.
    n2 = mgr._allocate_name(server, long_tool)
    assert n2 != n1
    assert len(n2) <= 64, f"collision suffix produced {len(n2)} chars: {n2!r}"
    mgr._owned_names.add(n2)

    # Even after dozens of collisions, every registered name fits.
    for _ in range(20):
        n = mgr._allocate_name(server, long_tool)
        assert len(n) <= 64
        assert n not in mgr._owned_names
        mgr._owned_names.add(n)
