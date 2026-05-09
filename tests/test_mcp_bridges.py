"""Phase 4b: bridge install scaffolding (CLI + HTTP + UI).

The actual `THU-SAGE/syll-bridges` repo isn't published yet. This test
file covers the wiring:

  - Allowlist enforced (unknown bridges rejected).
  - Pinned-tag enforcement (no `@latest` paths).
  - CLI surfaces a clean "not yet released" exit-code-2 for placeholders.
  - HTTP endpoint returns a job_id + 202 for known bridges; 404 for unknown.
  - HTTP endpoint requires admin token (covered transitively by AdminGuard).
  - WS broadcast handler is wired in the JS frontend.
  - Manifest read/write roundtrips.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from syll.agent.mcp import MCPManager
from syll.agent.mcp_bridges import (
    BRIDGE_ALLOWED_TAGS,
    BRIDGE_ALLOWLIST,
    BridgeInstallError,
    BridgeNotReleasedError,
    bridge_path,
    install_bridge,
    is_installed,
    list_installed,
    read_manifest,
    uninstall_bridge,
)
from syll.config.schema import MCPConfig
from syll.web import auth as auth_module
from syll.web.app import create_app
from tests.test_app_factory import _admin_headers, _make_config


@pytest.fixture(autouse=True)
def _isolate_token(monkeypatch, tmp_path):
    monkeypatch.setattr(auth_module, "ADMIN_TOKEN_PATH", tmp_path / "admin_token")


@pytest.fixture
def isolated_bridges_root(monkeypatch, tmp_path):
    """Redirect ~/.syll/bridges to a tempdir."""
    monkeypatch.setattr("syll.agent.mcp_bridges.bridges_root", lambda: tmp_path)
    monkeypatch.setattr("syll.agent.mcp_bridges.bridge_path",
                        lambda name: tmp_path / name)
    monkeypatch.setattr("syll.agent.mcp_bridges._manifest_path",
                        lambda name: tmp_path / name / ".syll-install.json")
    return tmp_path


# ── Allowlist + tag pinning ────────────────────────────────────────────


def test_bridge_allowlist_contains_stagehand():
    assert "stagehand" in BRIDGE_ALLOWLIST


def test_no_at_latest_in_allowed_tags():
    """Defense in depth: allowed tags must not contain `@latest` or HEAD."""
    for name, tags in BRIDGE_ALLOWED_TAGS.items():
        for tag in tags:
            assert "@latest" not in tag, f"{name}: {tag} contains @latest"
            assert tag.lower() != "head"
            assert tag.lower() != "main"


# ── install_bridge: allowlist enforcement ──────────────────────────────


@pytest.mark.timeout(10)
async def test_install_unknown_bridge_raises(isolated_bridges_root):
    with pytest.raises(BridgeInstallError, match="unknown bridge"):
        await install_bridge("totally-not-a-bridge")


@pytest.mark.timeout(10)
async def test_install_unreleased_bridge_raises_not_released(isolated_bridges_root):
    """Today's stagehand entry is `released: False` — install must surface
    a typed error rather than failing on git clone."""
    with pytest.raises(BridgeNotReleasedError):
        await install_bridge("stagehand")


@pytest.mark.timeout(10)
async def test_install_unknown_version_rejected(isolated_bridges_root, monkeypatch):
    """Even after release, unknown versions must be rejected."""
    # Flip the released flag so we exercise the version-check path.
    monkeypatch.setitem(BRIDGE_ALLOWLIST["stagehand"], "released", True)
    with pytest.raises(BridgeInstallError, match="not in allowed tags"):
        await install_bridge("stagehand", version="totally-fake-tag")


# ── manifest + list_installed ──────────────────────────────────────────


def test_is_installed_false_when_no_manifest(isolated_bridges_root):
    assert is_installed("stagehand") is False


def test_manifest_roundtrip(isolated_bridges_root):
    """Simulate a successful install: write a manifest + dist file at the
    package-root layout (review-pass-7 H2: dist lives under the subdirectory,
    not at the bridge clone root, so Node module resolution stays correct)."""
    from syll.agent.mcp_bridges import bridge_entry_point, bridge_package_root

    p = bridge_path("stagehand")
    pkg_root = bridge_package_root("stagehand")
    pkg_root.mkdir(parents=True, exist_ok=True)
    (pkg_root / "dist").mkdir()
    entry = bridge_entry_point("stagehand")
    entry.write_text("// stub", encoding="utf-8")
    manifest = {
        "name": "stagehand",
        "url": "https://example.git",
        "tag": "stagehand-mcp/v0.1.0",
        "sha": "deadbeef",
        "subdirectory": "stagehand-mcp",
        "entry_point": str(entry),
        "installed_at_ms": 0,
    }
    (p / ".syll-install.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert is_installed("stagehand") is True
    assert read_manifest("stagehand")["tag"] == "stagehand-mcp/v0.1.0"


def test_list_installed_includes_stagehand(isolated_bridges_root):
    rows = list_installed()
    names = [r["name"] for r in rows]
    assert "stagehand" in names
    row = next(r for r in rows if r["name"] == "stagehand")
    assert "released" in row
    assert "default_tag" in row


@pytest.mark.timeout(5)
async def test_uninstall_removes_directory(isolated_bridges_root):
    p = bridge_path("stagehand")
    p.mkdir(parents=True)
    (p / ".syll-install.json").write_text("{}", encoding="utf-8")
    removed = await uninstall_bridge("stagehand")
    assert removed is True
    assert not p.exists()


@pytest.mark.timeout(5)
async def test_uninstall_unknown_raises():
    with pytest.raises(BridgeInstallError, match="unknown bridge"):
        await uninstall_bridge("not-a-bridge")


# ── CLI ────────────────────────────────────────────────────────────────


def test_cli_bridge_list_runs():
    from syll.cli.commands import app as cli_app

    runner = CliRunner()
    r = runner.invoke(cli_app, ["bridge", "list"])
    assert r.exit_code == 0, r.output
    assert "stagehand" in r.output


def test_cli_bridge_install_unreleased_exits_code_2(isolated_bridges_root):
    """Per the CLI command contract: not-yet-released → exit code 2."""
    from syll.cli.commands import app as cli_app

    runner = CliRunner()
    r = runner.invoke(cli_app, ["bridge", "install", "stagehand"])
    assert r.exit_code == 2, r.output
    # Stderr/stdout merged in CliRunner — message visible somewhere.
    assert ("not yet released" in r.output) or ("placeholder" in r.output)


def test_cli_bridge_install_unknown_exits_code_1(isolated_bridges_root):
    from syll.cli.commands import app as cli_app

    runner = CliRunner()
    r = runner.invoke(cli_app, ["bridge", "install", "totally-fake"])
    assert r.exit_code == 1
    assert "unknown bridge" in r.output


# ── HTTP endpoints ─────────────────────────────────────────────────────


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


def test_get_bridges_route_lists_known_bridges():
    app = _build_app()
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        r = client.get("/api/v1/mcp/bridges")
        assert r.status_code == 200
        names = [b["name"] for b in r.json()["bridges"]]
        assert "stagehand" in names


def test_install_unknown_bridge_returns_404():
    app = _build_app()
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        client.headers.update(_admin_headers(client))
        r = client.post("/api/v1/mcp/bridges/totally-fake/install", json={})
        assert r.status_code == 404


def test_install_known_bridge_returns_202_with_job_id(isolated_bridges_root):
    """Bridge in allowlist → 202 + {job_id}. The background job will then
    fail with the not-yet-released error, but the route signature is
    correct — UI can subscribe to WS progress."""
    app = _build_app()
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        client.headers.update(_admin_headers(client))
        r = client.post("/api/v1/mcp/bridges/stagehand/install", json={})
        assert r.status_code == 202, r.text
        body = r.json()
        assert "job_id" in body
        assert body["status"] in ("started", "already_running")
        assert len(body["job_id"]) == 12


def test_install_route_requires_admin_token():
    app = _build_app()
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        # No admin token header — AdminGuardMiddleware should 401.
        r = client.post(
            "/api/v1/mcp/bridges/stagehand/install",
            json={},
            headers={"Origin": "http://localhost:18790"},
        )
        assert r.status_code == 401


# ── UI surface ─────────────────────────────────────────────────────────


def test_app_js_exposes_install_handlers():
    app_js = (Path(__file__).resolve().parent.parent
              / "syll" / "web" / "static" / "app.js").read_text(encoding="utf-8")
    assert "mcpInstallBridge" in app_js
    assert "_onMcpBridgeProgress" in app_js
    assert "mcpBridgeJobs" in app_js
    assert "mcpBridges" in app_js
    assert "fetch('/api/v1/mcp/bridges')" in app_js
    assert "mcpTemplateInstalled" in app_js
    assert "mcpTemplateNeedsInstall" in app_js
    # WS dispatcher branch present.
    assert "mcp_bridge_install_progress" in app_js


def test_index_html_has_install_via_web_button():
    index_html = (Path(__file__).resolve().parent.parent
                  / "syll" / "web" / "static" / "index.html").read_text(encoding="utf-8")
    assert "Install via web" in index_html
    assert "mcpInstallBridge" in index_html
    assert "installed" in index_html
    assert "mcpTemplateNeedsInstall(tpl)" in index_html
