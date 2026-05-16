"""Tests for the Textual dashboard's streaming ask UX."""

import asyncio
from types import SimpleNamespace

from rich.text import Text

from syll.cli.dashboard import (
    DashboardApp,
    _ActivitySink,
    _format_elapsed,
    _format_event_line,
    _format_tool_args,
    _format_tool_result,
)
from syll.web.streaming import process_streaming


class _FakeLog:
    def __init__(self):
        self.lines: list[str] = []
        self.values: list[object] = []

    def write(self, value):
        self.values.append(value)
        self.lines.append(str(value))


def test_activity_viewport_keeps_latest_lines_for_visible_height():
    """Activity should render only the newest lines that fit the viewport."""
    app = DashboardApp(title="test", subtitle=None, sections=[])
    app._activity_height_override = 3

    for i in range(5):
        app._write_activity(f"line {i}")

    assert app._activity_visible_text() == "line 2\nline 3\nline 4"


def test_activity_viewport_trims_wrapped_rows_not_logical_entries():
    """Multiline and wrapped entries should be clipped by visible terminal rows."""
    app = DashboardApp(title="test", subtitle=None, sections=[])
    app._activity_height_override = 3
    app._activity_width_override = 5

    app._write_activity("abcde\n1234567890")
    app._write_activity("tail")

    assert app._activity_visible_text() == "12345\n67890\ntail"


def test_activity_viewport_wraps_by_terminal_cell_width_for_cjk_text():
    """Wide CJK characters should count by terminal cells, not Python chars."""
    app = DashboardApp(title="test", subtitle=None, sections=[])
    app._activity_height_override = 2
    app._activity_width_override = 4

    app._write_activity("张掖今天")
    app._write_activity("ok")

    assert app._activity_visible_text() == "今天\nok"


def test_activity_viewport_uses_fallback_width_before_layout_is_stable():
    """A pre-layout width of one column should not render dangling characters."""
    app = DashboardApp(title="test", subtitle=None, sections=[])
    app._activity_height_override = 3
    app._activity_width_override = 1

    app._write_activity("No channels enabled")

    assert app._activity_visible_text() == "No channels enabled"


def test_activity_viewport_uses_fallback_height_before_layout_is_stable():
    """A pre-layout height of one row should not hide initialization logs."""
    app = DashboardApp(title="test", subtitle=None, sections=[])
    app._activity_height_override = 1

    app._write_activity("welcome back.")
    app._write_activity("Cron service started")
    app._write_activity("Heartbeat started")
    app._write_activity("Agent loop started")
    app._write_activity("No channels enabled")

    visible = app._activity_visible_text()
    assert "welcome back." in visible
    assert "Cron service started" in visible
    assert "Heartbeat started" in visible
    assert "Agent loop started" in visible
    assert "No channels enabled" in visible


def test_status_refresh_also_refreshes_activity_view_after_layout_stabilizes():
    """The periodic status tick should rerender activity after Textual layout settles."""
    app = DashboardApp(title="test", subtitle=None, sections=[])
    calls = {"status": 0, "activity": 0}

    def query_one(selector, widget_type):
        if selector == "#status-panel":
            calls["status"] += 1
        elif selector == "#activity-log":
            calls["activity"] += 1
        raise RuntimeError("not mounted")

    app.query_one = query_one  # type: ignore[method-assign]

    app._refresh_status()

    assert calls["status"] == 1
    assert calls["activity"] == 1


def test_activity_viewport_wraps_to_text_rows_without_markup_parsing():
    """Wrapped rows should remain Text renderables so markup-looking content is literal."""
    app = DashboardApp(title="test", subtitle=None, sections=[])
    app._activity_height_override = 4
    app._activity_width_override = 5

    app._write_activity("[red]danger[/red]")

    visible = app._activity_visible_lines()
    assert all(isinstance(row, Text) for row in visible)
    assert app._activity_visible_text() == "[red]\ndange\nr[/re\nd]"


def test_activity_viewport_expands_with_terminal_height():
    """The same buffer should show more recent history when the panel grows."""
    app = DashboardApp(title="test", subtitle=None, sections=[])
    for i in range(5):
        app._write_activity(f"line {i}")

    app._activity_height_override = 2
    assert app._activity_visible_text() == "line 3\nline 4"

    app._activity_height_override = 4
    assert app._activity_visible_text() == "line 1\nline 2\nline 3\nline 4"


def test_activity_clear_resets_buffer_and_restores_welcome():
    """Clearing the activity pane should remove old lines and restore the intro."""
    app = DashboardApp(title="test", subtitle=None, sections=[])
    app._activity_height_override = 10
    app._write_activity("old line")

    app.action_clear_log()

    visible = app._activity_visible_text()
    assert "old line" not in visible
    assert "welcome back." in visible


def test_activity_viewport_refreshes_on_resize():
    """Resize events should rerender the current activity tail."""
    app = DashboardApp(title="test", subtitle=None, sections=[])
    calls = 0

    def refresh():
        nonlocal calls
        calls += 1

    app._refresh_activity_view = refresh  # type: ignore[method-assign]

    app.on_resize(SimpleNamespace())

    assert calls == 1


def test_input_submit_lets_streaming_events_own_thinking_status():
    """Event streaming should not duplicate the initial thinking line."""
    app = DashboardApp(
        title="test",
        subtitle=None,
        sections=[],
        on_ask_events=lambda text: None,
    )
    fake_log = _FakeLog()
    app._log = lambda: fake_log  # type: ignore[method-assign]

    def run_worker(coro, **kwargs):
        coro.close()

    app.run_worker = run_worker  # type: ignore[method-assign]
    input_widget = SimpleNamespace(value="hello")
    event = SimpleNamespace(value="hello", input=input_widget)

    asyncio.run(app.on_input_submitted(event))

    rendered = "\n".join(fake_log.lines)
    assert "you       hello" in rendered
    assert "thinking" not in rendered


def test_input_submit_echo_uses_tight_user_prefix():
    """The echoed user line should not feel offset by extra prompt spaces."""
    app = DashboardApp(title="test", subtitle=None, sections=[])
    fake_log = _FakeLog()
    app._log = lambda: fake_log  # type: ignore[method-assign]

    def run_worker(coro, **kwargs):
        coro.close()

    app.run_worker = run_worker  # type: ignore[method-assign]
    input_widget = SimpleNamespace(value="hi")
    event = SimpleNamespace(value="hi", input=input_widget)

    asyncio.run(app.on_input_submitted(event))

    rendered = "\n".join(fake_log.lines)
    assert "you       hi" in rendered
    assert "›" not in rendered


def test_user_and_syll_message_prefixes_have_matching_width():
    """Chat turns should align regardless of whether the speaker is you or Syll."""
    app = DashboardApp(title="test", subtitle=None, sections=[])
    fake_log = _FakeLog()

    app._write_reply(fake_log, "hello\nthere", trailing_blank=False)

    assistant_lines = [str(value) for value in fake_log.values]
    assert assistant_lines == [
        "syll      hello",
        "          there",
    ]


def test_syll_prefix_uses_warmer_accent_color():
    """The Syll speaker label should stand apart from answer body text."""
    app = DashboardApp(title="test", subtitle=None, sections=[])
    fake_log = _FakeLog()

    app._write_reply(fake_log, "hello", trailing_blank=False)

    line = fake_log.values[0]
    assert isinstance(line, Text)
    assert line.spans[0].style == "bold #f2b56b"


def test_chat_meta_lines_are_dim_and_aligned_under_body_column():
    """Thinking and done metadata should sit under message bodies, not labels."""
    app = DashboardApp(title="test", subtitle=None, sections=[])

    line = app._meta_line("thinking... <0.1s")

    assert line.plain == "          thinking... <0.1s"
    assert line.spans[0].style == "italic #8a7d73"


def test_service_logs_render_as_quiet_system_rows():
    """Service logs show timestamp + level badge without overpowering chat."""
    app = DashboardApp(title="test", subtitle=None, sections=[])
    fake_log = _FakeLog()
    app._log = lambda: fake_log  # type: ignore[method-assign]

    app._write_log_line("23:33:33", "INFO", "service", "Cron service started with 0 jobs")

    line = fake_log.values[0]
    assert (
        str(line)
        == "system    23:33:33 INFO service · Cron service started with 0 jobs"
    )
    assert isinstance(line, Text)
    assert line.spans[0].style == "dim #8a7d73"
    assert any(
        span.style and "bold" in span.style and "7ec8a9" in span.style for span in line.spans
    ), "INFO badge should echo LOG_COLORS mint accent"

    warn_line = app._system_line("WARNING", "svc", "heads up")
    assert "WARNING" in warn_line.plain
    assert any(
        span.style and "italic" in span.style and "e6a96b" in span.style for span in warn_line.spans
    ), "warnings pick up warm amber styling"


def test_wrapped_syll_reply_preserves_prefix_color():
    """Long wrapped replies should keep the Syll label accent instead of turning white."""
    app = DashboardApp(title="test", subtitle=None, sections=[])
    app._activity_width_override = 20
    app._activity_height_override = 10

    app._write_reply(
        _ActivitySink(app),
        "I'm doing well~ it's been a long day of small check-ins.",
        trailing_blank=False,
    )

    first_line = next(line for line in app._activity_visible_lines() if line.plain.startswith("syll"))
    assert isinstance(first_line, Text)
    assert first_line.plain.startswith("syll")
    assert first_line.spans[0].style == "bold #f2b56b"


def test_wrapped_syll_reply_uses_hanging_indent_for_continuation_rows():
    """Wrapped assistant text should continue under the body, not at column zero."""
    app = DashboardApp(title="test", subtitle=None, sections=[])
    app._activity_width_override = 34
    app._activity_height_override = 6

    app._write_reply(
        _ActivitySink(app),
        "Doing fine~ it's past midnight now, and this wraps cleanly.",
        trailing_blank=False,
    )

    lines = [line.plain for line in app._activity_visible_lines()]
    assert lines[0].startswith("syll      Doing fine")
    assert any(line.startswith("          ") for line in lines[1:])
    assert not any(line.startswith("past midnight") for line in lines[1:])


def test_clean_chat_palette_separates_user_syll_and_meta_body_colors():
    """The chat view should use more than one foreground level."""
    app = DashboardApp(title="test", subtitle=None, sections=[])

    fake_log = _FakeLog()
    app._log = lambda: fake_log  # type: ignore[method-assign]

    def run_worker(coro, **kwargs):
        coro.close()

    app.run_worker = run_worker  # type: ignore[method-assign]
    input_widget = SimpleNamespace(value="hello")
    event = SimpleNamespace(value="hello", input=input_widget)

    asyncio.run(app.on_input_submitted(event))
    user_line = fake_log.values[0]
    syll_line = app._speaker_line("syll", "hello", label_style="bold #f2b56b", body_style="#ecebe7")
    meta_line = app._meta_line("done in 1.0s")

    assert isinstance(user_line, Text)
    assert user_line.spans[0].style == "bold #82aaff"
    assert user_line.spans[1].style == "#f4dcc1"
    assert syll_line.spans[0].style == "bold #f2b56b"
    assert syll_line.spans[1].style == "#ecebe7"
    assert meta_line.spans[0].style == "italic #8a7d73"


def test_streaming_thinking_status_is_rendered_as_syll_output():
    """Thinking is the assistant's activity, not a continuation of the user line."""
    app = DashboardApp(title="test", subtitle=None, sections=[])
    fake_log = _FakeLog()

    async def fake_events(text):
        yield {"type": "status", "content": "thinking"}
        yield {"type": "done", "content": "ok", "media": []}

    app._log = lambda: fake_log  # type: ignore[method-assign]
    app._on_ask_events = fake_events

    asyncio.run(app._dispatch_ask("hello"))

    assert "syll      thinking... <0.1s" in "\n".join(fake_log.lines)


def test_streaming_preview_uses_same_wrapping_as_final_reply():
    """Streaming text should not jump to a different width when finalized."""
    app = DashboardApp(title="test", subtitle=None, sections=[])
    app._activity_width_override = 34
    app._activity_height_override = 20
    seen: dict[str, str] = {}

    first = "Doing fine~ it's past midnight now, "
    second = (
        "and you've been checking in all day. "
        "This quiet back-and-forth is nice, actually."
    )

    async def fake_events(text):
        yield {"type": "token", "content": first}
        yield {"type": "token", "content": second}
        seen["preview"] = _reply_only_text(app._activity_visible_text())
        yield {"type": "done", "content": first + second, "media": []}

    app._log = lambda: _ActivitySink(app)  # type: ignore[method-assign]
    app._on_ask_events = fake_events

    asyncio.run(app._dispatch_ask("hello"))

    assert seen["preview"] == _reply_only_text(app._activity_visible_text())


def _reply_only_text(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines()
        if "done in" not in line and "thinking" not in line
    )


def test_activity_viewport_can_scroll_back_and_return_to_bottom():
    """Long chat history should be navigable instead of always clipped to the tail."""
    app = DashboardApp(title="test", subtitle=None, sections=[])
    app._activity_height_override = 3

    for i in range(8):
        app._write_activity(f"line {i}")

    assert app._activity_visible_text() == "line 5\nline 6\nline 7"

    app.action_scroll_activity_up()

    assert app._activity_visible_text() == "line 2\nline 3\nline 4"

    app.action_scroll_activity_down()

    assert app._activity_visible_text() == "line 5\nline 6\nline 7"


def test_dashboard_elapsed_format_avoids_misleading_zero_seconds():
    """Tiny completed durations should not look like impossible zero time."""
    assert _format_elapsed(0.0) == "<0.1s"
    assert _format_elapsed(0.04) == "<0.1s"
    assert _format_elapsed(0.12) == "0.1s"


def test_dashboard_formats_streaming_events_for_activity_log():
    """Tool progress should render as concise copyable text."""
    assert _format_event_line({"type": "status", "content": "thinking"}, 1.25) == (
        "thinking... 1.2s"
    )
    assert _format_tool_args({"city": "Zhangye", "units": "metric"}) == (
        '{"city": "Zhangye", "units": "metric"}'
    )
    assert _format_event_line(
        {
            "type": "tool_call",
            "name": "weather",
            "arguments": {"city": "Zhangye"},
        },
        1.5,
    ) == 'calling weather({"city": "Zhangye"})'
    assert _format_tool_result(
        {
            "name": "weather",
            "content": "晴，18C，西北风。适合户外活动。",
            "media": [{"mime": "image/png", "data": "..."}],
        },
        0.8,
    ) == "weather done in 0.8s: 晴，18C，西北风。适合户外活动。 [1 media]"


def test_dashboard_streaming_worker_uses_injected_clock_for_elapsed_times():
    """Synthetic clocks should make status/tool elapsed rendering deterministic."""
    now = 100.0

    def clock():
        return now

    async def fake_events(text):
        nonlocal now
        now = 100.03
        yield {"type": "status", "content": "thinking"}
        now = 100.20
        yield {
            "type": "tool_call",
            "name": "weather",
            "arguments": {"city": "Zhangye"},
        }
        now = 101.00
        yield {
            "type": "tool_result",
            "name": "weather",
            "content": "晴，18C",
            "media": [],
        }
        now = 101.40
        yield {"type": "done", "content": "张掖今天晴。", "media": []}

    app = DashboardApp(
        title="test",
        subtitle=None,
        sections=[],
        on_ask_events=fake_events,
        clock=clock,
    )
    fake_log = _FakeLog()
    app._log = lambda: fake_log  # type: ignore[method-assign]

    asyncio.run(app._dispatch_ask("查天气"))

    rendered = "\n".join(fake_log.lines)
    assert "thinking... <0.1s" in rendered
    assert "weather done in 0.8s" in rendered
    assert "done in 1.4s" in rendered


def test_dashboard_streaming_worker_flushes_first_token_before_done():
    """The first answer token should appear immediately, not only at done."""
    fake_log = _FakeLog()

    async def fake_events(text):
        yield {"type": "token", "content": "张"}
        assert "syll      张" in "\n".join(fake_log.lines)
        yield {"type": "done", "content": "张掖", "media": []}

    app = DashboardApp(
        title="test",
        subtitle=None,
        sections=[],
        on_ask_events=fake_events,
    )
    app._log = lambda: fake_log  # type: ignore[method-assign]

    asyncio.run(app._dispatch_ask("查天气"))

    rendered = "\n".join(fake_log.lines)
    assert "syll      张" in rendered
    assert "×  error" not in rendered


def test_dashboard_streaming_worker_flushes_buffered_tokens_before_tool_progress():
    """Buffered answer text should not appear after later tool progress lines."""

    async def fake_events(text):
        yield {"type": "token", "content": "hello"}
        yield {"type": "token", "content": " world"}
        yield {
            "type": "tool_call",
            "name": "weather",
            "arguments": {"city": "Zhangye"},
        }
        yield {"type": "done", "content": "hello world", "media": []}

    app = DashboardApp(
        title="test",
        subtitle=None,
        sections=[],
        on_ask_events=fake_events,
    )
    fake_log = _FakeLog()
    app._log = lambda: fake_log  # type: ignore[method-assign]

    asyncio.run(app._dispatch_ask("查天气"))

    rendered = "\n".join(fake_log.lines)
    assert rendered.index(" world") < rendered.index("calling weather")


def test_dashboard_streaming_done_does_not_repeat_streamed_answer_summary():
    """Final done status should not duplicate content already streamed above."""

    async def fake_events(text):
        yield {"type": "token", "content": "张掖今天晴。"}
        yield {"type": "done", "content": "张掖今天晴。", "media": []}

    app = DashboardApp(
        title="test",
        subtitle=None,
        sections=[],
        on_ask_events=fake_events,
    )
    app._log = lambda: _ActivitySink(app)  # type: ignore[method-assign]

    asyncio.run(app._dispatch_ask("查天气"))

    rendered = app._activity_visible_text()
    assert rendered.count("张掖今天晴。") == 1
    assert "done in" in rendered


def test_dashboard_streaming_done_only_content_is_not_repeated():
    """A provider that yields only done content should still render it once."""

    async def fake_events(text):
        yield {"type": "done", "content": "最终回答", "media": []}

    app = DashboardApp(
        title="test",
        subtitle=None,
        sections=[],
        on_ask_events=fake_events,
    )
    app._log = lambda: _ActivitySink(app)  # type: ignore[method-assign]

    asyncio.run(app._dispatch_ask("hello"))

    rendered = app._activity_visible_text()
    assert rendered.count("最终回答") == 1
    assert "done in" in rendered


def test_dashboard_streaming_replaces_interleaved_token_rows_with_final_answer():
    """Final formatted answer should replace all temporary token rows."""

    async def fake_events(text):
        yield {"type": "token", "content": "前半句"}
        yield {
            "type": "tool_call",
            "name": "lookup",
            "arguments": {"q": "x"},
        }
        yield {"type": "tool_result", "name": "lookup", "content": "ok", "media": []}
        yield {"type": "token", "content": "后半句"}
        yield {"type": "done", "content": "前半句后半句", "media": []}

    app = DashboardApp(
        title="test",
        subtitle=None,
        sections=[],
        on_ask_events=fake_events,
    )
    app._log = lambda: _ActivitySink(app)  # type: ignore[method-assign]

    asyncio.run(app._dispatch_ask("hello"))

    rendered = app._activity_visible_text()
    assert "calling lookup" in rendered
    assert "lookup done" in rendered
    assert rendered.count("前半句") == 1
    assert rendered.count("后半句") == 1
    assert "syll      前半句后半句" in rendered


def test_dashboard_streaming_renders_final_answer_when_activity_buffer_is_full():
    """A full activity buffer should not prevent final message-card rendering."""

    async def fake_events(text):
        yield {"type": "token", "content": "```\n/raw\n```"}
        yield {"type": "done", "content": "```\n/final\n```", "media": []}

    app = DashboardApp(
        title="test",
        subtitle=None,
        sections=[],
        on_ask_events=fake_events,
    )
    app._activity_height_override = 10
    app._activity_buffer_cap = 3
    app._write_activity("old 1")
    app._write_activity("old 2")
    app._write_activity("old 3")
    app._log = lambda: _ActivitySink(app)  # type: ignore[method-assign]

    asyncio.run(app._dispatch_ask("hello"))

    rendered = app._activity_visible_text()
    assert "```" not in rendered
    assert "      /final" in rendered
    assert "/raw" not in rendered
    assert "done in" in rendered


def test_dashboard_reply_formats_markdown_fences_for_tui_readability():
    """Assistant markdown should render as clean terminal text, not raw fences."""
    app = DashboardApp(title="test", subtitle=None, sections=[])
    fake_log = _FakeLog()

    app._write_reply(
        fake_log,
        "记忆路径：\n\n```\n/Users/bytedance/.syll/workspace/memory\n```\n\n- MEMORY.md\n- 2026-05-05.md",
        trailing_blank=False,
    )

    rendered = "\n".join(fake_log.lines)
    assert "```" not in rendered
    assert "syll      记忆路径：" in rendered
    assert "                /Users/bytedance/.syll/workspace/memory" in rendered
    assert "             • MEMORY.md" in rendered
    assert "             • 2026-05-05.md" in rendered


def test_dashboard_streaming_worker_writes_intermediate_lines():
    """The TUI should show progress before final completion."""

    async def fake_events(text):
        yield {"type": "status", "content": "thinking"}
        yield {
            "type": "tool_call",
            "name": "weather",
            "arguments": {"city": "Zhangye"},
        }
        yield {
            "type": "tool_result",
            "name": "weather",
            "content": "晴，18C",
            "media": [],
        }
        yield {"type": "token", "content": "张掖"}
        yield {"type": "token", "content": "今天晴。"}
        yield {"type": "done", "content": "张掖今天晴。", "media": []}

    app = DashboardApp(
        title="test",
        subtitle=None,
        sections=[],
        on_ask_events=fake_events,
    )
    app._log = lambda: _ActivitySink(app)  # type: ignore[method-assign]

    asyncio.run(app._dispatch_ask("查天气"))

    rendered = app._activity_visible_text()
    assert "thinking" in rendered
    assert "calling weather" in rendered
    assert "weather done" in rendered
    assert "张掖今天晴。" in rendered
    assert "done in" in rendered


def test_process_streaming_can_run_as_cli_channel(monkeypatch):
    """Shared streaming generator should no longer be hard-coded to web metadata."""
    seen: dict[str, object] = {}

    class _Session:
        def get_history(self):
            return []

        def add_message(self, role, content, **extras):
            pass

    class _Sessions:
        def get_or_create(self, session_key):
            seen["session_key"] = session_key
            return _Session()

        def save(self, session):
            pass

    class _Context:
        memory = SimpleNamespace(append_today=lambda summary: None)

        def build_messages(self, **kwargs):
            seen["build_messages"] = kwargs
            return []

    class _Provider:
        async def chat_stream(self, messages, tools, model):
            yield {"type": "token", "content": "ok"}
            yield {"type": "done"}

    class _Tools:
        def get_definitions(self):
            return []

    class _EventStore:
        def log_event(self, event):
            seen["event_source"] = event.source

    monkeypatch.setattr("syll.web.streaming.inject_skill_hint", lambda agent_loop, text: text)
    agent_loop = SimpleNamespace(
        sessions=_Sessions(),
        context=_Context(),
        provider=_Provider(),
        tools=_Tools(),
        model="stub",
        max_iterations=1,
        event_store=_EventStore(),
    )

    events = asyncio.run(
        _collect_events(
            process_streaming(
                agent_loop,
                "hello",
                "cli:dashboard",
                channel="cli",
                chat_id="dashboard",
            )
        )
    )

    assert events[-1]["type"] == "done"
    assert seen["session_key"] == "cli:dashboard"
    assert seen["build_messages"]["channel"] == "cli"
    assert seen["build_messages"]["chat_id"] == "dashboard"
    assert seen["event_source"].platform == "cli"
    assert seen["event_source"].chat_id == "dashboard"


def test_process_streaming_reports_followup_thinking_after_tool_result(monkeypatch):
    """After tools finish, the UI should show that the model is composing again."""
    monkeypatch.setattr("syll.web.streaming.inject_skill_hint", lambda agent_loop, text: text)

    class _Session:
        def get_history(self):
            return []

        def add_message(self, role, content, **extras):
            pass

    class _Sessions:
        def get_or_create(self, session_key):
            return _Session()

        def save(self, session):
            pass

    class _Context:
        memory = SimpleNamespace(append_today=lambda summary: None)

        def build_messages(self, **kwargs):
            return []

        def add_assistant_message(self, messages, content, tool_calls, **kwargs):
            messages.append({"role": "assistant", "content": content or "", "tool_calls": tool_calls})
            return messages

        def add_tool_result(self, messages, tool_call_id, name, result):
            messages.append({"role": "tool", "content": str(result), "tool_call_id": tool_call_id})
            return messages

    class _Provider:
        def __init__(self):
            self.calls = 0

        async def chat_stream(self, messages, tools, model):
            self.calls += 1
            if self.calls == 1:
                yield {
                    "type": "tool_calls",
                    "calls": [
                        {
                            "id": "call_weather",
                            "name": "weather",
                            "arguments": {"city": "Zhangye"},
                        }
                    ],
                }
                yield {"type": "done"}
                return

            yield {"type": "token", "content": "张掖今天晴。"}
            yield {"type": "done"}

    class _Tools:
        def get_definitions(self):
            return [{"name": "weather"}]

        async def execute(self, name, arguments):
            return "晴，18C"

    class _EventStore:
        def log_event(self, event):
            pass

    agent_loop = SimpleNamespace(
        sessions=_Sessions(),
        context=_Context(),
        provider=_Provider(),
        tools=_Tools(),
        model="stub",
        max_iterations=2,
        event_store=_EventStore(),
    )

    events = asyncio.run(
        _collect_events(
            process_streaming(
                agent_loop,
                "查天气",
                "cli:dashboard",
                channel="cli",
                chat_id="dashboard",
            )
        )
    )

    followup_status_index = next(
        i
        for i, event in enumerate(events)
        if event["type"] == "status" and event["content"] == "thinking with tool results"
    )
    tool_result_index = next(i for i, event in enumerate(events) if event["type"] == "tool_result")
    token_index = next(i for i, event in enumerate(events) if event["type"] == "token")

    assert tool_result_index < followup_status_index < token_index


async def _collect_events(generator):
    return [event async for event in generator]
