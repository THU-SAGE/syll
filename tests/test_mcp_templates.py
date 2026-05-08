"""Phase 4a: default-shipped MCP templates.

Tests:
  - Templates module exposes the expected ids.
  - Each template's `config` validates as MCPServerConfig.
  - playwright template excludes `browser_run_code_unsafe` (security).
  - playwright args include `--headless` (UX, surprising default upstream).
  - `requires_install` flag is set correctly per template.
  - GET /api/v1/mcp/templates returns the same list (public read).
  - UI surfaces a "Use template" action and the templates list.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from syll.agent.mcp import MCPManager, command_hash, command_preview
from syll.config.schema import MCPConfig, MCPServerConfig
from syll.web.app import create_app
from syll.web.routes._mcp_templates import (
    DEFAULT_MCP_TEMPLATES,
    PLAYWRIGHT_MCP_VERSION,
    STAGEHAND_DEFAULT_TOOLS,
    get_template,
    list_templates,
)
from tests.test_app_factory import _make_config

# ── Templates module ────────────────────────────────────────────────────


def test_templates_module_exposes_expected_ids():
    ids = [t["id"] for t in DEFAULT_MCP_TEMPLATES]
    assert "playwright" in ids
    assert "stagehand" in ids


def test_list_templates_returns_deep_copy():
    """Mutating the returned list must not affect the source."""
    a = list_templates()
    a[0]["title"] = "MUTATED"
    a[0]["config"]["enabled"] = True
    b = list_templates()
    assert b[0]["title"] != "MUTATED"
    assert b[0]["config"]["enabled"] is False


def test_get_template_returns_known_ids():
    assert get_template("playwright") is not None
    assert get_template("stagehand") is not None
    assert get_template("nonexistent") is None


def test_each_template_config_validates_as_mcp_server_config():
    """Templates must produce valid MCPServerConfig — they're seeded into
    the form which then PUTs them. Catch schema drift early."""
    for tpl in DEFAULT_MCP_TEMPLATES:
        cfg = MCPServerConfig.model_validate(tpl["config"])
        # Hash and preview must be computable too.
        assert command_hash(cfg).startswith("sha256:")
        assert isinstance(command_preview(cfg), str)


def test_playwright_template_excludes_browser_run_code_unsafe():
    """Playwright-mcp's `browser_run_code_unsafe` literal arbitrary-JS tool
    must not appear in our default `enabled_tools` allowlist."""
    tpl = get_template("playwright")
    assert tpl is not None
    assert "browser_run_code_unsafe" not in tpl["config"]["enabled_tools"]


def test_playwright_template_includes_headless_flag():
    """Playwright-mcp defaults to headed; our template must override."""
    tpl = get_template("playwright")
    assert tpl is not None
    args = tpl["config"]["stdio"]["args"]
    assert "--headless" in args


def test_playwright_template_uses_system_chrome_not_chrome_for_testing():
    """`--browser chromium` would force playwright-mcp to download
    chrome-for-testing (~150MB) on first run, which times out under the
    60s subprocess budget the agent has when an MCP server is the launch
    target. Default to `--browser chrome` so the system-installed Chrome
    is reused — the common case on macOS / Windows users.

    See review session: agent's first `mcp__playwright__browser_navigate`
    call returned 'Browser "chrome-for-testing" is not installed', and the
    install command itself timed out."""
    tpl = get_template("playwright")
    assert tpl is not None
    args = tpl["config"]["stdio"]["args"]
    # The flag value must be `chrome`, not `chromium`.
    assert "--browser" in args
    browser_idx = args.index("--browser")
    assert args[browser_idx + 1] == "chrome", (
        f"playwright template must default to `--browser chrome`; got "
        f"{args[browser_idx + 1]!r}. `chromium` triggers a chrome-for-testing "
        "download that doesn't fit in the agent's exec budget."
    )


def test_playwright_template_pins_version():
    """Args must include `@playwright/mcp@<version>` (no `@latest`)."""
    tpl = get_template("playwright")
    assert tpl is not None
    args = tpl["config"]["stdio"]["args"]
    pinned = f"@playwright/mcp@{PLAYWRIGHT_MCP_VERSION}"
    assert any(pinned in arg for arg in args), args
    assert all("@latest" not in str(arg) for arg in args)


def test_stagehand_template_marks_requires_install():
    tpl = get_template("stagehand")
    assert tpl is not None
    assert tpl["requires_install"] is True
    assert tpl.get("install_hint", "").startswith("syll bridge install")


def test_stagehand_template_default_tools_match_act_extract_observe():
    assert "act" in STAGEHAND_DEFAULT_TOOLS
    assert "extract" in STAGEHAND_DEFAULT_TOOLS
    assert "observe" in STAGEHAND_DEFAULT_TOOLS


def test_playwright_template_does_not_require_install():
    """Playwright works out of the box via npx — no install step needed."""
    tpl = get_template("playwright")
    assert tpl is not None
    assert tpl["requires_install"] is False


def test_template_default_enabled_is_false():
    """Templates seed forms; never auto-enable. The user must toggle and
    confirm explicitly to launch any subprocess."""
    for tpl in DEFAULT_MCP_TEMPLATES:
        assert tpl["config"]["enabled"] is False, (
            f"{tpl['id']}: templates must default enabled=False"
        )


# ── HTTP route ──────────────────────────────────────────────────────────


def _build_app():
    cfg = _make_config(Path(tempfile.mkdtemp()))
    cfg.mcp = MCPConfig()
    mgr = MCPManager(cfg.mcp)

    class _StubProvider:
        def get_default_model(self):
            return "stub"

    from syll.agent.loop import AgentLoop
    from syll.bus.queue import MessageBus

    with patch("syll.agent.loop.ContextBuilder") as ctx_cls, \
         patch("syll.agent.loop.SessionManager") as sess_cls, \
         patch("syll.agent.loop.EventStore") as evt_cls:
        ctx_cls.return_value = SimpleNamespace(
            identity=None, skills=SimpleNamespace(), memory=SimpleNamespace()
        )
        sess_cls.return_value = SimpleNamespace()
        evt_cls.return_value = SimpleNamespace()
        agent_loop = AgentLoop(
            bus=MessageBus(),
            provider=_StubProvider(),
            workspace=Path(tempfile.mkdtemp()),
            mcp_manager=mgr,
        )

    app = create_app(
        config=cfg, agent_loop=agent_loop,
        session_manager=SimpleNamespace(), skills_loader=SimpleNamespace(),
        memory_store=SimpleNamespace(), cron_service=None,
    )
    app.state.mcp_manager = mgr
    return app


def test_get_mcp_templates_route_is_public():
    """GET /api/v1/mcp/templates is a read endpoint — no admin token."""
    app = _build_app()
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        r = client.get("/api/v1/mcp/templates")
        assert r.status_code == 200
        body = r.json()
        ids = [t["id"] for t in body["templates"]]
        assert "playwright" in ids
        assert "stagehand" in ids


def test_get_mcp_templates_response_shape_matches_module():
    app = _build_app()
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        r = client.get("/api/v1/mcp/templates")
        body = r.json()
        # First-class fields the UI relies on.
        for tpl in body["templates"]:
            assert "id" in tpl
            assert "title" in tpl
            assert "description" in tpl
            assert "requires_install" in tpl
            assert "config" in tpl


# ── UI surface ─────────────────────────────────────────────────────────


def test_app_js_has_mcp_template_methods():
    app_js = (Path(__file__).resolve().parent.parent
              / "syll" / "web" / "static" / "app.js").read_text(encoding="utf-8")
    assert "mcpTemplates" in app_js
    assert "mcpStartFromTemplate" in app_js


def test_index_html_has_use_template_button_and_needs_install_pill():
    index_html = (Path(__file__).resolve().parent.parent
                  / "syll" / "web" / "static" / "index.html").read_text(encoding="utf-8")
    assert "Use template" in index_html
    assert "needs install" in index_html
    # The "Default-shipped templates" section header surfaces the templates.
    assert "Default-shipped templates" in index_html
