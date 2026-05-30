"""Config API routes."""

import copy
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from pydantic import BaseModel

from syll.config.loader import load_config, save_config
from syll.config.schema import Config, IdentityConfig
from syll.web.auth import require_admin

router = APIRouter(tags=["config"])

# Fields whose values should be masked
_SENSITIVE_KEYS = re.compile(r"(api_key|token|app_secret|encrypt_key)", re.IGNORECASE)

# Fields whose values are URLs that may embed user:pass@ credentials
_URL_KEYS = frozenset({"api_base", "proxy", "url", "bridge_url", "gateway_url", "api_url"})
# Matches scheme://user:pass@host... so we can detect userinfo in arbitrary values
_URL_USERINFO = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://[^/@]*@", re.IGNORECASE)


def _strip_url_userinfo(value: str) -> str:
    """Strip any user:pass@ segment from a URL, keeping scheme/host/path intact."""
    parts = urlsplit(value)
    if not parts.netloc or (parts.username is None and parts.password is None):
        return value
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


def _mask_sensitive(data: Any) -> Any:
    """Recursively mask sensitive fields, showing only last 4 chars."""
    if isinstance(data, dict):
        return {k: _mask_value(k, v) for k, v in data.items()}
    if isinstance(data, list):
        return [_mask_sensitive(item) for item in data]
    return data


def _mask_value(key: str, value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return _mask_sensitive(value)
    if isinstance(value, str) and _SENSITIVE_KEYS.search(key) and value:
        return f"...{value[-4:]}" if len(value) > 4 else "****"
    if isinstance(value, str) and value and (key in _URL_KEYS or _URL_USERINFO.match(value)):
        return _strip_url_userinfo(value)
    return value


def _merge_with_original(new_data: dict, original_data: dict) -> dict:
    """Keep original values for fields that are still masked."""
    for key, value in new_data.items():
        if isinstance(value, dict) and isinstance(original_data.get(key), dict):
            _merge_with_original(value, original_data[key])
        elif isinstance(value, str) and value.startswith("..."):
            # Still masked — keep original
            if key in original_data:
                new_data[key] = original_data[key]
    return new_data


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge `overlay` into a deep copy of `base`. Overlay values win for
    present keys; missing keys fall back to base. Used so partial PUT payloads
    (e.g. only `{identity: {...}}`) don't wipe other config sections."""
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _normalize_identity_aliases(data: dict | None) -> dict | None:
    """Map Syll-facing identity aliases onto the persisted config shape."""
    if not isinstance(data, dict):
        return data

    normalized = copy.deepcopy(data)
    identity = normalized.get("identity")
    if isinstance(identity, dict):
        if "syll_name" in identity and "ghost_name" not in identity:
            identity["ghost_name"] = identity["syll_name"]
        identity.pop("syll_name", None)
    return normalized


def _serialize_identity(identity: IdentityConfig) -> dict:
    """Return identity data with the Syll alias exposed for web clients."""
    data = identity.model_dump()
    data["syll_name"] = identity.ghost_name
    return data


@router.get("/config")
async def get_config():
    """Return the full config with sensitive fields masked.

    Phase 3 review-pass-6 (Critical): MCP `stdio.env` and HTTP/SSE `headers`
    have ARBITRARY user-supplied keys (`Authorization`, `MY_SECRET`, etc.)
    that the generic `_mask_sensitive` regex doesn't cover. We mask MCP
    env/headers via the dedicated walker, then strip the `mcp` section
    entirely from the response so this endpoint is never the place that
    leaks an MCP secret. The Pet UI uses `/api/v1/mcp` for MCP — single
    source of truth for that subtree. A defensive client (`saveConfig`)
    also strips `mcp` from PUT bodies.
    """
    from syll.web.routes._mcp_merge import mask_mcp_config_dict

    config = load_config()
    data = config.model_dump()
    if isinstance(data.get("identity"), dict):
        data["identity"]["syll_name"] = data["identity"].get("ghost_name", "")
    # Mask MCP env/headers BEFORE stripping — defense in depth in case the
    # strip is lifted in a future commit.
    if isinstance(data.get("mcp"), dict):
        data["mcp"] = mask_mcp_config_dict(data["mcp"])
    masked = _mask_sensitive(data)
    # Strip MCP from the response. UI must use /api/v1/mcp.
    masked.pop("mcp", None)
    return masked


@router.put("/config", dependencies=[Depends(require_admin)])
async def update_config(body: dict, request: Request):
    """Update and persist config.

    Masked fields (starting with '...') are left unchanged.
    When gui.enabled changes, hot-reload GUI tools in the agent loop.

    Phase 3: `mcp.*` is read-only via this endpoint. The dedicated route
    `/api/v1/mcp/servers/{name}` owns the consent/hash flow and is the only
    place an MCP change can land. We tolerate masked round-trips (a full
    GET-then-PUT from the Config tab) by mask-restoring the mcp section
    before computing the diff; only a real change forces 400.
    """
    current = load_config()
    current_data = current.model_dump()
    old_gui_config = current.tools.gui.model_dump()
    old_identity = current.identity.model_dump()

    # Phase 3: reject mcp.* mutations from this endpoint.
    incoming_mcp = body.get("mcp")
    if incoming_mcp is not None:
        from syll.web.routes._mcp_merge import restore_masked_mcp_server

        old_mcp = current_data.get("mcp", {}) or {}
        old_servers = (old_mcp.get("servers") or {}) if isinstance(old_mcp, dict) else {}
        new_servers_in = (incoming_mcp.get("servers") or {}) if isinstance(incoming_mcp, dict) else {}
        # Mask-restore each server pair-by-name before comparing.
        restored_servers: dict = {}
        for name, srv in new_servers_in.items():
            if isinstance(srv, dict):
                restored_servers[name] = restore_masked_mcp_server(srv, old_servers.get(name))
            else:
                restored_servers[name] = srv
        restored_mcp = {**incoming_mcp, "servers": restored_servers}
        if restored_mcp != old_mcp:
            raise HTTPException(
                status_code=400,
                detail=(
                    "mcp.* is read-only via /api/v1/config; use "
                    "/api/v1/mcp/servers/{name} to make MCP changes"
                ),
            )
        # Equal-after-mask-restore — strip from body and continue.
        body = {k: v for k, v in body.items() if k != "mcp"}

    # Deep-merge first so partial payloads (e.g. just {identity: ...}) don't
    # wipe other top-level sections, then resolve masked sensitive fields.
    normalized_body = _normalize_identity_aliases(body)
    merged = _deep_merge(current_data, normalized_body)
    merged = _merge_with_original(merged, current_data)
    new_config = Config.model_validate(merged)
    save_config(new_config)

    reloaded: list[str] = []

    new_gui_enabled = new_config.tools.gui.enabled
    new_gui_config = new_config.tools.gui.model_dump()
    gui_changed = old_gui_config != new_gui_config
    identity_changed = old_identity != new_config.identity.model_dump()
    agent_loop = getattr(request.app.state, "agent_loop", None)

    if agent_loop and identity_changed:
        try:
            agent_loop.context.identity = new_config.identity
            logger.info(
                f"Hot-reloaded identity: syll_name={new_config.identity.ghost_name!r} "
                f"user_name={new_config.identity.user_name!r}"
            )
            reloaded.append("identity")
        except Exception as e:
            logger.error(f"Failed to hot-reload identity: {e}")

    if agent_loop and gui_changed:
        try:
            from syll.agent.gui_skill import GUISkillStore
            from syll.agent.recorded_skill import RecordedSkillStore
            from syll.agent.tools.aloha_planner_tool import AlohaPlannerTool
            from syll.agent.tools.ui_tars import UITarsTool

            gui_config = new_config.tools.gui
            agent_loop.gui_config = gui_config

            for tool_name in ("gui_action", "gui_action_planned"):
                agent_loop.tools.unregister(tool_name)

            if new_gui_enabled:
                gui_skill_store = GUISkillStore(agent_loop.workspace)
                recorded_skill_store = RecordedSkillStore(agent_loop.workspace)

                ui_tars_tool = UITarsTool(
                    gui_config,
                    gui_skill_store=gui_skill_store,
                    aloha_skill_store=recorded_skill_store,
                    syll_config=new_config,
                )
                ui_tars_tool._event_store = agent_loop.event_store
                agent_loop.tools.register(ui_tars_tool)

                planner_tool = AlohaPlannerTool(
                    gui_config, recorded_skill_store, syll_config=new_config
                )
                planner_tool._event_store = agent_loop.event_store
                agent_loop.tools.register(planner_tool)

                # Keep the Adobe tools in sync with the GUI block (same helper
                # the agent loop uses at construction, so the two cannot drift).
                from syll.agent.adobe.register import register_adobe_tools
                register_adobe_tools(
                    agent_loop.tools,
                    agent_loop=agent_loop,
                    gui_config=gui_config,
                    syll_config=new_config,
                    workspace=agent_loop.workspace,
                    skill_store=recorded_skill_store,
                    event_store=agent_loop.event_store,
                )

                if hasattr(request.app.state, "gui_skill_store"):
                    request.app.state.gui_skill_store = gui_skill_store
                request.app.state.recorded_skill_store = recorded_skill_store
                request.app.state.aloha_skill_store = recorded_skill_store

                logger.info("Hot-reloaded GUI tools after config change")
            else:
                from syll.agent.adobe.register import unregister_adobe_tools
                unregister_adobe_tools(agent_loop.tools)
                logger.info("Unregistered GUI tools after config change")

            reloaded.append("gui")
        except Exception as e:
            logger.error(f"Failed to hot-reload GUI tools: {e}")

    return {"ok": True, "reloaded": reloaded}


# ── Dedicated identity endpoint ────────────────────────────────────────
# Bypasses /config merge logic so partial payloads from the Profile/Pet
# tabs cannot accidentally clobber other config sections.


class IdentityUpdate(BaseModel):
    syll_name: str | None = None
    ghost_name: str | None = None
    user_name: str | None = None
    primary_channel: str | None = None
    primary_chat_id: str | None = None
    rituals_enabled: bool | None = None


@router.get("/identity")
async def get_identity():
    """Return the current identity (all 5 fields)."""
    config = load_config()
    return _serialize_identity(config.identity)


def _clean_chat_id(raw: str | None, channel: str | None) -> str | None:
    """Strip accidental '<channel>:' prefix from a pasted chat_id.

    The agent loop logs `from feishu:ou_...` and users sometimes copy that
    whole string into the Chat ID field. Feishu (and most other channels)
    reject ids with a prefix. Strip it defensively at the entry point so
    the mistake can't propagate into the cron store.
    """
    if raw is None:
        return None
    cleaned = raw.strip()
    # Strip known channel prefixes (e.g. "feishu:ou_abc" -> "ou_abc")
    known_channels = {"feishu", "telegram", "discord", "whatsapp", "cli", "web"}
    if ":" in cleaned:
        prefix, rest = cleaned.split(":", 1)
        if prefix.lower() in known_channels:
            cleaned = rest.strip()
    # Also strip the user-supplied channel name if provided and matched
    if channel and ":" in cleaned:
        prefix, rest = cleaned.split(":", 1)
        if prefix.lower() == channel.lower():
            cleaned = rest.strip()
    return cleaned


@router.put("/identity", dependencies=[Depends(require_admin)])
async def update_identity(body: IdentityUpdate, request: Request):
    """Update the identity section. Partial payloads are merged with the
    current values (None means 'leave unchanged'). Hot-reloads agent context."""
    current = load_config()
    name_value = (
        body.syll_name
        if body.syll_name is not None
        else body.ghost_name
        if body.ghost_name is not None
        else current.identity.ghost_name
    )
    # Defensive: strip "<channel>:" prefix accidentally pasted into chat_id
    cleaned_chat_id = _clean_chat_id(
        body.primary_chat_id,
        body.primary_channel or current.identity.primary_channel,
    )
    new_identity = IdentityConfig(
        ghost_name=name_value,
        user_name=body.user_name if body.user_name is not None else current.identity.user_name,
        primary_channel=body.primary_channel if body.primary_channel is not None else current.identity.primary_channel,
        primary_chat_id=cleaned_chat_id if cleaned_chat_id is not None else current.identity.primary_chat_id,
        rituals_enabled=body.rituals_enabled if body.rituals_enabled is not None else current.identity.rituals_enabled,
    )

    if new_identity == current.identity:
        return {
            "ok": True,
            "reloaded": False,
            "identity": _serialize_identity(new_identity),
            "message": "no changes",
        }

    current.identity = new_identity
    save_config(current)
    logger.info(
        f"Identity updated: syll_name={new_identity.ghost_name!r} "
        f"user_name={new_identity.user_name!r} "
        f"primary_channel={new_identity.primary_channel!r} "
        f"primary_chat_id={new_identity.primary_chat_id!r} "
        f"rituals_enabled={new_identity.rituals_enabled}"
    )

    reloaded = False
    agent_loop = getattr(request.app.state, "agent_loop", None)
    if agent_loop is not None and getattr(agent_loop, "context", None) is not None:
        agent_loop.context.identity = new_identity
        reloaded = True
        logger.info("Hot-reloaded agent context.identity")
    else:
        logger.warning(
            "Identity saved to disk but agent_loop unavailable — restart needed for runtime effect"
        )

    return {
        "ok": True,
        "reloaded": reloaded,
        "identity": _serialize_identity(new_identity),
        "message": "saved" + (" (hot-reloaded)" if reloaded else " (restart needed)"),
    }


@router.get("/config/schema")
async def get_config_schema():
    """Return the JSON Schema for Config (for dynamic form rendering)."""
    return Config.model_json_schema()
