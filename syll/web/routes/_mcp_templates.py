"""Default-shipped MCP server templates.

Two templates ship by default:

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

Templates are SUGGESTIONS. The UI uses them to seed the Add-server form;
the user can edit any field before saving. The `requires_install` flag
marks rows the UI should label as "needs install" rather than "ready".

Pinned versions:
  - `@playwright/mcp@0.0.70` — Apache-2.0, current published release verified
    against microsoft/playwright-mcp releases during the May 2026 smoke pass.
  - `@browserbasehq/stagehand@3.3.0` — referenced in the (out-of-tree)
    bridge package; not pinned here directly.

Updating versions: bump the pin in the args list AND update the
description so users know what changed. Don't unpin (`@latest`) — that
would fork user environments silently and frustrate audit trails.
"""

from __future__ import annotations

PLAYWRIGHT_MCP_VERSION = "0.0.70"


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


def _build_default_templates() -> list[dict]:
    """Build the templates list; called by `list_templates()` so the
    Stagehand entry path is freshly resolved. The resolution is cheap and
    happens once per GET — keeps responses consistent with the installer's
    output even if the user moved their HOME mid-session (rare, but cheap
    correctness)."""
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
