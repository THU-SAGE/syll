"""Configuration loading utilities."""

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from loguru import logger

from syll.config.schema import Config


def get_config_path() -> Path:
    """Get the default configuration file path."""
    return Path.home() / ".syll" / "config.json"


def get_data_dir() -> Path:
    """Get the Syll data directory."""
    from syll.utils.helpers import get_data_path
    return get_data_path()


def load_config(config_path: Path | None = None) -> Config:
    """
    Load configuration from file or create default.

    Args:
        config_path: Optional path to config file. Uses default if not provided.

    Returns:
        Loaded configuration object.
    """
    path = config_path or get_config_path()

    if path.exists():
        try:
            with open(path) as f:
                data = json.load(f)
            data = _migrate_config(data)
            return Config.model_validate(convert_keys(data))
        except (json.JSONDecodeError, ValueError) as e:
            print(f"Warning: Failed to load config from {path}: {e}")
            print("Using default configuration.")

    return Config()


def save_config(config: Config, config_path: Path | None = None) -> None:
    """
    Save configuration to file.

    Args:
        config: Configuration to save.
        config_path: Optional path to save to. Uses default if not provided.
    """
    path = config_path or get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Config holds API keys + bot tokens; keep the parent dir owner-only.
    try:
        path.parent.chmod(0o700)
    except OSError as e:
        logger.warning(f"could not chmod {path.parent}: {e}")

    # Convert to camelCase format
    data = config.model_dump()
    data = convert_to_camel(data)

    # Write to a temp file in the same dir, lock it down, then atomically
    # replace the target so readers never see a partial / world-readable file.
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        try:
            os.chmod(tmp_path, 0o600)
        except OSError as e:
            logger.warning(f"could not chmod {tmp_path}: {e}")
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _migrate_config(data: dict) -> dict:
    """Migrate old config formats to current."""
    # Move tools.exec.restrictToWorkspace → tools.restrictToWorkspace
    tools = data.get("tools", {})
    exec_cfg = tools.get("exec", {})
    if "restrictToWorkspace" in exec_cfg and "restrictToWorkspace" not in tools:
        tools["restrictToWorkspace"] = exec_cfg.pop("restrictToWorkspace")

    # Migrate provider-based config → purpose-based models config
    if "providers" in data and "models" not in data:
        providers = data.get("providers", {})
        agents = data.get("agents", {})
        defaults = agents.get("defaults", {})
        gui = tools.get("gui", {})
        ui_tars = gui.get("uiTars", gui.get("ui_tars", {}))

        # Build the models config from old providers
        models: dict = {}

        # Chat: use the old default model + matching provider key
        old_model = defaults.get("model", "")
        if old_model:
            chat_key = _find_provider_key(old_model, providers)
            # Detect OpenRouter and prefix model for litellm
            or_provider = providers.get("openrouter", {})
            or_key = or_provider.get("apiKey", or_provider.get("api_key", ""))
            if or_key and chat_key == or_key and not old_model.startswith("openrouter/"):
                old_model = f"openrouter/{old_model}"
            models["chat"] = {
                "model": old_model,
                "apiKey": chat_key,
            }

        # Actor: from gui.uiTars
        if ui_tars.get("model") or ui_tars.get("apiKey") or ui_tars.get("apiBase"):
            models["actor"] = {
                "model": ui_tars.get("model", ""),
                "apiKey": ui_tars.get("apiKey", ui_tars.get("api_key", "")),
                "apiBase": ui_tars.get("apiBase", ui_tars.get("api_base", "")),
            }

        # Planner: from gui.plannerModel + matching provider key
        planner_model = gui.get("plannerModel", gui.get("planner_model", ""))
        if planner_model:
            planner_key = _find_provider_key(planner_model, providers)
            models["planner"] = {
                "model": planner_model,
                "apiKey": planner_key,
            }

        # STT: from providers.groq
        groq = providers.get("groq", {})
        if groq.get("apiKey") or groq.get("api_key"):
            models["stt"] = {
                "apiKey": groq.get("apiKey", groq.get("api_key", "")),
            }

        data["models"] = models

        # Clean up migrated fields from gui
        gui.pop("uiTars", None)
        gui.pop("ui_tars", None)
        gui.pop("plannerModel", None)
        gui.pop("planner_model", None)
        gui.pop("claudeApiKey", None)
        gui.pop("claude_api_key", None)

        # Remove old top-level providers (no longer in schema)
        data.pop("providers", None)

        # Remove old agents.defaults.model (moved to models.chat.model)
        defaults.pop("model", None)

    return data


def _find_provider_key(model: str, providers: dict) -> str:
    """Match a model name to its provider API key (for migration)."""
    model_lower = model.lower()
    keyword_map = {
        "openrouter": "openrouter", "deepseek": "deepseek",
        "anthropic": "anthropic", "claude": "anthropic",
        "openai": "openai", "gpt": "openai",
        "gemini": "gemini", "groq": "groq",
        "zhipu": "zhipu", "glm": "zhipu",
        "moonshot": "moonshot", "kimi": "moonshot",
    }
    for keyword, provider_name in keyword_map.items():
        if keyword in model_lower:
            provider = providers.get(provider_name, {})
            return provider.get("apiKey", provider.get("api_key", ""))
    # Fallback: return first available key
    for p in providers.values():
        if isinstance(p, dict):
            key = p.get("apiKey", p.get("api_key", ""))
            if key:
                return key
    return ""


# ── Path-aware key conversion ─────────────────────────────────────────
#
# Phase 1b finding (Critical): the global key converters mangle user-supplied
# env var names ("OPENAI_API_KEY" → "OPENAIApiKey" → "o_p_e_n_a_i_api_key")
# and HTTP/SSE header names. These dicts have ARBITRARY string keys provided
# by the user and must be preserved verbatim across save/load roundtrips.
#
# `_PRESERVE_DICT_KEY_PATHS` enumerates the dotted paths whose immediate
# DICT VALUES must keep their keys verbatim. The path components match keys
# at every nesting level after `convert_*` walks the tree; a `*` matches any
# key (used for the dict-of-server-name layer under `mcp.servers`).
#
# Examples covered:
#   mcp.servers.<server-name>.stdio.env    — env var names
#   mcp.servers.<server-name>.http.headers — HTTP header names
#   mcp.servers.<server-name>.sse.headers  — SSE header names
#   mcp.servers                            — server name keys themselves
#
# The server-name keys themselves are bound to ^[a-z][a-z0-9_]{0,30}$ so they
# would survive snake_case/camelCase translation today, but we lock them in
# as preserved-verbatim too so future relaxations don't silently break ids.

_PRESERVE_DICT_KEY_PATHS = (
    ("mcp", "servers"),                              # dict-of-server-name → server cfg
    ("mcp", "servers", "*", "stdio", "env"),         # env var names
    ("mcp", "servers", "*", "http", "headers"),      # HTTP header names
    ("mcp", "servers", "*", "sse", "headers"),       # SSE header names
)


def _path_matches(actual: tuple[str, ...], pattern: tuple[str, ...]) -> bool:
    if len(actual) != len(pattern):
        return False
    return all(p == "*" or p == a for a, p in zip(actual, pattern))


def _is_preserve_path(path: tuple[str, ...]) -> bool:
    return any(_path_matches(path, p) for p in _PRESERVE_DICT_KEY_PATHS)


def convert_keys(data: Any, _path: tuple[str, ...] = ()) -> Any:
    """Convert camelCase keys to snake_case for Pydantic.

    Path-aware: under `_PRESERVE_DICT_KEY_PATHS`, the immediate dict's keys
    are preserved verbatim (still recurses into values). This protects
    arbitrary user-provided keys (env var names, HTTP header names, MCP
    server names) from accidental case stomping.
    """
    if isinstance(data, dict):
        if _is_preserve_path(_path):
            return {k: convert_keys(v, _path + (k,)) for k, v in data.items()}
        return {
            camel_to_snake(k): convert_keys(v, _path + (camel_to_snake(k),))
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [convert_keys(item, _path) for item in data]
    return data


def convert_to_camel(data: Any, _path: tuple[str, ...] = ()) -> Any:
    """Convert snake_case keys to camelCase, path-aware (see `convert_keys`)."""
    if isinstance(data, dict):
        if _is_preserve_path(_path):
            return {k: convert_to_camel(v, _path + (k,)) for k, v in data.items()}
        return {
            snake_to_camel(k): convert_to_camel(v, _path + (snake_to_camel(k),))
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [convert_to_camel(item, _path) for item in data]
    return data


def camel_to_snake(name: str) -> str:
    """Convert camelCase to snake_case."""
    result = []
    for i, char in enumerate(name):
        if char.isupper() and i > 0:
            result.append("_")
        result.append(char.lower())
    return "".join(result)


def snake_to_camel(name: str) -> str:
    """Convert snake_case to camelCase."""
    components = name.split("_")
    return components[0] + "".join(x.title() for x in components[1:])
