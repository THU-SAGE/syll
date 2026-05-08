"""Mask helpers for MCP config in HTTP responses + PUT body merge.

Why this lives in its own module: the rest of `routes/config.py` already has
a generic `_mask_sensitive` that walks dicts and matches on key NAMES like
`api_key`/`token`. MCP env/header dicts have ARBITRARY user-supplied keys
that don't match the generic regex (`Authorization`, `MY_SECRET`, etc.) — so
we mask **every** value in those specific subtrees and pair-restore them on
PUT, by exact key, against the persisted config.

The mask format mirrors `routes/config.py::_mask_value`:
  - len > 4: `...<last4>`
  - len <= 4 (truthy): `****`
  - empty / non-string: passthrough

The restore inverse:
  - if a PUT value starts with `...` AND the same key exists in old config,
    swap in the old value
  - the literal `****` is a "trust the persisted value" sentinel — also restored
  - anything else (including a real string) is taken as the user's new value
"""

from __future__ import annotations

from typing import Any

MASKED_PREFIX = "..."
MASKED_SHORT = "****"


def _mask_value(v: Any) -> Any:
    if not isinstance(v, str):
        return v
    if not v:
        return v
    if len(v) > 4:
        return f"{MASKED_PREFIX}{v[-4:]}"
    return MASKED_SHORT


def mask_mcp_server_dict(server_dict: dict) -> dict:
    """Return a deep-copy with env/headers values masked. Other fields untouched."""
    out: dict = {**server_dict}
    for outer_key, inner_key in (("stdio", "env"), ("http", "headers"), ("sse", "headers")):
        outer = out.get(outer_key)
        if not isinstance(outer, dict):
            continue
        inner = outer.get(inner_key)
        if not isinstance(inner, dict):
            continue
        masked = {k: _mask_value(v) for k, v in inner.items()}
        outer = {**outer, inner_key: masked}
        out = {**out, outer_key: outer}
    return out


def restore_masked_mcp_server(new_dict: dict, old_dict: dict | None) -> dict:
    """If the PUT body has masked env/header values, swap them back from old_dict.

    Pairs values by exact key. The dict-of-arbitrary-keys layers (env / headers)
    are NOT subject to camel/snake conversion (path-aware loader, see
    `syll/config/loader.py::_PRESERVE_DICT_KEY_PATHS`), so the keys here are
    verbatim what the user originally typed — no canonicalization needed.
    """
    if not isinstance(old_dict, dict):
        return new_dict
    out = dict(new_dict)
    for outer_key, inner_key in (("stdio", "env"), ("http", "headers"), ("sse", "headers")):
        new_outer = out.get(outer_key)
        old_outer = old_dict.get(outer_key)
        if not isinstance(new_outer, dict) or not isinstance(old_outer, dict):
            continue
        new_inner = dict(new_outer.get(inner_key) or {})
        old_inner = old_outer.get(inner_key) or {}
        for k, v in list(new_inner.items()):
            if not isinstance(v, str):
                continue
            if (v.startswith(MASKED_PREFIX) or v == MASKED_SHORT) and k in old_inner:
                new_inner[k] = old_inner[k]
        new_outer = {**new_outer, inner_key: new_inner}
        out = {**out, outer_key: new_outer}
    return out


def mask_mcp_config_dict(mcp_dict: dict) -> dict:
    """Mask every server's env/headers in a full mcp config dict (for GET)."""
    out = dict(mcp_dict)
    servers = out.get("servers")
    if isinstance(servers, dict):
        out["servers"] = {
            name: mask_mcp_server_dict(s) if isinstance(s, dict) else s
            for name, s in servers.items()
        }
    return out
