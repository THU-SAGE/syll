"""MCP HTTP routes — Phase 3.

Endpoints (all under /api/v1/mcp; mutating ones are gated by the global
`AdminGuardMiddleware` in syll/web/auth.py — the explicit `Depends(require_admin)`
declarations below are documentation-as-defense-in-depth):

  GET    /api/v1/mcp                                  full state (env/headers masked)
  PUT    /api/v1/mcp                                  update master MCP settings
  PUT    /api/v1/mcp/servers/{name}                   upsert + 409 + preview consent flow
  DELETE /api/v1/mcp/servers/{name}                   remove + reload tools
  POST   /api/v1/mcp/_test                            one-shot probe; no persistence
  POST   /api/v1/mcp/servers/{name}/reconnect         force reconnect; no persistence

Save-only-on-success: `apply_server(strict=True)` raises `MCPConnectionError`
on transport failure → caller returns 502 → `save_config` never runs. Boot
path is non-strict (per-server failure logged, not propagated).

Mask flow: GET returns env/header values masked (`...<last4>` / `****`). PUT
restores masked-equal values from the persisted config so a UI round-trip
that didn't edit secrets doesn't blow them away.

Phase 4 will add `POST /api/v1/mcp/bridges/{name}/install` for Stagehand —
not in scope here.
"""

from __future__ import annotations

import asyncio
import re
import uuid as _uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from pydantic import BaseModel

from syll.agent.mcp import (
    MCPConnectionError,
    MCPManager,
    command_hash,
    command_preview,
    stdio_params_changed,
)
from syll.config.loader import load_config, save_config
from syll.config.schema import MCPServerConfig
from syll.web.auth import require_admin
from syll.web.routes._mcp_merge import (
    mask_mcp_server_dict,
    restore_masked_mcp_server,
)
from syll.web.routes._mcp_templates import list_templates

router = APIRouter(tags=["mcp"])

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,30}$")


class MCPSettingsUpdate(BaseModel):
    """Root-level MCP settings that are safe to update without launching."""

    enabled: bool


def _get_manager(request: Request) -> MCPManager:
    mgr: MCPManager | None = getattr(request.app.state, "mcp_manager", None)
    if mgr is None:
        raise HTTPException(503, "MCP manager not initialized for this gateway")
    return mgr


def _live_status(mgr: MCPManager, name: str, server_cfg: MCPServerConfig) -> dict:
    """Compose status fields for GET responses from the manager's session table."""
    sess = mgr._sessions.get(name)
    if sess is not None:
        owned = mgr._owned_by_server.get(name, [])
        return {
            "status": sess.status,
            "error": sess.error,
            "available_tools": [raw for raw, _ in owned] or [t.name for t in sess.tools],
            "registered_tools": [registered for _, registered in owned],
        }
    return {
        "status": "disabled" if not server_cfg.enabled else "disconnected",
        "error": None,
        "available_tools": [],
        "registered_tools": [],
    }


def _serialize_server(name: str, server: MCPServerConfig, mgr: MCPManager) -> dict:
    body = mask_mcp_server_dict(server.model_dump())
    body["name"] = name
    body.update(_live_status(mgr, name, server))
    return body


def _sync_manager_config(mgr: MCPManager, mcp_cfg) -> None:
    """Keep the live manager's root config flags in sync with saved config."""
    mgr.cfg.enabled = mcp_cfg.enabled
    mgr.cfg.max_tools_per_server = mcp_cfg.max_tools_per_server
    mgr.cfg.max_total_tools = mcp_cfg.max_total_tools
    mgr.cfg.servers = mcp_cfg.servers


def _raise_master_disabled(message: str | None = None) -> None:
    raise HTTPException(
        status_code=409,
        detail={
            "error": "mcp_master_disabled",
            "message": message
            or (
                "MCP is globally disabled (mcp.enabled=False). Flip the master "
                "switch on before enabling individual servers."
            ),
        },
    )


def _require_stdio_consent(server_cfg: MCPServerConfig) -> None:
    """Raise 409 unless an enabled stdio server carries a matching hash."""
    if server_cfg.transport != "stdio" or not server_cfg.enabled:
        return
    expected = command_hash(server_cfg)
    if server_cfg.confirmed_command_hash == expected:
        return
    raise HTTPException(
        status_code=409,
        detail={
            "error": "confirmation_required",
            "required_command_hash": expected,
            "effective_command_preview": command_preview(server_cfg),
        },
    )


# ── GET /api/v1/mcp ─────────────────────────────────────────────────────


@router.get("/mcp")
async def get_mcp_state(request: Request):
    """Full MCP state: config (masked) + per-server live status + discovered tools.

    Public read (no admin token). Env/headers are always masked, so leaking
    this response to a foreign origin is non-fatal — and AdminGuard only
    gates UNSAFE methods, not GETs.
    """
    cfg_now = load_config()
    mgr = _get_manager(request)
    servers = {
        name: _serialize_server(name, server, mgr)
        for name, server in cfg_now.mcp.servers.items()
    }
    return {
        "enabled": cfg_now.mcp.enabled,
        "max_tools_per_server": cfg_now.mcp.max_tools_per_server,
        "max_total_tools": cfg_now.mcp.max_total_tools,
        "servers": servers,
    }


# ── PUT /api/v1/mcp ─────────────────────────────────────────────────────


@router.put("/mcp", dependencies=[Depends(require_admin)])
async def update_mcp_settings(body: MCPSettingsUpdate, request: Request):
    """Update safe root MCP settings.

    Toggling the master switch never launches subprocesses. Enabling MCP only
    permits future explicit server PUT/reconnect/test actions; disabling MCP
    stops live sessions immediately and refreshes the agent registry.
    """
    cfg_now = load_config()
    mgr = _get_manager(request)
    was_enabled = cfg_now.mcp.enabled
    cfg_now.mcp.enabled = body.enabled
    _sync_manager_config(mgr, cfg_now.mcp)

    if was_enabled and not cfg_now.mcp.enabled:
        await mgr.stop()

    save_config(cfg_now)
    agent_loop = getattr(request.app.state, "agent_loop", None)
    if agent_loop is not None and hasattr(agent_loop, "reload_mcp_tools"):
        agent_loop.reload_mcp_tools()

    return {
        "ok": True,
        "enabled": cfg_now.mcp.enabled,
        "max_tools_per_server": cfg_now.mcp.max_tools_per_server,
        "max_total_tools": cfg_now.mcp.max_total_tools,
    }


# ── PUT /api/v1/mcp/servers/{name} ──────────────────────────────────────


@router.put("/mcp/servers/{name}", dependencies=[Depends(require_admin)])
async def upsert_server(name: str, body: MCPServerConfig, request: Request):
    """Create or update a server.

    Save-only-on-success: the manager's apply_server runs in strict mode;
    a transport failure produces 502 and the on-disk config is unchanged.

    Consent flow: stdio servers being enabled (or having their launch params
    changed) require a matching `confirmed_command_hash`. When missing/stale,
    the route returns 409 with the freshly-computed hash and a human-readable
    preview, so the UI can show a confirmation modal and re-PUT with the
    echoed hash.
    """
    if not _NAME_RE.match(name):
        raise HTTPException(400, f"invalid server name {name!r}; must match {_NAME_RE.pattern}")

    cfg_now = load_config()
    # H2: master-switch gate — refuse to enable a server when MCP is globally
    # off. Saving a server entry with enabled=False is still permitted (config
    # only, no launch). Mirrors the manager's apply_server gate; redundant by
    # design (route returns a clear 409 with reason; manager silently no-ops).
    if not cfg_now.mcp.enabled and body.enabled:
        _raise_master_disabled()
    old_server = cfg_now.mcp.servers.get(name)

    # Restore masked env/headers BEFORE we hash, so "...beef" round-trips
    # produce the same hash as the persisted real value.
    body_dump = body.model_dump()
    if old_server is not None:
        body_dump = restore_masked_mcp_server(body_dump, old_server.model_dump())
    try:
        body = MCPServerConfig.model_validate(body_dump)
    except Exception as e:
        raise HTTPException(400, f"invalid server config: {e}")

    # Consent: a stdio server is about to LAUNCH a subprocess only when
    # `body.enabled and body.transport=='stdio'`. Disabled stdio entries are
    # config-only and don't need consent. SSE / streamable-HTTP servers don't
    # spawn local processes — also no consent.
    #
    # When consent IS required, we accept any `body.confirmed_command_hash`
    # that matches the freshly-computed hash — including a re-send of the
    # previously-confirmed value (no-op PUT round-trips).
    # `stdio_params_changed` is no longer used in the consent decision — kept
    # imported for `apply_server`'s diff path inside the manager.
    _ = stdio_params_changed
    _require_stdio_consent(body)

    # Apply to live manager. strict=True so connect/list_tools failure raises
    # → caller doesn't persist a broken config.
    mgr = _get_manager(request)
    _sync_manager_config(mgr, cfg_now.mcp)
    try:
        await mgr.apply_server(name, body, strict=True)
    except MCPConnectionError as e:
        raise HTTPException(502, f"server failed to connect: {e}")

    # Persist + refresh agent registry.
    cfg_now.mcp.servers[name] = body
    save_config(cfg_now)
    agent_loop = getattr(request.app.state, "agent_loop", None)
    if agent_loop is not None and hasattr(agent_loop, "reload_mcp_tools"):
        agent_loop.reload_mcp_tools()

    logger.info(f"mcp[{name}] upserted (enabled={body.enabled})")
    return {"ok": True, "server": _serialize_server(name, body, mgr)}


# ── DELETE /api/v1/mcp/servers/{name} ───────────────────────────────────


@router.delete("/mcp/servers/{name}", dependencies=[Depends(require_admin)])
async def delete_server(name: str, request: Request):
    cfg_now = load_config()
    if name not in cfg_now.mcp.servers:
        raise HTTPException(404, f"unknown server {name!r}")
    mgr = _get_manager(request)
    await mgr.remove_server(name)
    cfg_now.mcp.servers.pop(name, None)
    save_config(cfg_now)
    agent_loop = getattr(request.app.state, "agent_loop", None)
    if agent_loop is not None and hasattr(agent_loop, "reload_mcp_tools"):
        agent_loop.reload_mcp_tools()
    logger.info(f"mcp[{name}] deleted")
    return {"ok": True, "name": name}


# ── POST /api/v1/mcp/_test ──────────────────────────────────────────────


@router.post("/mcp/_test", dependencies=[Depends(require_admin)])
async def test_server_endpoint(body: MCPServerConfig, request: Request):
    """Probe a server config without persisting it.

    Used by the UI's "Test connection" button on unsaved Add-server forms.
    Hard-capped at MCPManager.test_server's TEST_TIMEOUT (5s).

    **Consent semantic (review-pass-6 H3)**: a `_test` call against a stdio
    config IS launching a subprocess. Apply the same `confirmed_command_hash`
    gate as PUT — the user must see the resolved command preview and click
    Confirm before the test runs. UI flow: click Test → 409 → modal with
    preview → Confirm → re-POST with hash → run.

    SSE / streamableHttp probes don't run local processes, so they bypass
    the consent gate. Master `mcp.enabled=False` blocks all probes.
    """
    cfg_now = load_config()
    # Master-switch gate.
    if not cfg_now.mcp.enabled:
        _raise_master_disabled(
            "MCP is globally disabled. Flip mcp.enabled=true before running "
            "connection tests."
        )

    mgr = _get_manager(request)
    _sync_manager_config(mgr, cfg_now.mcp)

    # Mask-restore against the persisted server of the same shape, if any.
    # The body might carry masked values when the user is testing an EDIT to
    # an already-configured server.
    body_dump = body.model_dump()
    name_hint = request.headers.get("X-Mcp-Probe-Name")
    if name_hint and name_hint in cfg_now.mcp.servers:
        body_dump = restore_masked_mcp_server(
            body_dump, cfg_now.mcp.servers[name_hint].model_dump()
        )
        try:
            body = MCPServerConfig.model_validate(body_dump)
        except Exception as e:
            raise HTTPException(400, f"invalid server config: {e}")

    # Stdio consent: same hash + preview as PUT. The user can re-send the
    # body with `confirmed_command_hash` after seeing the modal.
    _require_stdio_consent(body)

    return await mgr.test_server(body)


# ── POST /api/v1/mcp/servers/{name}/reconnect ───────────────────────────


@router.post("/mcp/servers/{name}/reconnect", dependencies=[Depends(require_admin)])
async def reconnect_server(name: str, request: Request):
    """Force a reconnect without changing the on-disk config."""
    if not _NAME_RE.match(name):
        raise HTTPException(400, "invalid server name")
    cfg_now = load_config()
    server_cfg = cfg_now.mcp.servers.get(name)
    if server_cfg is None:
        raise HTTPException(404, f"unknown server {name!r}")
    mgr = _get_manager(request)
    if server_cfg.enabled:
        if not cfg_now.mcp.enabled:
            _raise_master_disabled(
                "MCP is globally disabled. Flip mcp.enabled=true before reconnecting "
                "servers."
            )
        _require_stdio_consent(server_cfg)
    _sync_manager_config(mgr, cfg_now.mcp)
    await mgr.remove_server(name)
    if server_cfg.enabled:
        try:
            await mgr.apply_server(name, server_cfg, strict=True)
        except MCPConnectionError as e:
            # Don't 502 here — reconnect is best-effort, and the failure is
            # already broadcast as `mcp_server_status:failed`.
            return {
                "ok": False,
                "name": name,
                "error": str(e),
                "server": _serialize_server(name, server_cfg, mgr),
            }
    agent_loop = getattr(request.app.state, "agent_loop", None)
    if agent_loop is not None and hasattr(agent_loop, "reload_mcp_tools"):
        agent_loop.reload_mcp_tools()
    return {"ok": True, "name": name, "server": _serialize_server(name, server_cfg, mgr)}


# ── GET /api/v1/mcp/diag ────────────────────────────────────────────────


@router.get("/mcp/diag", dependencies=[Depends(require_admin)])
async def get_mcp_diag(request: Request):
    """Diagnostic snapshot of the live MCP runtime — what tools the agent
    loop ACTUALLY has registered right now.

    Useful when verifying hot reload: if you just clicked "Confirm and
    enable" but the agent still doesn't see the tools, this endpoint
    answers "did `agent_loop.reload_mcp_tools()` actually run?". Admin-only:
    values are not secrets, but they are operational introspection.
    """
    mgr = _get_manager(request)
    agent_loop = getattr(request.app.state, "agent_loop", None)
    owned = sorted(getattr(agent_loop, "_mcp_owned", set()) or [])
    sessions = {}
    for name, sess in (mgr._sessions or {}).items():
        sessions[name] = {
            "status": sess.status,
            "error": sess.error,
            "raw_tool_count": len(sess.tools),
            "registered_count": len(mgr._owned_by_server.get(name, [])),
        }
    return {
        "loop_class": type(agent_loop).__name__ if agent_loop else None,
        "loop_has_reload": hasattr(agent_loop, "reload_mcp_tools"),
        "mcp_master_enabled": mgr.cfg.enabled,
        "loop_mcp_owned_count": len(owned),
        "loop_mcp_owned": owned,
        "live_sessions": sessions,
    }


# ── GET /api/v1/mcp/templates ───────────────────────────────────────────


@router.get("/mcp/templates")
async def get_mcp_templates():
    """Default-shipped server templates. Public read.

    The UI uses these to seed the Add-server form. `requires_install` rows
    show a "needs install" hint instead of "Test connection" until the
    underlying bridge has been installed via `syll bridge install <id>`.
    """
    return {"templates": list_templates()}


# ── GET /api/v1/mcp/bridges ─────────────────────────────────────────────


@router.get("/mcp/bridges")
async def get_mcp_bridges():
    """List known MCP bridges and their install status. Public read."""
    from syll.agent.mcp_bridges import list_installed

    return {"bridges": list_installed()}


# ── POST /api/v1/mcp/bridges/{name}/install ─────────────────────────────
#
# Background-job model: the route returns `{job_id}` immediately. The
# install runs as an asyncio task; progress lines flow over the existing
# WebSocket as `mcp_bridge_install_progress` events. UI subscribes to
# those, streams them into a modal, and refreshes on `status: "ok" | "error"`.


class _BridgeInstallBody(BaseModel):
    version: str | None = None
    force: bool = False


# Track in-flight install jobs at the app level so a re-entrant POST
# returns the existing job_id instead of spawning a duplicate process.
def _bridge_jobs(request: Request) -> dict:
    state = request.app.state
    jobs = getattr(state, "_bridge_install_jobs", None)
    if jobs is None:
        jobs = {}
        state._bridge_install_jobs = jobs
    return jobs


@router.post(
    "/mcp/bridges/{name}/install",
    dependencies=[Depends(require_admin)],
    status_code=202,
)
async def install_bridge_route(name: str, body: _BridgeInstallBody, request: Request):
    """Trigger a bridge install in the background.

    Returns 202 with `{job_id}` immediately. Progress flows over the
    existing chat WebSocket as `mcp_bridge_install_progress` events:
        {type, job_id, bridge, line, status, extra?}

    Master-switch gate is intentionally NOT applied here: installing a
    bridge is a build-time action that doesn't launch the MCP server. The
    user must subsequently flip the master switch and Save the resulting
    server entry to actually use it.
    """
    from syll.agent.mcp_bridges import BRIDGE_ALLOWLIST, install_bridge

    if name not in BRIDGE_ALLOWLIST:
        raise HTTPException(404, f"unknown bridge {name!r}")

    jobs = _bridge_jobs(request)
    if name in jobs and not jobs[name].done():
        return {"job_id": jobs[name]._syll_job_id, "status": "already_running"}

    job_id = _uuid.uuid4().hex[:12]
    broadcast = getattr(request.app.state, "broadcast_ws", None)

    async def _emit(progress) -> None:
        if broadcast is None:
            return
        try:
            await broadcast({
                "type": "mcp_bridge_install_progress",
                "job_id": progress.job_id,
                "bridge": progress.bridge,
                "line": progress.line,
                "status": progress.status,
                "extra": progress.extra,
            })
        except Exception as e:
            logger.warning(f"bridge install broadcast failed: {e}")

    async def _run() -> None:
        try:
            await install_bridge(
                name,
                version=body.version,
                job_id=job_id,
                progress=_emit,
                force=body.force,
            )
        except Exception as e:
            logger.warning(f"bridge install {name!r} failed: {e}")
            await _emit(type("P", (), {
                "job_id": job_id, "bridge": name,
                "line": str(e), "status": "error", "extra": {},
            })())

    task = asyncio.create_task(_run(), name=f"bridge-install-{name}")
    task._syll_job_id = job_id  # type: ignore[attr-defined]
    jobs[name] = task
    return {"job_id": job_id, "status": "started"}


# ── Re-export for app.py mounting ───────────────────────────────────────

__all__ = ["router"]
