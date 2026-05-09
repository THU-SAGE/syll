"""Startup-speed regressions for ``syll wake``."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from syll.cli import commands
from syll.cli.commands import app


def _install_fake_wake_runtime(monkeypatch, tmp_path: Path):
    calls: dict[str, object] = {
        "order": [],
        "splash": [],
        "startup_sound": [],
        "provider": [],
        "dashboard": [],
        "web_app": [],
        "uvicorn_config": [],
        "uvicorn_server": [],
        "config_obj": None,
    }

    fake_splash = types.ModuleType("syll.cli.splash")

    def run_splash(auto_dismiss_seconds=None):
        calls["order"].append("splash")
        calls["splash"].append(auto_dismiss_seconds)

    fake_splash.run_splash = run_splash
    monkeypatch.setitem(sys.modules, "syll.cli.splash", fake_splash)

    fake_loader = types.ModuleType("syll.config.loader")
    config = SimpleNamespace(
        workspace_path=tmp_path,
        models=SimpleNamespace(
            chat=SimpleNamespace(
                model="deepseek/deepseek-v4-pro",
                api_key="sk-test",
                api_base="https://api.deepseek.com",
            )
        ),
        agents=SimpleNamespace(defaults=SimpleNamespace(max_tool_iterations=3)),
        tools=SimpleNamespace(
            web=SimpleNamespace(search=SimpleNamespace(api_key=None)),
            exec=SimpleNamespace(),
            restrict_to_workspace=True,
            gui=SimpleNamespace(),
        ),
        gateway=SimpleNamespace(host="127.0.0.1", port=18790),
        startup=SimpleNamespace(
            sound=SimpleNamespace(enabled=True, path=""),
        ),
        mcp=SimpleNamespace(
            enabled=False,
            servers={},
            max_tools_per_server=32,
            max_total_tools=200,
        ),
    )
    calls["config_obj"] = config

    def load_config():
        calls["order"].append("config")
        return config

    fake_loader.load_config = load_config
    fake_loader.get_config_path = lambda: tmp_path / "config.json"
    fake_loader.get_data_dir = lambda: tmp_path / "data"
    fake_loader.migrate_legacy_workspace = lambda: None
    monkeypatch.setitem(sys.modules, "syll.config.loader", fake_loader)

    fake_startup_sound = types.ModuleType("syll.cli.startup_sound")
    def play_startup_sound(sound_config):
        calls["order"].append("startup_sound")
        calls["startup_sound"].append(sound_config)

    fake_startup_sound.play_startup_sound = play_startup_sound
    monkeypatch.setitem(sys.modules, "syll.cli.startup_sound", fake_startup_sound)

    fake_provider = types.ModuleType("syll.providers.litellm_provider")

    class LiteLLMProvider:
        def __init__(self, **kwargs):
            calls["order"].append("provider")
            calls["provider"].append(kwargs)

    def preload_litellm_in_background():
        calls["order"].append("litellm_preload")

    fake_provider.LiteLLMProvider = LiteLLMProvider
    fake_provider.preload_litellm_in_background = preload_litellm_in_background
    monkeypatch.setitem(sys.modules, "syll.providers.litellm_provider", fake_provider)

    fake_agent = types.ModuleType("syll.agent.loop")

    class AgentLoop:
        def __init__(self, **kwargs):
            self.sessions = SimpleNamespace()
            self.context = SimpleNamespace(
                skills=SimpleNamespace(),
                memory=SimpleNamespace(),
                identity=SimpleNamespace(rituals_enabled=True),
                substitute=lambda prompt: prompt,
            )

        async def process_direct(self, *args, **kwargs):
            return SimpleNamespace(text="", media=[])

        async def run(self):
            await asyncio.sleep(0)

        def reload_mcp_tools(self):  # Phase 1c
            return 0

        def stop(self):
            pass

    fake_agent.AgentLoop = AgentLoop
    monkeypatch.setitem(sys.modules, "syll.agent.loop", fake_agent)

    fake_bus = types.ModuleType("syll.bus.queue")
    fake_bus.MessageBus = lambda: SimpleNamespace(publish_outbound=lambda message: None)
    monkeypatch.setitem(sys.modules, "syll.bus.queue", fake_bus)

    fake_channels = types.ModuleType("syll.channels.manager")

    class ChannelManager:
        enabled_channels: list[str] = []

        def __init__(self, config, bus):
            pass

        async def start_all(self):
            await asyncio.sleep(0)

        async def stop_all(self):
            pass

    fake_channels.ChannelManager = ChannelManager
    monkeypatch.setitem(sys.modules, "syll.channels.manager", fake_channels)

    fake_cron_service = types.ModuleType("syll.cron.service")

    class CronService:
        def __init__(self, path):
            self.on_job = None

        def status(self):
            return {"jobs": 0}

        async def start(self):
            pass

        def stop(self):
            pass

    fake_cron_service.CronService = CronService
    monkeypatch.setitem(sys.modules, "syll.cron.service", fake_cron_service)

    fake_cron_types = types.ModuleType("syll.cron.types")
    fake_cron_types.CronJob = object
    monkeypatch.setitem(sys.modules, "syll.cron.types", fake_cron_types)

    fake_heartbeat = types.ModuleType("syll.heartbeat.service")

    class HeartbeatService:
        def __init__(self, **kwargs):
            pass

        async def start(self):
            pass

        def stop(self):
            pass

    fake_heartbeat.HeartbeatService = HeartbeatService
    monkeypatch.setitem(sys.modules, "syll.heartbeat.service", fake_heartbeat)

    fake_dashboard = types.ModuleType("syll.cli.dashboard")

    class DashboardApp:
        def __init__(self, **kwargs):
            calls["dashboard"].append(kwargs)

        async def run_async(self):
            pass

    fake_dashboard.DashboardApp = DashboardApp
    fake_dashboard.attach_loguru_to_dashboard = lambda dashboard, level="INFO": None
    fake_dashboard.detach_loguru_from_dashboard = lambda: None
    monkeypatch.setitem(sys.modules, "syll.cli.dashboard", fake_dashboard)

    fake_web_app = types.ModuleType("syll.web.app")
    async def _noop_broadcast(event):  # Phase 1c stub
        return None
    def create_app(**kwargs):
        calls["order"].append("web_app")
        calls["web_app"].append(kwargs)
        return SimpleNamespace(
            state=SimpleNamespace(broadcast_ws=_noop_broadcast)
        )

    fake_web_app.create_app = create_app
    monkeypatch.setitem(sys.modules, "syll.web.app", fake_web_app)

    fake_uvicorn = types.ModuleType("uvicorn")
    def uvicorn_config(*args, **kwargs):
        calls["order"].append("uvicorn_config")
        calls["uvicorn_config"].append((args, kwargs))
        return SimpleNamespace(args=args, kwargs=kwargs)

    fake_uvicorn.Config = uvicorn_config

    class Server:
        should_exit = False

        def __init__(self, config):
            calls["order"].append("uvicorn_server")
            calls["uvicorn_server"].append(config)
            self.config = config

        async def serve(self):
            await asyncio.sleep(0)

    fake_uvicorn.Server = Server
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

    return calls


def test_wake_no_splash_skips_splash_and_writes_startup_profile_sml(
    monkeypatch,
    tmp_path,
):
    calls = _install_fake_wake_runtime(monkeypatch, tmp_path)
    output = tmp_path / "startup_profile_sml.json"
    monkeypatch.setenv("SYLL_STARTUP_PROFILE_SML", "1")
    monkeypatch.setenv("SYLL_STARTUP_PROFILE_OUTPUT_SML", str(output))

    result = CliRunner().invoke(app, ["wake", "--no-splash"])

    assert result.exit_code == 0
    assert calls["splash"] == []
    profile = json.loads(output.read_text(encoding="utf-8"))
    phase_names = {entry["name"] for entry in profile["phases"]}
    assert "splash_sml" in phase_names
    assert "imports_sml" in phase_names
    assert "config_sml" in phase_names
    assert "provider_sml" in phase_names
    assert "agent_sml" in phase_names
    assert "web_app_sml" in phase_names
    assert "cron_heartbeat_sml" in phase_names
    assert "dashboard_mount_sml" in phase_names


def test_wake_splash_timeout_forwards_auto_dismiss_seconds(monkeypatch, tmp_path):
    calls = _install_fake_wake_runtime(monkeypatch, tmp_path)

    result = CliRunner().invoke(app, ["wake", "--splash-timeout", "0.25"])

    assert result.exit_code == 0
    assert calls["splash"] == [0.25]


def test_wake_default_splash_timeout_lingers_longer(monkeypatch, tmp_path):
    calls = _install_fake_wake_runtime(monkeypatch, tmp_path)

    result = CliRunner().invoke(app, ["wake"])

    assert result.exit_code == 0
    assert calls["splash"] == [3.0]


def test_wake_zero_splash_timeout_preserves_manual_splash(monkeypatch, tmp_path):
    calls = _install_fake_wake_runtime(monkeypatch, tmp_path)

    result = CliRunner().invoke(app, ["wake", "--splash-timeout", "0"])

    assert result.exit_code == 0
    assert calls["splash"] == [0.0]


def test_wake_plays_configured_startup_sound(monkeypatch, tmp_path):
    calls = _install_fake_wake_runtime(monkeypatch, tmp_path)

    result = CliRunner().invoke(app, ["wake", "--no-splash"])

    assert result.exit_code == 0
    assert len(calls["startup_sound"]) == 1
    assert calls["startup_sound"][0].enabled is True


def test_wake_plays_startup_sound_before_splash_and_provider(monkeypatch, tmp_path):
    calls = _install_fake_wake_runtime(monkeypatch, tmp_path)

    result = CliRunner().invoke(app, ["wake"])

    assert result.exit_code == 0
    assert calls["order"].index("startup_sound") < calls["order"].index("splash")
    assert calls["order"].index("startup_sound") < calls["order"].index("provider")


def test_wake_preloads_litellm_while_splash_can_cover_import_cost(monkeypatch, tmp_path):
    calls = _install_fake_wake_runtime(monkeypatch, tmp_path)

    result = CliRunner().invoke(app, ["wake"])

    assert result.exit_code == 0
    assert calls["order"].index("litellm_preload") < calls["order"].index("splash")
    assert calls["order"].index("litellm_preload") < calls["order"].index("provider")


def test_wake_tui_only_skips_web_app_and_uvicorn(monkeypatch, tmp_path):
    calls = _install_fake_wake_runtime(monkeypatch, tmp_path)

    result = CliRunner().invoke(app, ["wake", "--no-splash", "--tui-only"])

    assert result.exit_code == 0
    assert calls["web_app"] == []
    assert calls["uvicorn_config"] == []
    assert calls["uvicorn_server"] == []


def test_wake_cli_host_port_override_gateway_config_before_web_start(monkeypatch, tmp_path):
    calls = _install_fake_wake_runtime(monkeypatch, tmp_path)

    result = CliRunner().invoke(
        app,
        ["wake", "--no-splash", "--host", "127.0.0.1", "--port", "18791"],
    )

    assert result.exit_code == 0
    config = calls["config_obj"]
    assert config.gateway.host == "127.0.0.1"
    assert config.gateway.port == 18791
    assert calls["web_app"][0]["config"] is config
    assert calls["uvicorn_config"][0][1]["host"] == "127.0.0.1"
    assert calls["uvicorn_config"][0][1]["port"] == 18791


def test_wake_without_cli_host_port_respects_loaded_gateway_config(monkeypatch, tmp_path):
    calls = _install_fake_wake_runtime(monkeypatch, tmp_path)
    config = calls["config_obj"]
    config.gateway.host = "127.0.0.2"
    config.gateway.port = 18888

    result = CliRunner().invoke(app, ["wake", "--no-splash"])

    assert result.exit_code == 0
    assert config.gateway.host == "127.0.0.2"
    assert config.gateway.port == 18888
    assert calls["uvicorn_config"][0][1]["host"] == "127.0.0.2"
    assert calls["uvicorn_config"][0][1]["port"] == 18888


def test_template_migration_sml_marker_skips_repeated_template_walk(
    monkeypatch,
    tmp_path,
):
    import syll.templates as templates

    marker = tmp_path / ".template_migration_sml.json"
    marker.write_text(
        json.dumps({"template_version_sml": templates.WORKSPACE_TEMPLATE_VERSION_SML}),
        encoding="utf-8",
    )

    def fail_copy(*args, **kwargs):
        raise AssertionError("template tree should not be walked when sml marker is fresh")

    monkeypatch.setattr(commands, "_copy_tree_missing_only", fail_copy)

    commands._migrate_workspace_templates(tmp_path)


def test_template_migration_sml_marker_written_after_missing_only_copy(
    monkeypatch,
    tmp_path,
):
    src = tmp_path / "templates"
    src.mkdir()
    (src / "IDENTITY.md").write_text("identity", encoding="utf-8")
    workspace = tmp_path / "workspace"

    import syll.templates as templates

    monkeypatch.setattr(templates, "WORKSPACE_TEMPLATE_ROOT", src)

    commands._migrate_workspace_templates(workspace)

    marker = workspace / ".template_migration_sml.json"
    assert (workspace / "IDENTITY.md").read_text(encoding="utf-8") == "identity"
    assert json.loads(marker.read_text(encoding="utf-8")) == {
        "template_version_sml": templates.WORKSPACE_TEMPLATE_VERSION_SML
    }


def test_template_migration_sml_marker_write_failure_is_non_fatal(
    monkeypatch,
    tmp_path,
):
    src = tmp_path / "templates"
    src.mkdir()
    (src / "IDENTITY.md").write_text("identity", encoding="utf-8")
    workspace = tmp_path / "workspace"

    import syll.templates as templates

    monkeypatch.setattr(templates, "WORKSPACE_TEMPLATE_ROOT", src)
    monkeypatch.setattr(
        commands,
        "_write_template_migration_marker_sml",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("read-only marker")),
    )

    commands._migrate_workspace_templates(workspace)

    assert (workspace / "IDENTITY.md").read_text(encoding="utf-8") == "identity"
