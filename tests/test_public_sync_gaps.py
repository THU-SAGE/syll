"""Regression checks for the public-branch sync port."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import types
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import syll.agent.loop as loop_module  # noqa: E402
import syll.web.routes.ghost as ghost_routes  # noqa: E402
from syll.cli.banner import build_syll_title_block  # noqa: E402
from syll.cli.commands import app  # noqa: E402
from syll.web.app import create_app  # noqa: E402
from syll.web.streaming import process_streaming  # noqa: E402


class _StubTools:
    class exec:
        max_timeout = 30
        default_timeout = 10
        restrict_to_workspace = False

    class web:
        class search:
            api_key = None

    class gui:
        pass

    restrict_to_workspace = False


def _make_config(tmp_dir: Path):
    return SimpleNamespace(
        workspace_path=tmp_dir,
        tools=_StubTools(),
        models=SimpleNamespace(
            chat=SimpleNamespace(model="stub", api_key=None, api_base=None)
        ),
        gateway=SimpleNamespace(host="127.0.0.1", port=18790),
        agents=SimpleNamespace(defaults=SimpleNamespace(max_tool_iterations=5)),
        channels=SimpleNamespace(),
    )


def _make_agent_loop():
    return SimpleNamespace(
        sessions=SimpleNamespace(),
        context=SimpleNamespace(skills=SimpleNamespace(), memory=SimpleNamespace()),
    )


def test_pet_api_supports_both_syll_and_ghost_routes(tmp_path, monkeypatch):
    new_prefs = tmp_path / "ghost_prefs.json"
    legacy_syll = tmp_path / "legacy-syll.json"
    legacy_ghost = tmp_path / "legacy-ghost.json"
    legacy_ghost.write_text(json.dumps({"size": "L", "notifications_enabled": False}))

    monkeypatch.setattr(ghost_routes, "_PREFS_PATH", new_prefs)
    monkeypatch.setattr(ghost_routes, "_LEGACY_PREF_PATHS", (legacy_syll, legacy_ghost))

    tmp = Path(tempfile.mkdtemp())
    app_instance = create_app(
        config=_make_config(tmp),
        agent_loop=_make_agent_loop(),
        session_manager=SimpleNamespace(),
        skills_loader=SimpleNamespace(),
        memory_store=SimpleNamespace(),
        cron_service=None,
    )

    with TestClient(app_instance) as client:
        resp = client.get("/api/v1/syll/config")
        assert resp.status_code == 200
        assert resp.json()["size"] == "L"
        assert resp.json()["notifications_enabled"] is False

        update = client.put(
            "/api/v1/ghost/config",
            json={"size": "S", "state_svg_map": {"idle": "custom.svg"}},
        )
        assert update.status_code == 200
        assert update.json()["size"] == "S"

        legacy_alias = client.get("/api/v1/syll/config")
        assert legacy_alias.status_code == 200
        assert legacy_alias.json()["size"] == "S"
        assert legacy_alias.json()["state_svg_map"]["idle"] == "custom.svg"
        assert new_prefs.exists()


def test_pet_svg_upload_rejects_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(ghost_routes, "_SVG_DIR", tmp_path)

    tmp = Path(tempfile.mkdtemp())
    app_instance = create_app(
        config=_make_config(tmp),
        agent_loop=_make_agent_loop(),
        session_manager=SimpleNamespace(),
        skills_loader=SimpleNamespace(),
        memory_store=SimpleNamespace(),
        cron_service=None,
    )

    with TestClient(app_instance) as client:
        resp = client.post(
            "/api/v1/syll/svgs",
            files={"file": ("../escape.svg", BytesIO(b"<svg/>"), "image/svg+xml")},
        )

    assert resp.status_code == 200
    assert resp.json() == {"error": "Invalid filename"}
    assert list(tmp_path.iterdir()) == []


def test_desktop_cli_aliases_launch_the_same_ghost(monkeypatch):
    calls: list[int | None] = []

    fake_desktop = types.ModuleType("syll.desktop.ghost")
    fake_desktop.run_ghost = lambda port=None: calls.append(port)
    monkeypatch.setitem(sys.modules, "syll.desktop.ghost", fake_desktop)

    fake_splash = types.ModuleType("syll.cli.splash")
    fake_splash.run_splash = lambda auto_dismiss_seconds=None: None
    monkeypatch.setitem(sys.modules, "syll.cli.splash", fake_splash)

    fake_loader = types.ModuleType("syll.config.loader")
    fake_loader.load_config = lambda: SimpleNamespace(gateway=SimpleNamespace(port=8765))
    monkeypatch.setitem(sys.modules, "syll.config.loader", fake_loader)

    runner = CliRunner()

    result = runner.invoke(app, ["ghost"])
    assert result.exit_code == 0

    alias = runner.invoke(app, ["syll"])
    assert alias.exit_code == 0

    assert calls == [8765, 8765]


def test_record_command_runs_recorder_pipeline(monkeypatch, tmp_path):
    created: dict[str, object] = {}

    class FakeRecordingSession:
        def __init__(self, project_name, output_dir=None, fps=15, monitor_idx=0):
            created["session"] = {
                "project_name": project_name,
                "output_dir": output_dir,
                "fps": fps,
                "monitor_idx": monitor_idx,
            }

    fake_core = types.ModuleType("syll.recorder.core")
    fake_core.RecordingSession = FakeRecordingSession
    monkeypatch.setitem(sys.modules, "syll.recorder.core", fake_core)

    fake_ui = types.ModuleType("syll.recorder.ui")

    def _run_recorder(session, project_name):
        created["run"] = {"project_name": project_name, "session_type": type(session).__name__}

    fake_ui.run_recorder = _run_recorder
    monkeypatch.setitem(sys.modules, "syll.recorder.ui", fake_ui)

    runner = CliRunner()
    out_dir = tmp_path / "capture-out"
    result = runner.invoke(
        app,
        [
            "record",
            "demo-capture",
            "--output",
            str(out_dir),
            "--fps",
            "12",
            "--monitor",
            "1",
        ],
    )

    assert result.exit_code == 0
    assert created["session"] == {
        "project_name": "demo-capture",
        "output_dir": str(out_dir),
        "fps": 12,
        "monitor_idx": 1,
    }
    assert created["run"] == {"project_name": "demo-capture", "session_type": "FakeRecordingSession"}


def test_cli_banner_uses_shared_syll_title_block():
    title_lines = build_syll_title_block("0.2.0")

    assert title_lines[-2].strip() == "v0.2.0"
    assert title_lines[-1] == "╶  your syll in the shell  ╴"


def test_index_html_prefers_syll_storage_keys_with_nanobot_fallback():
    html = (ROOT / "syll" / "web" / "static" / "index.html").read_text(encoding="utf-8")

    assert "localStorage.getItem('syll-theme')" in html
    assert "localStorage.getItem('nanobot-theme')" in html
    assert "localStorage.setItem('syll-theme', 'dark')" in html
    assert "localStorage.setItem('syll-theme', 'light')" in html
    assert "localStorage.setItem('nanobot-theme'" not in html
    assert "localStorage.getItem('syll-syll-visible')" in html
    assert "localStorage.getItem('nanobot-syll-visible')" in html
    assert "localStorage.setItem('syll-syll-visible', this.syllVisible)" in html


def test_index_html_keeps_demo_recording_workbench_surface():
    html = (ROOT / "syll" / "web" / "static" / "index.html").read_text(encoding="utf-8")

    assert '<div class="record-panel">' in html
    assert "record-live-layout" in html
    assert "record-preview-layout" in html
    assert "openRecorderView()" in html
    assert "recorderDraftStatusLabel()" in html
    assert "return this.recorderStatus.status === 'recording' ? 'Capture Live' : 'Capture Workflow';" in html


def test_index_html_keeps_memory_workspace_surface():
    html = (ROOT / "syll" / "web" / "static" / "index.html").read_text(encoding="utf-8")

    assert '<div class="memory-hero">' in html
    assert "memory-hero-mark" in html
    assert "memoryStatLine()" in html
    assert "memory-heatmap-wrap" in html
    assert 'id="memory-cal-heatmap"' in html


def test_process_direct_injects_hints_once_and_preserves_raw_language_text(monkeypatch):
    captured: dict[str, object] = {}

    async def _fake_process_message(
        self,
        msg,
        *,
        prompt_content=None,
        language_hint_text=None,
    ):
        captured["msg_content"] = msg.content
        captured["prompt_content"] = prompt_content
        captured["language_hint_text"] = language_hint_text
        return SimpleNamespace(content="ok", media=[])

    loop = object.__new__(loop_module.AgentLoop)
    loop._process_message = types.MethodType(_fake_process_message, loop)
    monkeypatch.setattr(
        "syll.web.skill_router.inject_skill_hint",
        lambda agent_loop, text: f"{text} [hint]",
    )

    result = asyncio.run(
        loop.process_direct(
            "帮我跑一下内部审批",
            session_key="web:test",
            channel="web",
            chat_id="test",
            inject_skill_hints=True,
        )
    )

    assert result.text == "ok"
    assert captured["msg_content"] == "帮我跑一下内部审批"
    assert captured["prompt_content"] == "帮我跑一下内部审批 [hint]"
    assert captured["language_hint_text"] == "帮我跑一下内部审批"


def test_chat_route_passes_raw_text_to_process_direct(tmp_path):
    captured: dict[str, object] = {}

    class _StubAgentLoop:
        def __init__(self):
            self.sessions = SimpleNamespace()
            self.context = SimpleNamespace(
                skills=SimpleNamespace(),
                memory=SimpleNamespace(),
            )

        async def process_direct(self, content, session_key, channel, chat_id, **kwargs):
            captured["content"] = content
            captured["session_key"] = session_key
            captured["channel"] = channel
            captured["chat_id"] = chat_id
            captured["kwargs"] = kwargs
            return SimpleNamespace(text="done", media=[])

    app_instance = create_app(
        config=_make_config(tmp_path),
        agent_loop=_StubAgentLoop(),
        session_manager=SimpleNamespace(),
        skills_loader=SimpleNamespace(),
        memory_store=SimpleNamespace(),
        cron_service=None,
    )

    with TestClient(app_instance) as client:
        resp = client.post(
            "/api/v1/chat/message",
            json={"content": "帮我跑一下内部审批", "session_key": "web:test"},
        )

    assert resp.status_code == 200
    assert captured["content"] == "帮我跑一下内部审批"
    assert captured["kwargs"]["language_hint_text"] == "帮我跑一下内部审批"
    assert captured["kwargs"]["inject_skill_hints"] is True


def test_streaming_builds_with_hinted_prompt_but_saves_raw_user_text(monkeypatch):
    seen: dict[str, object] = {"messages": []}

    class _Session:
        def __init__(self):
            self.history = []

        def get_history(self):
            return []

        def add_message(self, role, content, **extras):
            seen["messages"].append((role, content, extras))

    class _Sessions:
        def __init__(self):
            self.session = _Session()

        def get_or_create(self, session_key):
            seen["session_key"] = session_key
            return self.session

        def save(self, session):
            seen["saved"] = True

    class _Context:
        def __init__(self):
            self.memory = SimpleNamespace(
                append_today=lambda summary: seen.setdefault("daily_summaries", []).append(summary)
            )

        def build_messages(self, **kwargs):
            seen["build_messages"] = kwargs
            return []

        def add_assistant_message(self, messages, content, tool_calls):
            return messages

        def add_tool_result(self, messages, tool_call_id, tool_name, result):
            return messages

    class _Provider:
        async def chat(self, messages, tools, model):
            seen["provider_messages"] = messages
            return SimpleNamespace(has_tool_calls=False, content="final")

    class _EventStore:
        def log_event(self, event):
            seen["event_text"] = event.content.text

    class _Tools:
        def get_definitions(self):
            return []

    agent_loop = SimpleNamespace(
        sessions=_Sessions(),
        context=_Context(),
        provider=_Provider(),
        tools=_Tools(),
        model="stub",
        max_iterations=1,
        event_store=_EventStore(),
    )

    monkeypatch.setattr(
        "syll.web.streaming.inject_skill_hint",
        lambda agent_loop, text: f"{text} [hint]",
    )

    async def _collect():
        return [event async for event in process_streaming(agent_loop, "你好", "web:test")]

    events = asyncio.run(_collect())

    assert events[-1]["type"] == "done"
    assert seen["build_messages"]["current_message"] == "你好 [hint]"
    assert seen["build_messages"]["language_hint_text"] == "你好"
    assert seen["messages"][0][0:2] == ("user", "你好")
    assert seen["event_text"].startswith("User: 你好\nAssistant: final")
    assert "[hint]" not in seen["daily_summaries"][-1]
    assert seen["daily_summaries"][-1].endswith("User: 你好\n")
