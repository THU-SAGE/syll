"""Chat-input MCP picker + /api/v1/mcp/diag diagnostic.

The picker shows users at-a-glance whether the agent has MCP tools
available, without making them switch to the MCP tab. The diag endpoint
lets the user (or an agent) verify hot reload actually reached the
AgentLoop's tool registry.
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from syll.agent.mcp import MCPManager, command_hash
from syll.config.schema import (
    MCPConfig,
    MCPServerConfig,
)
from syll.web import auth as auth_module
from syll.web.app import create_app
from tests.test_app_factory import _admin_headers, _make_config

REPO = Path(__file__).resolve().parent.parent
APP_JS = REPO / "syll" / "web" / "static" / "app.js"
INDEX_HTML = REPO / "syll" / "web" / "static" / "index.html"


@pytest.fixture(autouse=True)
def _isolate_token(monkeypatch, tmp_path):
    monkeypatch.setattr(auth_module, "ADMIN_TOKEN_PATH", tmp_path / "admin_token")


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _make_loop_with_mcp(mcp_manager):
    from syll.agent.loop import AgentLoop
    from syll.bus.queue import MessageBus

    class _StubProvider:
        def get_default_model(self):
            return "stub"

    with patch("syll.agent.loop.ContextBuilder") as ctx_cls, \
         patch("syll.agent.loop.SessionManager") as sess_cls, \
         patch("syll.agent.loop.EventStore") as evt_cls:
        ctx_cls.return_value = SimpleNamespace(
            identity=None, skills=SimpleNamespace(), memory=SimpleNamespace()
        )
        sess_cls.return_value = SimpleNamespace()
        evt_cls.return_value = SimpleNamespace()
        return AgentLoop(
            bus=MessageBus(),
            provider=_StubProvider(),
            workspace=Path(tempfile.mkdtemp()),
            mcp_manager=mcp_manager,
        )


def _build_app(mcp_cfg: MCPConfig | None = None):
    cfg = _make_config(Path(tempfile.mkdtemp()))
    cfg.mcp = mcp_cfg or MCPConfig()
    mgr = MCPManager(
        cfg.mcp,
        workspace_path=cfg.workspace_path,
        restrict_to_workspace=False,
    )
    agent_loop = _make_loop_with_mcp(mgr)
    app = create_app(
        config=cfg, agent_loop=agent_loop,
        session_manager=SimpleNamespace(), skills_loader=SimpleNamespace(),
        memory_store=SimpleNamespace(), cron_service=None,
    )
    app.state.mcp_manager = mgr
    return app, cfg, mgr


# ── /api/v1/mcp/diag — proves hot reload reached the loop ──────────────


def test_diag_reports_empty_loop_state_when_nothing_enabled(monkeypatch):
    """Fresh start: no servers, agent's _mcp_owned set is empty.

    This is the response the user sees when they open the diag endpoint
    immediately after `syll wake` and before clicking anything. It proves
    the loop has the new `reload_mcp_tools` method (i.e., the running
    process is the new code, not a pre-MCP gateway).
    """
    monkeypatch.setattr(
        "syll.web.routes.mcp.load_config",
        lambda: __import__("syll.config.schema", fromlist=["Config"]).Config(),
    )
    app, _, _ = _build_app()
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        r0 = client.get("/api/v1/mcp/diag")
        assert r0.status_code == 401
        client.headers.update(_admin_headers(client))
        r = client.get("/api/v1/mcp/diag")
        assert r.status_code == 200
        body = r.json()
        assert body["loop_class"] == "AgentLoop"
        assert body["loop_has_reload"] is True, (
            "running process predates Phase 1c — restart `syll wake`"
        )
        assert body["loop_mcp_owned_count"] == 0
        assert body["loop_mcp_owned"] == []
        assert body["live_sessions"] == {}


@pytest.mark.timeout(60)
def test_diag_reports_live_tools_after_apply(repo_root, monkeypatch):
    """End-to-end: PUT a server with valid hash → diag reports the
    registered tool names. This is the closure check users actually want
    when debugging hot reload."""
    monkeypatch.chdir(repo_root)
    app, _, mgr = _build_app()
    monkeypatch.setattr("syll.web.routes.mcp.save_config", lambda c, p=None: None)
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        client.headers.update(_admin_headers(client))
        body = {
            "transport": "stdio",
            "stdio": {
                "command": sys.executable,
                "args": ["-m", "tests.fixtures.echo_mcp_server"],
            },
            "enabled": True,
            "enabled_tools": ["*"],
        }
        body["confirmed_command_hash"] = command_hash(MCPServerConfig.model_validate(body))
        r = client.put("/api/v1/mcp/servers/echo", json=body)
        assert r.status_code == 200, r.text

        # Now the diag should show the live state.
        d = client.get("/api/v1/mcp/diag").json()
        assert "mcp__echo__echo" in d["loop_mcp_owned"]
        assert "mcp__echo__add" in d["loop_mcp_owned"]
        assert d["loop_mcp_owned_count"] >= 2
        assert "echo" in d["live_sessions"]
        assert d["live_sessions"]["echo"]["status"] == "connected"
        assert d["live_sessions"]["echo"]["registered_count"] >= 2


# ── Chat-input picker UI surface ───────────────────────────────────────


def test_picker_html_inside_input_container():
    """The MCP picker must live INSIDE `.input-container` so it sits with
    the chat input and not in some unrelated place. Verifies the script
    src/order doesn't drift."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    # Find the input-container block (inside Chat tab).
    # The chat tab-panel's content is between its `tab-panel ... activeTab === 'chat'`
    # opening and the next `tab-panel` (the MCP panel).
    chat_panel_start = html.index('class="tab-panel" :class="{ \'active\': activeTab === \'chat\' }"')
    chat_panel_end = html.index("activeTab === 'mcp'", chat_panel_start)
    chat_panel = html[chat_panel_start:chat_panel_end]
    assert 'class="mcp-picker"' in chat_panel, (
        "MCP picker must live inside the Chat tab's input container"
    )
    assert "mcp-picker-toggle" in chat_panel
    assert "mcp-picker-pane" in chat_panel


def test_picker_helpers_present_in_app_js():
    src = APP_JS.read_text(encoding="utf-8")
    assert "mcpPickerSummary" in src
    assert "refreshMcpPicker" in src


def test_picker_summary_returns_master_count_pillclass():
    """Inspect the JS body of mcpPickerSummary to confirm it returns the
    expected shape and pillClass categories."""
    src = APP_JS.read_text(encoding="utf-8")
    idx = src.index("mcpPickerSummary()")
    body = src[idx : idx + 1500]
    # Returns the right keys for the toggle + dot.
    for key in ("master", "total", "connected", "failed", "count", "pillClass"):
        assert key in body, f"summary must expose `{key}`"
    # Pill class buckets cover the cases the toggle's CSS knows about.
    for cls in ("connected", "connecting", "failed", "off", "mixed"):
        assert f"'{cls}'" in body, f"missing pillClass bucket: {cls}"


def test_picker_css_dot_classes_present():
    html = INDEX_HTML.read_text(encoding="utf-8")
    for cls in (
        "mcp-picker",
        "mcp-picker-toggle",
        "mcp-picker-dot",
        "mcp-picker-dot--connected",
        "mcp-picker-dot--failed",
        "mcp-picker-dot--mixed",
        "mcp-picker-pane",
        "mcp-picker-row",
    ):
        assert re.search(rf"\.{re.escape(cls)}\b", html), (
            f"CSS rule for .{cls} missing"
        )


def test_picker_links_to_mcp_tab_for_full_management():
    """The picker is read-mostly; full management lives in the MCP tab.
    Verify there's a `switchTab('mcp')` link from the picker pane."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    # The chat tab-panel's content is between its `tab-panel ... activeTab === 'chat'`
    # opening and the next `tab-panel` (the MCP panel).
    chat_panel_start = html.index('class="tab-panel" :class="{ \'active\': activeTab === \'chat\' }"')
    chat_panel_end = html.index("activeTab === 'mcp'", chat_panel_start)
    chat_panel = html[chat_panel_start:chat_panel_end]
    # The "open MCP tab ↗" link.
    assert 'switchTab(\'mcp\')' in chat_panel


# ── One-click Enable / Disable on disabled rows ────────────────────────


def test_one_click_enable_button_present():
    """Disabled servers must show a primary Enable button that triggers
    the consent flow without requiring Edit→toggle→Save."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'mcpEnableServer(name)' in html
    assert 'mcpDisableServer(name)' in html
    # Button visibility is conditional on the server's enabled flag.
    assert 'x-show="!server.enabled"' in html
    assert 'x-show="server.enabled"' in html


def test_enable_handler_routes_409_to_consent_modal():
    src = APP_JS.read_text(encoding="utf-8")
    idx = src.index("async mcpEnableServer(name)")
    body = src[idx : idx + 2200]
    # 409 → set mcpConfirm with action: 'save' so confirm modal opens.
    assert "r.status === 409" in body
    assert "this.mcpConfirm" in body
    assert "action: 'save'" in body
    # Master-disabled is handled separately with a clear message.
    assert "mcp_master_disabled" in body


def test_one_click_enable_disable_preserves_metadata_fields():
    src = APP_JS.read_text(encoding="utf-8")
    helper_idx = src.index("_mcpServerToBody(src, enabled)")
    helper = src[helper_idx : helper_idx + 1200]
    assert "description: src.description || ''" in helper
    assert "tool_timeout_seconds: Number(src.tool_timeout_seconds) || 60" in helper
    enable_idx = src.index("async mcpEnableServer(name)")
    enable = src[enable_idx : enable_idx + 1200]
    disable_idx = src.index("async mcpDisableServer(name)")
    disable = src[disable_idx : disable_idx + 1200]
    assert "this._mcpServerToBody(src, true)" in enable
    assert "this._mcpServerToBody(src, false)" in disable


def test_mcp_form_carries_description_and_timeout_fields():
    src = APP_JS.read_text(encoding="utf-8")
    assert "description: cfg.description || ''" in src
    assert "description: copy.description || ''" in src
    assert "tool_timeout_seconds: cfg.tool_timeout_seconds || 60" in src
    assert "tool_timeout_seconds: copy.tool_timeout_seconds || 60" in src

    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "x-model=\"mcpForm.description\"" in html
    assert "x-model.number=\"mcpForm.tool_timeout_seconds\"" in html


def test_disable_handler_does_not_open_consent_modal():
    """Disabling never launches a subprocess → no consent needed."""
    src = APP_JS.read_text(encoding="utf-8")
    idx = src.index("async mcpDisableServer(name)")
    body = src[idx : idx + 1200]
    assert "this._mcpServerToBody(src, false)" in body
    # No consent flow / mcpConfirm in the disable path.
    assert "mcpConfirm" not in body


# ── Static asset cache-busting ─────────────────────────────────────────


def test_index_serves_cache_busted_app_js():
    """Avoid the trap where an edited app.js doesn't reach the browser
    because of stale cache. The served `index.html` must inject
    `?v=<mtime>-<size>` into known asset URLs."""
    import re
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from syll.agent.mcp import MCPManager
    from syll.config.schema import MCPConfig
    from syll.web import auth as auth_module
    from syll.web.app import create_app
    from tests.test_app_factory import _make_agent_loop, _make_config

    auth_module.ADMIN_TOKEN_PATH = Path(tempfile.mkdtemp()) / "admin_token"
    cfg = _make_config(Path(tempfile.mkdtemp()))
    cfg.mcp = MCPConfig()
    mgr = MCPManager(cfg.mcp)
    app = create_app(
        config=cfg, agent_loop=_make_agent_loop(),
        session_manager=SimpleNamespace(), skills_loader=SimpleNamespace(),
        memory_store=SimpleNamespace(), cron_service=None,
    )
    app.state.mcp_manager = mgr
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        page = client.get("/").text
        # `app.js` MUST carry a version tag.
        m = re.search(r'/static/app\.js\?v=(\d+)-(\d+)', page)
        assert m, "served index.html does not cache-bust /static/app.js"
        # Several vendored assets too — spot-check.
        for asset in ("vendor/dompurify.min.js", "vendor/alpinejs.min.js"):
            assert re.search(rf'/static/{re.escape(asset)}\?v=\d+-\d+', page), (
                f"{asset} not cache-busted"
            )
        # Plain (un-busted) `/static/app.js"` must NOT remain in the page.
        assert '"/static/app.js"' not in page
        assert '"/static/vendor/dompurify.min.js"' not in page


def test_cache_bust_tag_changes_when_file_changes(tmp_path, monkeypatch):
    """Edit app.js → reload → tag changes → browser fetches fresh."""
    import re
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from syll.agent.mcp import MCPManager
    from syll.config.schema import MCPConfig
    from syll.web import auth as auth_module
    from syll.web.app import create_app
    from tests.test_app_factory import _make_agent_loop, _make_config

    auth_module.ADMIN_TOKEN_PATH = tmp_path / "admin_token"
    cfg = _make_config(Path(tempfile.mkdtemp()))
    cfg.mcp = MCPConfig()
    mgr = MCPManager(cfg.mcp)
    app = create_app(
        config=cfg, agent_loop=_make_agent_loop(),
        session_manager=SimpleNamespace(), skills_loader=SimpleNamespace(),
        memory_store=SimpleNamespace(), cron_service=None,
    )
    app.state.mcp_manager = mgr

    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        page1 = client.get("/").text
        m1 = re.search(r'/static/app\.js\?v=(\d+-\d+)', page1)
        assert m1
        tag1 = m1.group(1)

        # Touch app.js to bump mtime → tag should change on next request.
        # Restore the real repo file timestamp so this test does not leave
        # a changed cache-bust tag in a developer's working tree.
        app_js = APP_JS
        import os
        orig_stat = app_js.stat()
        try:
            os.utime(
                app_js,
                ns=(orig_stat.st_atime_ns, orig_stat.st_mtime_ns + 1_000_000),
            )

            page2 = client.get("/").text
            m2 = re.search(r'/static/app\.js\?v=(\d+-\d+)', page2)
            assert m2
            tag2 = m2.group(1)
            assert tag1 != tag2, (
                f"cache-bust tag did not change after mtime bump: {tag1} == {tag2}"
            )
        finally:
            os.utime(app_js, ns=(orig_stat.st_atime_ns, orig_stat.st_mtime_ns))
