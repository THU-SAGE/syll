"""Default-shipped MCP server templates.

Four templates ship by default:

  - `playwright` — Microsoft `@playwright/mcp` (Apache-2.0). `npx`-installed
    on first run; works out of the box. Default args use `--headless`,
    `--browser chrome` (system Chrome), `--isolated` (in-memory profile) and exclude the
    `browser_run_code_unsafe` tool from `enabled_tools` because re-enabling
    it gives the agent arbitrary JS execution on every page.

  - `stagehand` — Browserbase Stagehand wrapped as an MCP server. Lives in
    a separate `THU-SAGE/syll-bridges` repo (Phase 4b). The template
    captures the intended config (`node <absolute bridge_entry_point("stagehand")>`,
    `OPENAI_API_KEY` env) but the bridge itself must be installed first via
    `syll bridge install stagehand` (Phase 4b). Until then the UI shows a
    "needs install" hint and an install action.

  - `blender` — Blender MCP (`uvx blender-mcp`) for controlling a local
    Blender session through the Blender MCP add-on. It requires Blender,
    uvx, and the add-on server running inside Blender before enabling.

  - `godot` — GoPeak Godot MCP (`npx -y gopeak@<pin>`) for creating,
    running, debugging, and screenshotting local Godot 4 projects. It
    requires Node.js 18+ and a local Godot 4 executable.

Templates are SUGGESTIONS. The UI uses them to seed the Add-server form;
the user can edit any field before saving. The `requires_install` flag
marks rows the UI should label as "needs install" rather than "ready".

Pinned versions:
  - `@playwright/mcp@0.0.70` — Apache-2.0, current published release verified
    against microsoft/playwright-mcp releases during the May 2026 smoke pass.
  - `@browserbasehq/stagehand@3.3.0` — referenced in the (out-of-tree)
    bridge package; not pinned here directly.
  - `blender-mcp` — Python tool launched through `uvx`; the package is not
    pinned yet because the upstream distribution currently documents the
    unversioned `uvx blender-mcp` entrypoint. Users still get stdio command
    hash confirmation before anything launches.
  - `gopeak@2.3.6` — GoPeak Godot MCP package pin used for deterministic
    demo behavior instead of floating to npm latest.

Updating versions: bump the pin in the args list AND update the
description so users know what changed. Don't unpin (`@latest`) — that
would fork user environments silently and frustrate audit trails.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

PLAYWRIGHT_MCP_VERSION = "0.0.70"
GOPEAK_GODOT_MCP_VERSION = "2.3.6"


def _stagehand_entry_path() -> str:
    """Absolute, ~-expanded path to the installed Stagehand bridge entry JS.

    Computed at GET time (in `list_templates`) so a UI fetch sees the path
    that matches the installer's actual layout. Earlier this was a static
    `~/.syll/bridges/stagehand-mcp/...` string that (1) had the wrong name
    (mcp_bridges installs to `bridges/stagehand/`, not `bridges/stagehand-mcp/`)
    and (2) wouldn't shell-expand under `create_subprocess_exec`. See
    review-pass-7 H1.
    """
    from syll.agent.mcp_bridges import bridge_entry_point

    return str(bridge_entry_point("stagehand"))


def _default_godot_path() -> str:
    """Best-effort local Godot executable discovery for the Godot template.

    GoPeak defaults to `/Applications/Godot.app/Contents/MacOS/Godot` when
    GODOT_PATH is empty, but macOS users often run the downloaded app directly
    from `~/Downloads`. Seeding the detected path avoids a failed first enable
    while still leaving the field editable in the UI.
    """
    candidates: list[str] = []
    env_path = os.environ.get("GODOT_PATH")
    if env_path:
        candidates.append(env_path)

    for exe in ("godot", "Godot"):
        resolved = shutil.which(exe)
        if resolved:
            candidates.append(resolved)

    candidates.extend(
        [
            "/Applications/Godot.app/Contents/MacOS/Godot",
            "~/Applications/Godot.app/Contents/MacOS/Godot",
            "~/Downloads/Godot.app/Contents/MacOS/Godot",
        ]
    )

    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.is_file():
            return str(path)
    return ""

# Tool allowlist for playwright-mcp — excludes `browser_run_code_unsafe`
# (literal arbitrary-JS execution) and the `--caps=storage` cookie/local-
# storage subset which most Pet-tab users don't need by default.
PLAYWRIGHT_DEFAULT_TOOLS = [
    "browser_navigate",
    "browser_click",
    "browser_type",
    "browser_fill_form",
    "browser_select_option",
    "browser_press_key",
    "browser_hover",
    "browser_drag",
    "browser_snapshot",
    "browser_take_screenshot",
    "browser_console_messages",
    "browser_navigate_back",
    "browser_tabs",
    "browser_wait_for",
    "browser_handle_dialog",
    "browser_resize",
    "browser_evaluate",
    "browser_network_requests",
    "browser_close",
]

STAGEHAND_DEFAULT_TOOLS = [
    "act",
    "extract",
    "observe",
    "navigate",
    "screenshot",
    "close",
]

BLENDER_DEFAULT_TOOLS = ["*"]
GODOT_DEFAULT_TOOLS = ["*"]


def _build_default_templates() -> list[dict]:
    """Build the templates list; called by `list_templates()` so the
    Stagehand entry path is freshly resolved. The resolution is cheap and
    happens once per GET — keeps responses consistent with the installer's
    output even if the user moved their HOME mid-session. The Godot template
    also does a small local executable probe so common macOS app locations
    seed a working GODOT_PATH instead of a blank value."""
    return [
    {
        "id": "playwright",
        "name": "playwright",
        "title": "Microsoft Playwright MCP",
        "description": (
            f"Browser automation via @playwright/mcp@{PLAYWRIGHT_MCP_VERSION} "
            "(Apache-2.0). Uses your system Google Chrome headless with an "
            "isolated in-memory profile — works out of the box on macOS / "
            "Windows where Chrome is already installed."
        ),
        "homepage": "https://github.com/microsoft/playwright-mcp",
        "requires_install": False,
        "warning": (
            "browser_run_code_unsafe is excluded from enabled_tools by default "
            "— re-enabling it gives the agent arbitrary JS execution. If you "
            "don't have Chrome installed, edit args and switch `--browser chrome` "
            "to `--browser firefox`, or to `--browser chromium` to auto-download "
            "chrome-for-testing (~150MB)."
        ),
        "config": {
            "transport": "stdio",
            "stdio": {
                "command": "npx",
                "args": [
                    "-y",
                    f"@playwright/mcp@{PLAYWRIGHT_MCP_VERSION}",
                    "--headless",
                    # `chrome` uses the system-installed Google Chrome; works
                    # without any extra download. `chromium` would force
                    # playwright-mcp to fetch chrome-for-testing on first run
                    # (~150MB), which times out under the 60s exec budget the
                    # agent has when tunneled through a sub-agent install path.
                    "--browser",
                    "chrome",
                    "--isolated",
                ],
                "env": {},
            },
            "enabled": False,
            "enabled_tools": PLAYWRIGHT_DEFAULT_TOOLS,
            "propagate_to_subagents": False,
            "tool_timeout_seconds": 60,
            "description": "Browser automation (playwright-mcp).",
        },
    },
    {
        "id": "stagehand",
        "name": "stagehand",
        "title": "Browserbase Stagehand",
        "description": (
            "Semantic browser primitives (act / extract / observe) on top "
            "of Playwright. Requires `syll bridge install stagehand` to "
            "fetch and build the Node bridge from THU-SAGE/syll-bridges."
        ),
        "homepage": "https://github.com/browserbase/stagehand",
        "requires_install": True,
        "install_hint": "syll bridge install stagehand",
        "warning": (
            "Stagehand internally calls an LLM (OPENAI_API_KEY in env). "
            "Stdin commands run on your host."
        ),
        "config": {
            "transport": "stdio",
            "stdio": {
                "command": "node",
                "args": [_stagehand_entry_path()],
                "env": {
                    "OPENAI_API_KEY": "",
                },
            },
            "enabled": False,
            "enabled_tools": STAGEHAND_DEFAULT_TOOLS,
            "propagate_to_subagents": False,
            "tool_timeout_seconds": 60,
            "description": (
                "Stagehand bridge (act/extract/observe). Run "
                "`syll bridge install stagehand` first."
            ),
        },
    },
    {
        "id": "blender",
        "name": "blender",
        "title": "Blender MCP",
        "description": (
            "Control a local Blender session to create, edit, and render "
            "3D scenes through Blender MCP. Requires Blender, uvx, and the "
            "Blender MCP add-on running inside Blender."
        ),
        "homepage": "https://github.com/ahujasid/blender-mcp",
        "requires_install": True,
        "install_hint": (
            "Install the Blender MCP add-on in Blender, click Start Server, "
            "then enable this template."
        ),
        "warning": (
            "Blender MCP can execute Blender Python and modify local scene "
            "files. Only enable it for a trusted local Blender session."
        ),
        "config": {
            "transport": "stdio",
            "stdio": {
                "command": "uvx",
                "args": ["blender-mcp"],
                "env": {
                    "DISABLE_TELEMETRY": "true",
                },
            },
            "enabled": False,
            "enabled_tools": BLENDER_DEFAULT_TOOLS,
            "propagate_to_subagents": True,
            "tool_timeout_seconds": 120,
            "description": "Local Blender creative scene control.",
        },
    },
    {
        "id": "godot",
        "name": "godot",
        "title": "Godot MCP",
        "description": (
            f"Create, run, debug, and screenshot Godot 4 projects via "
            f"GoPeak Godot MCP {GOPEAK_GODOT_MCP_VERSION}. Best for game "
            "prototype and simulation-scene demos in a local workspace."
        ),
        "homepage": "https://github.com/HaD0Yun/Gopeak-godot-mcp",
        "requires_install": True,
        "install_hint": (
            "Install Godot 4.x and Node.js 18+. If Godot is not on PATH, "
            "set GODOT_PATH to your local Godot executable before enabling."
        ),
        "warning": (
            "Godot MCP can create, edit, run, and screenshot local Godot "
            "projects. Use a demo workspace or version-controlled project."
        ),
        "config": {
            "transport": "stdio",
            "stdio": {
                "command": "npx",
                "args": ["-y", f"gopeak@{GOPEAK_GODOT_MCP_VERSION}"],
                "env": {
                    "GODOT_PATH": _default_godot_path(),
                    "GOPEAK_TOOL_PROFILE": "compact",
                    "DISABLE_TELEMETRY": "true",
                },
            },
            "enabled": False,
            "enabled_tools": GODOT_DEFAULT_TOOLS,
            "propagate_to_subagents": False,
            "tool_timeout_seconds": 120,
            "description": "Local Godot 4 project automation (GoPeak MCP).",
        },
    },
]


# Module-level constant kept for tests that want to assert the static set
# of template ids without re-resolving paths. Call `list_templates()` at
# request time to get the freshly-resolved version.
DEFAULT_MCP_TEMPLATES: list[dict] = _build_default_templates()


def list_templates() -> list[dict]:
    """Return a freshly-built copy of the templates.

    Rebuilt per-call so the resolved Stagehand entry path tracks any future
    relocation of `~/.syll/bridges/`. Cheap (no I/O), and the deep-copy is
    free since `_build_default_templates()` already constructs new dicts.
    """
    return _build_default_templates()


def get_template(template_id: str) -> dict | None:
    for t in _build_default_templates():
        if t["id"] == template_id:
            return t
    return None
