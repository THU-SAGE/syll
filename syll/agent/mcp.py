"""MCP (Model Context Protocol) client for Syll — single-file foundation.

Brings external MCP servers (stdio / SSE / streamable-HTTP) into Syll's
ToolRegistry as namespaced tools (`mcp__<server>__<tool>`).

Patterns ported verbatim from HKUDS/nanobot's `nanobot/agent/tools/mcp.py`
(transient-retry list, Windows shell wrap, schema flatten, per-server
AsyncExitStack), with security and lifecycle improvements documented inline:

  * Per-step timeouts on connect / list_tools / call_tool / test
    (NanoBot has only the call_tool timeout).
  * `_closing` gate rejects new calls BEFORE incrementing the inflight
    counter, so drain observes accurate counts.
  * Per-session `disconnect(drain_timeout=...)` that awaits inflight=0
    before closing the transport.
  * `apply_server(strict=True)` for HTTP routes (raise on connect/list
    failure → caller refuses save) vs `strict=False` for boot
    (per-server failure → status='failed', broadcast, suite continues).
  * `boot_validate()` refuses to start any enabled stdio whose
    `command_hash` doesn't match the persisted `confirmed_command_hash` —
    defends against direct config-file tampering.
  * Manager owns the explicit set of registered tool names so
    `AgentLoop.reload_mcp_tools()` never clobbers a non-MCP tool that
    happens to start with `mcp__`.
  * `ImageContent` blocks persisted to
    workspace/.syll/mcp_media/<server>/<sha>.<ext> (NanoBot stringifies
    and loses these).

Section index:
  - Errors
  - Constants (timeouts, transient-exception names, Windows wrap list)
  - Namespace helpers (`canonical_name`, `parse_namespaced`)
  - Schema normalization for OpenAI-compatible providers
  - Transport builders (stdio env hygiene + Windows wrap)
  - MCPSession — one server's persistent client session
  - MCPTool — Syll Tool ABC adapter with jsonschema validation
  - Safety — `command_hash`, `command_preview`, `stdio_params_changed`
  - MCPManager — lifecycle, hot-reload diff, owned-name tracking
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Awaitable, Callable

import jsonschema
from loguru import logger
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from mcp.types import (
    CallToolResult,
    ImageContent,
    TextContent,
)
from mcp.types import (
    Tool as McpTool,
)

from syll.agent.tools.base import Tool
from syll.config.schema import (
    MCPConfig,
    MCPServerConfig,
    MCPStdioParams,
)

# ── Errors ────────────────────────────────────────────────────────────────


class MCPError(Exception):
    """Base for MCP-related errors raised by Syll's manager."""


class MCPConnectionError(MCPError):
    """Raised when initialize() / list_tools() / transport open fails."""


class MCPInvocationError(MCPError):
    """Raised when call_tool fails for transport / lifecycle reasons (not
    application errors — those come back as `CallToolResult.is_error=True`)."""


class MCPHashMismatchError(MCPError):
    """Raised by `boot_validate()` when an enabled stdio server's
    confirmed_command_hash doesn't match the actual launch params."""


# ── Constants ─────────────────────────────────────────────────────────────

# Per-step timeouts (seconds). Phase 1b improvement over NanoBot which times
# only the call_tool path.
CONNECT_TIMEOUT = 10.0
LIST_TOOLS_TIMEOUT = 10.0
TEST_TIMEOUT = 5.0
DRAIN_TIMEOUT = 5.0

# Transient exception class names (NanoBot port: nanobot/agent/tools/mcp.py:19-44).
# Connection-flake errors that warrant exactly one retry; application errors
# are surfaced unchanged and `asyncio.TimeoutError` / `CancelledError` are
# never retried.
TRANSIENT_EXC_NAMES = frozenset({
    "ClosedResourceError",
    "BrokenResourceError",
    "EndOfStream",
    "BrokenPipeError",
    "ConnectionResetError",
    "ConnectionRefusedError",
    "ConnectionAbortedError",
    "ConnectionError",
})

# Windows-only shell launchers that must be wrapped with `cmd.exe /d /c`
# (NanoBot port: nanobot:52-81). Without the wrap, `npx`/`yarn`/etc. silently
# buffer stdout under subprocess and the MCP handshake hangs.
_WIN_SHELL_COMMANDS = frozenset({"npx", "npm", "pnpm", "yarn", "bunx"})


# ── Namespace ─────────────────────────────────────────────────────────────


def canonical_name(server: str, tool: str) -> str:
    """Compute the registered name for an MCP tool.

    Format: `mcp__<server>__<tool>` — Claude-Code convention; OpenAI
    tool-name-safe (regex `^[a-zA-Z0-9_-]{1,64}$`). Length-clamped to 64
    by truncating + appending a 4-char SHA1 suffix when the canonical form
    would overflow.
    """
    sanitized = re.sub(r"[^A-Za-z0-9_-]", "_", tool)
    full = f"mcp__{server}__{sanitized}"
    if len(full) <= 64:
        return full
    digest = hashlib.sha1(full.encode()).hexdigest()[:4]
    return f"{full[:59]}_{digest}"  # 59 + 1 + 4 = 64


def parse_namespaced(name: str) -> tuple[str, str] | None:
    """Inverse of `canonical_name` for un-clamped names. Returns None if the
    input is not a Syll-owned MCP tool name."""
    m = re.match(r"^mcp__([a-z][a-z0-9_]{0,30})__(.+)$", name)
    if not m:
        return None
    return (m.group(1), m.group(2))


# ── Schema normalization (NanoBot port: nanobot:103-141) ──────────────────


def _normalize_schema_for_openai(schema: dict) -> dict:
    """Flatten `type: ["x","null"]` → `type: "x"` + `nullable: true`.

    OpenAI-compatible providers (and several LiteLLM passthroughs) reject
    the JSON-Schema union form. NanoBot does this; we extend by recursing
    into nested schemas (allOf/anyOf/oneOf, items, additionalProperties,
    $defs, definitions) so deep schemas don't slip through.
    """
    if not isinstance(schema, dict):
        return schema
    out = dict(schema)
    typ = out.get("type")
    if isinstance(typ, list):
        non_null = [t for t in typ if t != "null"]
        if "null" in typ and len(non_null) == 1:
            out["type"] = non_null[0]
            out["nullable"] = True
        elif len(non_null) == 1:
            out["type"] = non_null[0]
    for key in ("properties", "definitions", "$defs"):
        if key in out and isinstance(out[key], dict):
            out[key] = {
                k: _normalize_schema_for_openai(v) for k, v in out[key].items()
            }
    if "items" in out and isinstance(out["items"], dict):
        out["items"] = _normalize_schema_for_openai(out["items"])
    if "additionalProperties" in out and isinstance(out["additionalProperties"], dict):
        out["additionalProperties"] = _normalize_schema_for_openai(out["additionalProperties"])
    for key in ("allOf", "anyOf", "oneOf"):
        if key in out and isinstance(out[key], list):
            out[key] = [_normalize_schema_for_openai(s) for s in out[key]]
    return out


# ── Transports ────────────────────────────────────────────────────────────


def _normalize_windows_stdio_command(
    command: str, args: list[str]
) -> tuple[str, list[str]]:
    """Wrap shell-launcher commands with `cmd.exe /d /c` on Windows.

    NanoBot port. On non-Windows this is a no-op.
    """
    if sys.platform != "win32":
        return command, args
    base = Path(command).stem.lower()
    if base in _WIN_SHELL_COMMANDS:
        return "cmd.exe", ["/d", "/c", command, *args]
    return command, args


def build_stdio_params(
    cfg: MCPStdioParams,
    *,
    workspace_path: Path | None = None,
    restrict_to_workspace: bool = False,
) -> StdioServerParameters:
    """Build `StdioServerParameters` with env hygiene and Windows shell-wrap.

    The mcp Python SDK's `stdio_client` already restricts inherited env to
    a safe whitelist (HOME, LOGNAME, PATH, SHELL, TERM, USER on POSIX;
    Windows equivalent). User-supplied `cfg.env` is merged on top by the
    SDK. We only set:
      - `cwd` defaulting to `workspace_path` when `restrict_to_workspace`
        is on and the user didn't specify one.
      - Windows wrap when applicable.
    """
    command, args = _normalize_windows_stdio_command(cfg.command, list(cfg.args))
    env: dict[str, str] | None = dict(cfg.env) if cfg.env else None

    cwd = cfg.cwd
    if cwd is None and restrict_to_workspace and workspace_path is not None:
        cwd = str(workspace_path)

    return StdioServerParameters(
        command=command,
        args=args,
        env=env,
        cwd=cwd,
    )


# ── Safety: command hash + preview ────────────────────────────────────────


def command_hash(cfg: MCPServerConfig) -> str:
    """sha256 of the canonical (transport, params) tuple — the consent token.

    Hashed bytes are launch-relevant only: transport, command, args, env,
    cwd OR url + headers. NOT enabled / enabled_tools / propagate / timeout —
    those flip without changing what runs on the user's machine.
    """
    h = hashlib.sha256()
    h.update(cfg.transport.encode())
    if cfg.transport == "stdio" and cfg.stdio is not None:
        s = cfg.stdio
        h.update(b"\x10")
        h.update(s.command.encode())
        for a in s.args:
            h.update(b"\x00")
            h.update(a.encode())
        h.update(b"\x01")
        for k in sorted(s.env or {}):
            h.update(k.encode())
            h.update(b"=")
            h.update((s.env[k] or "").encode())
            h.update(b"\x00")
        if s.cwd:
            h.update(b"\x02")
            h.update(s.cwd.encode())
    elif cfg.transport in ("sse", "streamableHttp"):
        params = cfg.sse if cfg.transport == "sse" else cfg.http
        if params is not None:
            h.update(b"\x20")
            h.update(params.url.encode())
            for k in sorted(params.headers or {}):
                h.update(b"\x00")
                h.update(k.encode())
                h.update(b"=")
                h.update((params.headers[k] or "").encode())
    return f"sha256:{h.hexdigest()[:24]}"


def command_preview(cfg: MCPServerConfig) -> str:
    """Human-readable rendering of the launch command for the consent UI.

    Secrets (env values, header values) are NOT included — the modal shows
    keys-only because the values are masked in the GET response anyway.
    """
    if cfg.transport == "stdio" and cfg.stdio is not None:
        cmd = cfg.stdio.command
        argv = " ".join(cfg.stdio.args)
        cwd = f" (cwd: {cfg.stdio.cwd})" if cfg.stdio.cwd else ""
        env_keys = sorted(cfg.stdio.env or {})
        env_part = (
            f" with env keys: {', '.join(env_keys)}" if env_keys else ""
        )
        return f"$ {cmd} {argv}{cwd}{env_part}".rstrip()
    if cfg.transport == "sse" and cfg.sse is not None:
        keys = sorted(cfg.sse.headers or {})
        hdr = f" with headers: {', '.join(keys)}" if keys else ""
        return f"SSE → {cfg.sse.url}{hdr}"
    if cfg.transport == "streamableHttp" and cfg.http is not None:
        keys = sorted(cfg.http.headers or {})
        hdr = f" with headers: {', '.join(keys)}" if keys else ""
        return f"HTTP → {cfg.http.url}{hdr}"
    return f"<{cfg.transport}>"


def stdio_params_changed(
    new: MCPServerConfig, old: MCPServerConfig | None
) -> bool:
    """Return True iff the launch-relevant params differ between old and new."""
    if old is None:
        return True
    if new.transport != old.transport:
        return True
    return command_hash(new) != command_hash(old)


# ── MCPSession ────────────────────────────────────────────────────────────


class MCPSession:
    """One persistent MCP client session.

    Owns its own `AsyncExitStack` so cancellations don't propagate across
    servers (NanoBot port). Tracks in-flight tool-calls via a refcount; a
    `_closing` flag rejects new calls BEFORE incrementing so `drain()`
    observes accurate counts.

    Status flow:
        disconnected → connecting → connected
                           ↓             ↓
                         failed       closing → disconnected
    """

    def __init__(
        self,
        name: str,
        cfg: MCPServerConfig,
        *,
        workspace_path: Path | None = None,
        restrict_to_workspace: bool = False,
    ):
        self.name = name
        self.cfg = cfg
        self.workspace_path = workspace_path
        self.restrict_to_workspace = restrict_to_workspace

        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._owner_task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._connect_future: asyncio.Future[None] | None = None
        self._tools: list[McpTool] = []
        self._closing = False
        self._inflight = 0
        self._inflight_zero = asyncio.Event()
        self._inflight_zero.set()
        self.status: str = "disconnected"
        self.error: str | None = None

    # ── Connection lifecycle ──────────────────────────────────────────

    async def connect(self) -> None:
        """Open transport, initialize, and list tools.

        Per-step timeouts (10s connect/init, 10s list_tools). Raises
        `MCPConnectionError` on any failure; caller decides whether to
        propagate or swallow.
        """
        if self._session is not None:
            return
        if self._owner_task is not None and not self._owner_task.done():
            if self._connect_future is not None:
                await asyncio.shield(self._connect_future)
            return

        self.status = "connecting"
        self.error = None
        self._closing = False
        self._stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        self._connect_future = loop.create_future()
        self._owner_task = asyncio.create_task(
            self._run_owner(self._connect_future),
            name=f"mcp-session-{self.name}",
        )
        # `_run_owner` applies the advertised per-step timeouts. The outer
        # wait is a last-resort guard so a regression in the owner task cannot
        # hang callers forever.
        connect_budget = CONNECT_TIMEOUT * 2 + LIST_TOOLS_TIMEOUT + 2.0
        try:
            await asyncio.wait_for(
                asyncio.shield(self._connect_future),
                timeout=connect_budget,
            )
        except asyncio.CancelledError:
            await self._cancel_owner()
            raise
        except MCPConnectionError:
            raise
        except Exception as e:
            self.status = "failed"
            self.error = f"{type(e).__name__}: {e}"
            await self._cancel_owner()
            raise MCPConnectionError(self.error) from e

    async def _run_owner(self, connect_future: asyncio.Future[None]) -> None:
        """Own the SDK transport contexts for this session.

        anyio cancel scopes used by the MCP SDK must be exited by the same
        task that entered them. Keeping the open AsyncExitStack in a dedicated
        owner task lets HTTP route hot-reloads call `disconnect()` from any
        task without leaking subprocesses or hitting cross-task cancel-scope
        errors.
        """
        self._stack = AsyncExitStack()
        try:
            await self._stack.__aenter__()
            # Wrap transport open in the connect timeout too (review-pass-5
            # finding M4). A hung SSE / streamable-HTTP open could otherwise
            # block start() / apply_server(strict=True) past the advertised
            # 10s budget.
            # Do not use asyncio.wait_for(self._open_transport()) here:
            # wait_for wraps the coroutine in a child task, while the MCP SDK
            # transports enter anyio cancel scopes that must later be exited
            # by the same task. asyncio.timeout keeps the enter/exit inside
            # this owner task and still enforces the connect budget.
            async with asyncio.timeout(CONNECT_TIMEOUT):
                transport_streams = await self._open_transport()
            self._session = await self._stack.enter_async_context(
                ClientSession(*transport_streams)
            )
            await asyncio.wait_for(
                self._session.initialize(), timeout=CONNECT_TIMEOUT
            )
            tools_result = await asyncio.wait_for(
                self._session.list_tools(), timeout=LIST_TOOLS_TIMEOUT
            )
            self._tools = list(tools_result.tools)
            self.status = "connected"
            logger.info(
                f"mcp[{self.name}] connected; {len(self._tools)} tool(s)"
            )
            if not connect_future.done():
                connect_future.set_result(None)
            assert self._stop_event is not None
            await self._stop_event.wait()
        except asyncio.CancelledError:
            self.status = "failed"
            self.error = "cancelled"
            if not connect_future.done():
                connect_future.set_exception(MCPConnectionError(self.error))
        except Exception as e:
            self.status = "failed"
            self.error = f"{type(e).__name__}: {e}"
            logger.warning(f"mcp[{self.name}] connect failed: {self.error}")
            if not connect_future.done():
                connect_future.set_exception(MCPConnectionError(self.error))
        finally:
            await self._cleanup_stack()
            if self._closing and self.status != "failed":
                self.status = "disconnected"
            if not connect_future.done():
                err = self.error or "connection closed before initialize"
                connect_future.set_exception(MCPConnectionError(err))

    async def _open_transport(self):
        """Open the configured transport. Returns a 2-tuple of (read, write)."""
        cfg = self.cfg
        assert self._stack is not None
        if cfg.transport == "stdio":
            assert cfg.stdio is not None
            params = build_stdio_params(
                cfg.stdio,
                workspace_path=self.workspace_path,
                restrict_to_workspace=self.restrict_to_workspace,
            )
            return await self._stack.enter_async_context(stdio_client(params))
        if cfg.transport == "sse":
            assert cfg.sse is not None
            return await self._stack.enter_async_context(
                sse_client(cfg.sse.url, headers=cfg.sse.headers or None)
            )
        if cfg.transport == "streamableHttp":
            assert cfg.http is not None
            http_client = None
            if cfg.http.headers:
                http_client = await self._stack.enter_async_context(
                    create_mcp_http_client(headers=cfg.http.headers)
                )
            read, write, _get_id = await self._stack.enter_async_context(
                streamable_http_client(
                    cfg.http.url,
                    http_client=http_client,
                )
            )
            return (read, write)
        raise MCPConnectionError(f"unknown transport: {cfg.transport!r}")

    async def disconnect(self, *, drain_timeout: float = DRAIN_TIMEOUT) -> None:
        """Close cleanly: gate new calls, drain in-flight, close transport.

        Idempotent. After `disconnect()`, `call_tool` raises
        `MCPInvocationError("session closing")` immediately without
        incrementing the inflight counter.
        """
        if self._closing:
            return
        self._closing = True
        self.status = "closing"
        if self._inflight > 0:
            try:
                await asyncio.wait_for(
                    self._inflight_zero.wait(), timeout=drain_timeout
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"mcp[{self.name}] drain timed out with "
                    f"{self._inflight} call(s) in flight"
                )
        if self._stop_event is not None:
            self._stop_event.set()
        owner_task = self._owner_task
        if owner_task is not None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(owner_task), timeout=drain_timeout
                )
            except asyncio.TimeoutError:
                logger.warning(f"mcp[{self.name}] owner task did not stop in time")
                await self._cancel_owner()
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise
                logger.warning(f"mcp[{self.name}] disconnect observed cancellation")
            except MCPConnectionError:
                # Owner already recorded/logged the connection state.
                pass
        self.status = "disconnected"
        self._owner_task = None
        self._stop_event = None
        self._connect_future = None

    async def _cleanup_stack(self) -> None:
        if self._stack is None:
            return
        try:
            await self._stack.aclose()
        except asyncio.CancelledError:
            # SDK cancel-scope cancellation during unwind — only propagate
            # when the OUTER task is actually being cancelled. Otherwise it's
            # a benign unwind artefact (anyio task-group exit) we can swallow.
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
            logger.warning(f"mcp[{self.name}] cleanup observed cancel-scope cancellation")
        except Exception as e:
            logger.warning(f"mcp[{self.name}] cleanup error: {e}")
        finally:
            self._stack = None
            self._session = None

    async def _cancel_owner(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        owner_task = self._owner_task
        if owner_task is not None and not owner_task.done():
            owner_task.cancel()
            try:
                await owner_task
            except (asyncio.CancelledError, MCPConnectionError):
                pass
            except Exception as e:
                logger.warning(f"mcp[{self.name}] owner cancel error: {e}")
        self._owner_task = None
        self._stop_event = None
        self._connect_future = None

    # ── Properties ────────────────────────────────────────────────────

    @property
    def tools(self) -> list[McpTool]:
        return list(self._tools)

    @property
    def connected(self) -> bool:
        return self.status == "connected" and not self._closing

    # ── Tool invocation ───────────────────────────────────────────────

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> CallToolResult:
        """Invoke `name` with `arguments`.

        The `_closing` gate is checked BEFORE the inflight refcount
        increments, so `disconnect()`'s drain observes a correct count
        regardless of timing.

        Raises `MCPInvocationError` on lifecycle issues (closing, not
        connected). `asyncio.TimeoutError` for tool-timeout. Other
        exceptions propagate; the caller (MCPTool.execute) decides whether
        to retry-once based on transient classification.
        """
        if self._closing:
            raise MCPInvocationError("session closing")
        if self._session is None:
            raise MCPInvocationError("session not connected")
        timeout = self.cfg.tool_timeout_seconds

        self._inflight += 1
        self._inflight_zero.clear()
        try:
            return await asyncio.wait_for(
                self._session.call_tool(name, arguments=arguments),
                timeout=timeout,
            )
        finally:
            self._inflight -= 1
            if self._inflight == 0:
                self._inflight_zero.set()


# ── MCPTool ───────────────────────────────────────────────────────────────


class MCPTool(Tool):
    """Adapt a discovered MCP tool to Syll's Tool ABC."""

    def __init__(self, session: MCPSession, raw_tool: McpTool, *, override_name: str | None = None):
        self._session = session
        self._raw_tool = raw_tool
        self._raw_name = raw_tool.name
        # Keep BOTH versions: the raw schema is authoritative for
        # validation (jsonschema doesn't understand `nullable`, so a
        # flattened `type: "string", nullable: true` would reject None
        # inputs the MCP server actually accepts). The normalized version
        # is exposed via `parameters` for the LLM provider's tool-defs.
        self._raw_input_schema = raw_tool.inputSchema or {
            "type": "object", "properties": {}
        }
        self._normalized_input_schema = _normalize_schema_for_openai(
            self._raw_input_schema
        )
        self._override_name = override_name  # used when manager resolves a collision

    @property
    def name(self) -> str:
        return self._override_name or canonical_name(
            self._session.name, self._raw_name
        )

    @property
    def description(self) -> str:
        return self._raw_tool.description or f"MCP tool {self._raw_name}"

    @property
    def parameters(self) -> dict:
        return self._normalized_input_schema

    def validate_params(self, params: dict) -> list[str]:
        """Use `jsonschema`'s full implementation against the RAW MCP schema.

        Validating against the OpenAI-normalized schema would reject `None`
        for fields whose original type was `["string", "null"]` (the
        normalized form replaces it with `nullable: true`, which jsonschema
        does not honor — it's an OpenAPI-3 / OpenAI extension).
        """
        try:
            jsonschema.validate(params, self._raw_input_schema)
            return []
        except jsonschema.ValidationError as e:
            return [str(e.message)]
        except jsonschema.SchemaError:
            # Server schema malformed — let the MCP server reject downstream.
            return []

    async def execute(self, **kwargs: Any) -> str:
        """Invoke the MCP tool with NanoBot-style transient retry-once."""
        attempts = 0
        last_error: BaseException | None = None
        while attempts < 2:
            try:
                result = await self._session.call_tool(self._raw_name, kwargs)
                return self._coerce_content(result)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                # Don't retry timeouts or cancellations — caller wants to know.
                raise
            except MCPInvocationError as e:
                # Lifecycle issue — retry once after backoff.
                last_error = e
                attempts += 1
                if attempts < 2:
                    await asyncio.sleep(1.0)
            except Exception as e:
                if type(e).__name__ in TRANSIENT_EXC_NAMES:
                    last_error = e
                    attempts += 1
                    if attempts < 2:
                        await asyncio.sleep(1.0)
                else:
                    return f"Error: {type(e).__name__}: {e}"
        return f"Error: {type(last_error).__name__}: {last_error}"

    def _coerce_content(self, result: CallToolResult) -> str:
        """CallToolResult → string for the LLM.

        ImageContent persisted to workspace/.syll/mcp_media/<server>/<sha>.<ext>
        (NanoBot stringifies and loses these). Other non-text blocks fall
        through to `str()`.
        """
        # MCP SDK 1.27 exposes the field as `isError` (camelCase). Use
        # getattr to tolerate both spellings across SDK minor versions.
        if getattr(result, "isError", False) or getattr(result, "is_error", False):
            text_parts = [
                b.text for b in (result.content or []) if isinstance(b, TextContent)
            ]
            return "Error: " + ("; ".join(text_parts) or "tool returned isError=True")
        parts: list[str] = []
        for block in result.content or []:
            if isinstance(block, TextContent):
                parts.append(block.text)
            elif isinstance(block, ImageContent):
                parts.append(self._persist_image(block))
            else:
                parts.append(str(block))
        return "\n".join(parts) or "(no output)"

    def _persist_image(self, block: ImageContent) -> str:
        """Save base64 ImageContent under workspace/.syll/mcp_media/<server>/."""
        ws = self._session.workspace_path
        if ws is None:
            return f"<inline image ({block.mimeType}, {len(block.data)} b64-bytes)>"
        media_dir = ws / ".syll" / "mcp_media" / self._session.name
        try:
            media_dir.mkdir(parents=True, exist_ok=True)
            raw = base64.b64decode(block.data)
            digest = hashlib.sha1(raw).hexdigest()[:16]
            ext = (block.mimeType.split("/")[-1] or "png").lower()
            target = media_dir / f"{digest}.{ext}"
            if not target.exists():
                target.write_bytes(raw)
            return f"[image saved: {target}]"
        except Exception as e:
            logger.warning(f"mcp[{self._session.name}] image persist failed: {e}")
            return f"<image ({block.mimeType}) — persist failed>"


# ── MCPManager ────────────────────────────────────────────────────────────


BroadcastFn = Callable[[dict], Awaitable[None]]


class MCPManager:
    """Lifecycle owner + name-tracking authority for all configured servers.

    `start()` is best-effort: per-server connect failure is logged and
    broadcast but never raises. `apply_server(strict=True)` raises typed
    errors so an HTTP route can refuse to persist a save.

    Hot-reload: tools are tracked by manager-owned canonical names so
    `AgentLoop.reload_mcp_tools()` can unregister exactly what we own and
    never clobber non-MCP tools.
    """

    def __init__(
        self,
        cfg: MCPConfig,
        *,
        workspace_path: Path | None = None,
        restrict_to_workspace: bool = False,
        broadcast: BroadcastFn | None = None,
    ):
        self.cfg = cfg
        self.workspace_path = workspace_path
        self.restrict_to_workspace = restrict_to_workspace
        self.broadcast: BroadcastFn | None = broadcast
        self._sessions: dict[str, MCPSession] = {}
        self._owned_names: set[str] = set()
        # Per server: ordered list of (raw_name, canonical_or_collision_name).
        self._owned_by_server: dict[str, list[tuple[str, str]]] = {}
        self._lock = asyncio.Lock()

    # ── Boot validation ───────────────────────────────────────────────

    async def boot_validate(self) -> None:
        """Refuse to start any enabled stdio whose hash doesn't match.

        Called from gateway boot before `start()`. A user editing
        `~/.syll/config.json` directly cannot enable a stdio server without
        also recomputing and writing the matching `confirmed_command_hash`.
        """
        if not self.cfg.enabled:
            return
        for name, server in self.cfg.servers.items():
            if not server.enabled:
                continue
            if server.transport != "stdio":
                continue
            expected = command_hash(server)
            if server.confirmed_command_hash != expected:
                raise MCPHashMismatchError(
                    f"server {name!r}: confirmed_command_hash does not match "
                    f"the on-disk launch command (expected {expected}). "
                    "Either disable the server or re-confirm via the MCP tab "
                    "(/api/v1/mcp/servers)."
                )

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        """Open sessions for every enabled server (best-effort, non-fatal)."""
        if not self.cfg.enabled:
            return
        async with self._lock:
            for name, server in self.cfg.servers.items():
                if not server.enabled:
                    continue
                await self._connect_server(name, server, strict=False)

    async def stop(self) -> None:
        """Close every session sequentially in REVERSE start order (LIFO).

        IMPORTANT: do NOT use `asyncio.gather`. The MCP SDK's `stdio_client`
        and `streamable_http_client` enter anyio cancel scopes inside the
        calling task; exiting them from a different task (which `gather`
        spawns) raises "Attempted to exit cancel scope in a different task
        than it was entered in".

        And: anyio cancel scopes are TASK-LOCAL and must be exited LIFO.
        When start() opened servers A then B, the cancel-scope stack is
        [A, B] — exiting A first violates "current task's current cancel
        scope" and surfaces as `RuntimeError`. Reverse the order on stop.
        """
        async with self._lock:
            # Preserve the start order from cfg.servers (dict-insertion order
            # in modern Python), then reverse for LIFO disconnect.
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._owned_names.clear()
            self._owned_by_server.clear()
        for s in reversed(sessions):
            try:
                await s.disconnect()
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise
                logger.warning(f"mcp[{s.name}] stop observed cancel-scope cancellation")
            except Exception as e:
                logger.warning(f"mcp[{s.name}] stop error: {e}")

    async def apply_server(
        self,
        name: str,
        server: MCPServerConfig,
        *,
        strict: bool = True,
    ) -> None:
        """Apply an upsert for a single server.

        **Master-switch gate (review-pass-6 H2)**: when `cfg.enabled=False`,
        this is inert — no subprocess launches, no transport opens. An
        existing session for `name` is still torn down so `enabled=False`
        flips reach the live state. Defense in depth: a malicious caller
        that bypasses the HTTP-route master check still cannot launch a
        process via direct manager access.

        Three branches:

          1. `enabled=False` — tear down any existing session and return.
          2. `enabled=True` AND (no old session OR launch params changed OR
             was previously disabled) — full reconnect.
          3. `enabled=True` AND launch params unchanged AND was already
             enabled — pure metadata refresh: reuse the live session, just
             rebuild the owned-name set against the new `enabled_tools`.
             Critically, do NOT open a new subprocess — that would leak the
             old session and rename tools (review-pass-5 finding H2).

        With `strict=True`, connect/list_tools failure raises typed errors
        (HTTP route refuses to persist). With `strict=False`, failures are
        logged + broadcast and the manager continues (boot path).
        """
        async with self._lock:
            old = self._sessions.get(name)

            # Master-switch gate (H2). If MCP is disabled at the cfg level,
            # this is the only path that can launch a subprocess — refuse,
            # but still tear down any straggler session so toggling off the
            # master switch eventually reaches the live state.
            if not self.cfg.enabled:
                if old is not None:
                    self._sessions.pop(name, None)
                    self._drop_owned(name)
                    await old.disconnect()
                logger.warning(
                    f"mcp[{name}] apply_server skipped: master mcp.enabled=False"
                )
                return

            # Branch 1: turning off.
            if not server.enabled:
                if old is not None:
                    self._sessions.pop(name, None)
                    self._drop_owned(name)
                    await old.disconnect()
                return

            # Branch 2: needs a fresh session.
            needs_reconnect = (
                old is None
                or not old.cfg.enabled
                or stdio_params_changed(server, old.cfg)
            )
            if needs_reconnect:
                if old is not None:
                    self._sessions.pop(name, None)
                    self._drop_owned(name)
                    await old.disconnect()
                await self._connect_server(name, server, strict=strict)
                return

            # Branch 3: metadata-only refresh on the existing session.
            old.cfg = server  # picks up new propagate_to_subagents / timeout
            self._drop_owned(name)
            owned = self._select_and_register_tools(name, server, old.tools)
            await self._broadcast_status(
                name, "connected", tool_count=len(owned)
            )

    async def remove_server(self, name: str) -> None:
        async with self._lock:
            old = self._sessions.pop(name, None)
            self._drop_owned(name)
        if old:
            await old.disconnect()

    # ── Connect / register ────────────────────────────────────────────

    async def _connect_server(
        self, name: str, server: MCPServerConfig, *, strict: bool
    ) -> None:
        session = MCPSession(
            name,
            server,
            workspace_path=self.workspace_path,
            restrict_to_workspace=self.restrict_to_workspace,
        )
        try:
            await session.connect()
        except MCPConnectionError as e:
            await self._broadcast_status(name, "failed", error=str(e))
            if strict:
                raise
            return
        self._sessions[name] = session
        owned = self._select_and_register_tools(name, server, session.tools)
        await self._broadcast_status(
            name, "connected", tool_count=len(owned)
        )

    def _select_and_register_tools(
        self,
        name: str,
        server: MCPServerConfig,
        tools: list[McpTool],
    ) -> list[tuple[str, str]]:
        """Filter discovered tools by enabled_tools + caps; record owned names.

        Returns the list of (raw_name, registered_name) pairs we own.
        """
        allowed_set = set(server.enabled_tools or ["*"])
        wildcard = "*" in allowed_set
        cap = self.cfg.max_tools_per_server

        # Stage 1: enabled_tools filter.
        candidates: list[McpTool] = []
        for t in tools:
            if wildcard or t.name in allowed_set:
                candidates.append(t)

        # Stage 1b: enabled_tools spelling drift warning (Phase 1b improvement).
        if not wildcard:
            discovered = {t.name for t in tools}
            unknown = sorted(allowed_set - discovered)
            if unknown:
                logger.warning(
                    f"mcp[{name}] enabled_tools references unknown names: "
                    f"{unknown}; discovered: {sorted(discovered)}"
                )

        # Stage 2: per-server cap (only meaningful when wildcard).
        if wildcard and len(candidates) > cap:
            logger.warning(
                f"mcp[{name}] {len(candidates)} discovered tools exceed "
                f"max_tools_per_server={cap}; truncating. Set explicit "
                "enabled_tools to silence this warning."
            )
            candidates = candidates[:cap]

        # Stage 3: total cap.
        owned: list[tuple[str, str]] = []
        for t in candidates:
            if len(self._owned_names) >= self.cfg.max_total_tools:
                logger.warning(
                    f"mcp[{name}] further tools dropped: max_total_tools="
                    f"{self.cfg.max_total_tools} reached"
                )
                break
            registered = self._allocate_name(name, t.name)
            self._owned_names.add(registered)
            owned.append((t.name, registered))

        self._owned_by_server[name] = owned
        return owned

    def _allocate_name(self, server: str, raw_tool: str) -> str:
        """Compute canonical_name and resolve collisions WITHIN the 64-char
        limit.

        If `base` is already 64 chars (because canonical_name had to clamp
        with a hash), naive `f"{base}_{i}"` would produce 66+ chars and
        break OpenAI's tool-name limit. Trim from the right and append the
        suffix so the total stays at 64 (review-pass-5 finding L5).
        """
        base = canonical_name(server, raw_tool)
        if base not in self._owned_names:
            return base
        for i in range(2, 10000):
            suffix = f"_{i}"
            budget = 64 - len(suffix)
            candidate = base[:budget] + suffix
            if candidate not in self._owned_names:
                return candidate
        # Pathological — fall back to a fresh hash-only id within budget.
        digest = hashlib.sha1(
            f"{server}/{raw_tool}/{len(self._owned_names)}".encode()
        ).hexdigest()[:16]
        return f"mcp__{server[:8]}__{digest}"[:64]

    def _drop_owned(self, name: str) -> None:
        owned = self._owned_by_server.pop(name, [])
        for _, registered in owned:
            self._owned_names.discard(registered)

    # ── Iteration for AgentLoop ───────────────────────────────────────

    def iter_enabled_tools(self) -> list[MCPTool]:
        """Yield MCPTool adapters for every connected, enabled server."""
        out: list[MCPTool] = []
        for name, session in self._sessions.items():
            if not session.connected:
                continue
            owned = self._owned_by_server.get(name, [])
            raw_to_registered = dict(owned)
            for raw in session.tools:
                if raw.name not in raw_to_registered:
                    continue
                out.append(
                    MCPTool(session, raw, override_name=raw_to_registered[raw.name])
                )
        return out

    def iter_propagating_tools(self) -> list[MCPTool]:
        """Subset of `iter_enabled_tools` whose servers opted into subagent
        propagation."""
        return [
            t
            for t in self.iter_enabled_tools()
            if t._session.cfg.propagate_to_subagents
        ]

    # ── Test helper ───────────────────────────────────────────────────

    async def test_server(self, server: MCPServerConfig) -> dict[str, Any]:
        """Open a one-shot connection, list tools, and tear down. Used by the
        UI's `POST /_test` route. Hard-capped at `TEST_TIMEOUT`.

        Never persists. Never mutates the manager's session table.
        """
        session = MCPSession(
            "_test",
            server,
            workspace_path=self.workspace_path,
            restrict_to_workspace=self.restrict_to_workspace,
        )
        start = asyncio.get_event_loop().time()
        try:
            await asyncio.wait_for(session.connect(), timeout=TEST_TIMEOUT)
            elapsed_ms = int((asyncio.get_event_loop().time() - start) * 1000)
            tools = [t.name for t in session.tools]
            return {
                "ok": True,
                "latency_ms": elapsed_ms,
                "tool_count": len(tools),
                "tools": tools,
            }
        except (MCPConnectionError, asyncio.TimeoutError) as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        finally:
            try:
                await asyncio.wait_for(session.disconnect(), timeout=2.0)
            except Exception:
                pass

    # ── Broadcast ─────────────────────────────────────────────────────

    async def _broadcast_status(
        self, name: str, status: str, **extra: Any
    ) -> None:
        if self.broadcast is None:
            return
        try:
            await self.broadcast({
                "type": "mcp_server_status",
                "server": name,
                "status": status,
                **extra,
            })
        except Exception as e:
            logger.warning(f"mcp[{name}] broadcast {status} failed: {e}")
