"""Post-splash split-screen TUI for the Syll gateway.

Layout
------
    ┌──────────────────────────────────────────────────────────┐
    │                                                          │
    │    [ SYLL · ABLE big pixel wordmark — splash typeface ]  │
    │                                                          │
    ├────────────────────────────┬─────────────────────────────┤
    │ status panel               │  live activity              │
    │   runtime                  │    HH:MM:SS INFO  …         │
    │   services                 │    › you  question…         │
    │   endpoints                │    ‹ syll …                 │
    │                            │                             │
    │ ◦ listening · clock        │                             │
    ├────────────────────────────┤                             │
    │ ask syll —                 │                             │
    │ [ input field            ] │                             │
    └────────────────────────────┴─────────────────────────────┘

Top row hosts the same pixel wordmark the welcome splash uses, so the
dashboard reads as a direct continuation of the boot identity. Below it,
left column is static status + input, right column is the live activity
stream.

Integration note: the gateway launches this with ``app.run_async()`` as one
of the awaited tasks alongside agent/channels/cron/heartbeat/uvicorn.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import AsyncIterator
from typing import Any, Awaitable, Callable

from rich.align import Align
from rich.cells import cell_len
from rich.console import Console, Group
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, Static

from syll.cli.banner import _build_boot_panel, build_syll_able_text

# Colour map for log levels — matches configure_warm_logging().
LOG_COLORS = {
    "INFO": "#7ec8a9",
    "SUCCESS": "#7ec8a9",
    "WARNING": "#e6a96b",
    "ERROR": "#d17f7c",
    "CRITICAL": "#d17f7c",
    "DEBUG": "#8a7d73",
}

# Soft pills behind uppercase level tags — stays inside the warm terminal palette.
LOG_LEVEL_BADGE_BG = {
    "INFO": "#152218",
    "SUCCESS": "#152218",
    "WARNING": "#2c2318",
    "ERROR": "#2c1818",
    "CRITICAL": "#321616",
    "DEBUG": "#161c22",
}

_LOG_LEVEL_KEYS = frozenset(LOG_COLORS.keys())
TOKEN_FLUSH_CHARS = 80
TOKEN_FLUSH_INTERVAL_SECONDS = 0.25
ACTIVITY_FALLBACK_WIDTH = 80
ACTIVITY_MIN_MEASURED_WIDTH = 20
ACTIVITY_FALLBACK_HEIGHT = 12
CHAT_LABEL_WIDTH = 10
CHAT_BODY_PREFIX = " " * CHAT_LABEL_WIDTH
USER_LABEL_STYLE = "bold #82aaff"
SYLL_LABEL_STYLE = "bold #f2b56b"
SYSTEM_LABEL_STYLE = "dim #8a7d73"
USER_BODY_STYLE = "#f4dcc1"
CHAT_BODY_STYLE = "#ecebe7"
CHAT_META_STYLE = "italic #8a7d73"
CHAT_CONTINUATION_STYLE = "#68454b"
SYSTEM_BODY_STYLE = "#a58a7a"

AskFn = Callable[[str], Awaitable[str]]
AskEventFn = Callable[[str], AsyncIterator[dict[str, Any]]]
ClockFn = Callable[[], float]
ActivityValue = str | Text


def _format_elapsed(seconds: float) -> str:
    if seconds < 0.1:
        return "<0.1s"
    return f"{seconds:.1f}s"


def _truncate_text(text: str, limit: int = 120) -> str:
    clean = " ".join(str(text).split())
    if len(clean) <= limit:
        return clean
    return clean[: max(0, limit - 1)].rstrip() + "…"


def _format_tool_args(arguments: Any, limit: int = 90) -> str:
    try:
        rendered = json.dumps(arguments or {}, ensure_ascii=False)
    except TypeError:
        rendered = str(arguments)
    return _truncate_text(rendered, limit)


def _format_tool_result(event: dict[str, Any], elapsed: float, limit: int = 120) -> str:
    name = str(event.get("name") or "tool")
    content = _truncate_text(str(event.get("content") or ""), limit)
    media = event.get("media") or []
    media_note = f" [{len(media)} media]" if media else ""
    return f"{name} done in {_format_elapsed(elapsed)}: {content}{media_note}"


def _format_event_line(event: dict[str, Any], elapsed: float) -> str:
    kind = event.get("type")
    if kind == "status":
        content = str(event.get("content") or "working")
        if content == "thinking":
            content = "thinking..."
        return f"{content} {_format_elapsed(elapsed)}"
    if kind == "tool_call":
        name = str(event.get("name") or "tool")
        args = _format_tool_args(event.get("arguments"))
        return f"calling {name}({args})"
    if kind == "done":
        content = _truncate_text(str(event.get("content") or ""), 160)
        if content:
            return f"done in {_format_elapsed(elapsed)}: {content}"
        return f"done in {_format_elapsed(elapsed)}"
    if kind == "error":
        return f"error {_truncate_text(str(event.get('content') or 'unknown error'), 160)}"
    return f"{kind or 'event'} {_format_elapsed(elapsed)}"


def _assistant_message_lines(reply: str) -> list[tuple[str, str]]:
    """Convert common Markdown-ish assistant text into calm terminal rows."""
    rows: list[tuple[str, str]] = []
    in_code = False
    for raw_line in reply.strip().splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            rows.append(("code", line))
            continue
        if not stripped:
            rows.append(("blank", ""))
            continue
        if stripped.startswith(("- ", "* ")):
            rows.append(("bullet", stripped[2:].strip()))
            continue
        rows.append(("text", stripped))
    return rows or [("text", "(empty response)")]


class _ActivitySink:
    """Small compatibility wrapper for the old RichLog write/clear API."""

    def __init__(self, app: "DashboardApp"):
        self._app = app

    def write(self, value: ActivityValue) -> None:
        self._app._write_activity(value)

    def clear(self) -> None:
        self._app._clear_activity()


class DashboardApp(App):
    """Split-screen TUI that runs for the lifetime of the gateway."""

    CSS = """
    Screen {
        background: #14100c;
    }

    #root {
        layout: vertical;
        height: 1fr;
    }

    #wordmark {
        height: auto;
        content-align: center top;
        padding: 1 2 0 2;
        color: #f4dcc1;
    }

    #wordmark-sep {
        height: 1;
        color: #68454b;
        content-align: center middle;
    }

    #split {
        layout: horizontal;
        height: 1fr;
    }

    #left-col {
        width: 50%;
        min-width: 54;
        layout: vertical;
        padding: 1 1 1 2;
    }

    #status-panel {
        height: 1fr;
        overflow-y: auto;
    }

    #ask-label {
        height: 1;
        color: #e2a57d;
        text-style: bold;
        padding: 0 0 0 1;
    }

    #ask-input {
        background: #1a1410;
        color: #f4dcc1;
        border: round #d18b75;
    }
    #ask-input:focus {
        border: round #edbe8e;
    }

    #right-col {
        width: 1fr;
        layout: vertical;
        padding: 1 2 1 1;
    }

    #activity-header {
        height: 1;
        color: #e2a57d;
        text-style: bold;
        padding: 0 0 0 1;
    }

    #activity-log {
        height: 1fr;
        background: transparent;
        border: round #68454b;
        padding: 1 2;
        color: #ecebe7;
    }
    """

    BINDINGS = [
        # Quit must win over Input's ctrl+c copy when the field has a selection
        # (Textual defaults to select-on-focus).
        Binding("ctrl+c", "quit", "quit", priority=True),
        ("ctrl+l", "clear_log", "clear"),
        ("ctrl+k", "focus_input", "focus input"),
        ("pageup", "scroll_activity_up", "scroll up"),
        ("pagedown", "scroll_activity_down", "scroll down"),
        ("end", "scroll_activity_end", "bottom"),
    ]

    def __init__(
        self,
        *,
        title: str,
        subtitle: str | None,
        sections: list,
        footer: str | None = None,
        on_ask: AskFn | None = None,
        on_ask_events: AskEventFn | None = None,
        clock: ClockFn | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._clock = clock or time.monotonic
        self._title_text = title
        self._subtitle_text = subtitle
        self._sections = sections
        self._footer_text = footer
        self._on_ask = on_ask
        self._on_ask_events = on_ask_events
        self._started_at = self._clock()
        # Log lines can arrive before the Textual app has mounted (e.g.
        # cron.start() / heartbeat.start() fire before run_async returns
        # control to us) and after it has torn down. Buffer them against
        # a lock so we never race ``call_from_thread``'s running-check.
        self._log_lock = threading.Lock()
        self._log_buffer: list[tuple[str, str, str, str]] = []
        self._log_buffer_cap = 500
        self._log_ready = False
        self._activity_lines: list[ActivityValue] = []
        self._activity_buffer_cap = 500
        self._activity_height_override: int | None = None
        self._activity_width_override: int | None = None
        self._activity_scroll_offset = 0

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            yield Static(self._render_wordmark(), id="wordmark", markup=False)
            yield Static("╴ ╶ ╴ ╶ ╴ ╶ ╴ ╶ ╴ ╶ ╴ ╶ ╴ ╶ ╴ ╶ ╴ ╶", id="wordmark-sep", markup=False)
            with Horizontal(id="split"):
                with Vertical(id="left-col"):
                    yield Static(self._render_status(), id="status-panel", markup=False)
                    yield Static("ask syll —", id="ask-label")
                    yield Input(
                        placeholder="type a question, then enter…",
                        id="ask-input",
                        select_on_focus=False,
                    )
                with Vertical(id="right-col"):
                    yield Static("activity —", id="activity-header")
                    yield Static("", id="activity-log", markup=False)

    # ------------------------------------------------------------------
    # Status rendering (static panel + live footer line)
    # ------------------------------------------------------------------

    def _uptime_str(self) -> str:
        secs = int(self._clock() - self._started_at)
        h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
        return f"{h}:{m:02d}:{s:02d}"

    def _live_line(self) -> Text:
        """Clock + breathing pulse + uptime — refreshed every second."""
        phase = int(self._clock() - self._started_at)
        pulse_char = "●" if phase % 2 == 0 else "◦"
        line = Text()
        line.append("  ")
        line.append(pulse_char + " ", style="bold #7ec8a9")
        line.append("listening", style="#c8a88a")
        line.append("   ·   ", style="#68454b")
        line.append(time.strftime("%H:%M:%S"), style="#f4dcc1")
        line.append("   ·   uptime ", style="#c8a88a")
        line.append(self._uptime_str(), style="#f4dcc1")
        return line

    def _render_status(self):
        # Title/subtitle already live in the top wordmark band — pass empty
        # strings so the panel header isn't a duplicate of the identity above.
        panel = _build_boot_panel(
            "",
            None,
            self._sections,
            self._footer_text,
            elapsed=999.0,
        )
        return Group(panel, Text(""), self._live_line())

    def _render_wordmark(self):
        """Centred SYLL·ABLE pixel wordmark + title/subtitle underneath."""
        elapsed = self._clock() - self._started_at
        lines = build_syll_able_text(elapsed)
        # Centre horizontally inside whatever width the Static gets.
        body: list = [Align.center(line) for line in lines]
        body.append(Text(""))
        body.append(Align.center(Text(self._title_text, style="bold #f4dcc1")))
        if self._subtitle_text:
            body.append(Align.center(Text(self._subtitle_text, style="italic #a58a7a")))
        return Group(*body)

    def _refresh_status(self) -> None:
        try:
            self.query_one("#status-panel", Static).update(self._render_status())
        except Exception:
            pass
        try:
            self.query_one("#wordmark", Static).update(self._render_wordmark())
        except Exception:
            pass
        self._refresh_activity_view()

    def on_resize(self, event: events.Resize) -> None:
        self._refresh_activity_view()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        self.set_interval(1.0, self._refresh_status)
        self._welcome()
        self.call_later(self._focus_input_deferred)
        # Flush any log records that arrived before the app was mounted.
        with self._log_lock:
            self._log_ready = True
            pending = list(self._log_buffer)
            self._log_buffer.clear()
        for rec in pending:
            self._write_log_line(*rec)
        self.call_later(self._refresh_activity_view)

    def _focus_input_deferred(self) -> None:
        try:
            self.query_one("#ask-input", Input).focus()
        except Exception:
            pass

    def _welcome(self) -> None:
        self._write_activity(
            self._system_line(
                "ready",
                "chat",
                "welcome back. type below to ask the ghost",
            )
        )
        self._write_activity("")

    def _chat_label(self, label: str) -> str:
        return f"{label:<{CHAT_LABEL_WIDTH}}"

    def _speaker_line(
        self,
        label: str,
        text: str,
        *,
        label_style: str,
        body_style: str = CHAT_BODY_STYLE,
    ) -> Text:
        line = Text()
        line.append(self._chat_label(label), style=label_style)
        line.append(text, style=body_style)
        return line

    def _body_line(self, text: str, *, style: str = CHAT_BODY_STYLE) -> Text:
        line = Text()
        line.append(CHAT_BODY_PREFIX, style=CHAT_CONTINUATION_STYLE)
        line.append(text, style=style)
        return line

    def _meta_line(self, text: str) -> Text:
        line = Text()
        line.append(CHAT_BODY_PREFIX, style=CHAT_META_STYLE)
        line.append(text, style=CHAT_META_STYLE)
        return line

    def _system_line(
        self,
        level: str,
        source: str,
        message: str,
        *,
        time_str: str | None = None,
    ) -> Text:
        """Render system rows — structured badges for real log levels, flat line otherwise."""
        lvl_key = level.upper()
        line = Text()
        line.append(self._chat_label("system"), style=SYSTEM_LABEL_STYLE)

        if lvl_key not in _LOG_LEVEL_KEYS:
            body = f"{level.lower()} {source} · {message}"
            line.append(body, style=SYSTEM_BODY_STYLE)
            return line

        if time_str:
            line.append(f"{time_str} ", style="dim #6d625c")

        color = LOG_COLORS[lvl_key]
        badge_bg = LOG_LEVEL_BADGE_BG.get(lvl_key, "#161c22")
        emphasis = "bold"
        if lvl_key in ("WARNING", "ERROR", "CRITICAL"):
            emphasis = "bold italic"
        line.append(level.upper(), style=f"{emphasis} {color} on {badge_bg}")

        line.append(" ", style="")
        line.append(f"{source} · ", style="italic dim #a58a7a")

        msg_style = CHAT_BODY_STYLE
        if lvl_key == "DEBUG":
            msg_style = "italic #b5aaa3"
        elif lvl_key == "WARNING":
            msg_style = "#f2dcc8"
        elif lvl_key in ("ERROR", "CRITICAL"):
            msg_style = "#f0ddd9"
        line.append(message, style=msg_style)

        return line

    def _activity_height(self) -> int:
        if self._activity_height_override is not None:
            if self._activity_height_override <= 1:
                return ACTIVITY_FALLBACK_HEIGHT
            return max(1, self._activity_height_override)
        try:
            widget = self.query_one("#activity-log", Static)
            # Account for the round border and vertical padding declared in CSS.
            if widget.size.height <= 1:
                return ACTIVITY_FALLBACK_HEIGHT
            return max(1, widget.size.height - 4)
        except Exception:
            return ACTIVITY_FALLBACK_HEIGHT

    def _activity_width(self) -> int:
        if self._activity_width_override is not None:
            if self._activity_width_override <= 1:
                return ACTIVITY_FALLBACK_WIDTH
            return self._activity_width_override
        try:
            widget = self.query_one("#activity-log", Static)
            # Account for the round border and horizontal padding declared in CSS.
            measured = widget.size.width - 6
            if measured < ACTIVITY_MIN_MEASURED_WIDTH:
                return ACTIVITY_FALLBACK_WIDTH
            return measured
        except Exception:
            return ACTIVITY_FALLBACK_WIDTH

    def _activity_line_text(self, value: ActivityValue) -> str:
        if isinstance(value, Text):
            return value.plain
        return str(value)

    def _activity_text(self, value: ActivityValue) -> Text:
        if isinstance(value, Text):
            return value
        return Text(str(value))

    def _activity_rows_for_value(self, value: ActivityValue, width: int) -> list[ActivityValue]:
        physical_lines: list[ActivityValue] = []
        text = self._activity_text(value)
        chunks = text.split("\n", allow_blank=True)
        if len(chunks) == 1 and cell_len(text.plain) <= width:
            return [value]

        for chunk in chunks:
            if not chunk.plain:
                physical_lines.append(Text(""))
                continue
            physical_lines.extend(self._wrap_activity_chunk(chunk, width))
        return physical_lines

    def _wrap_activity_chunk(self, chunk: Text, width: int) -> list[Text]:
        if cell_len(chunk.plain) <= width:
            return [chunk]
        if len(chunk.plain) <= CHAT_LABEL_WIDTH:
            console = Console(width=width, color_system=None)
            return list(chunk.wrap(console, width, overflow="fold"))

        prefix, body = chunk.divide([CHAT_LABEL_WIDTH])
        if not self._is_chat_prefix(prefix):
            console = Console(width=width, color_system=None)
            return list(chunk.wrap(console, width, overflow="fold"))

        body_width = max(1, width - CHAT_LABEL_WIDTH)
        console = Console(width=body_width, color_system=None)
        body_rows = list(body.wrap(console, body_width, overflow="fold"))
        if not body_rows:
            return [prefix]

        rows = [self._join_text(prefix, body_rows[0])]
        for body_row in body_rows[1:]:
            rows.append(self._join_text(self._continuation_prefix_for(prefix), body_row))
        return rows

    def _is_chat_prefix(self, prefix: Text) -> bool:
        plain = prefix.plain
        return len(plain) == CHAT_LABEL_WIDTH and (
            plain.strip() in {"you", "syll", "system", "error"} or plain == CHAT_BODY_PREFIX
        )

    def _continuation_prefix_for(self, prefix: Text) -> Text:
        style = CHAT_META_STYLE if prefix.plain == CHAT_BODY_PREFIX else CHAT_CONTINUATION_STYLE
        return Text(CHAT_BODY_PREFIX, style=style)

    def _join_text(self, prefix: Text, body: Text) -> Text:
        line = prefix.copy()
        line.append_text(body)
        return line

    def _activity_rendered_rows(self) -> list[ActivityValue]:
        width = self._activity_width()
        rows: list[ActivityValue] = []
        for line in self._activity_lines:
            rows.extend(self._activity_rows_for_value(line, width))
        return rows

    def _activity_visible_lines(self) -> list[ActivityValue]:
        height = self._activity_height()
        rows = self._activity_rendered_rows()
        if not rows:
            return []
        offset = min(self._activity_scroll_offset, self._max_activity_scroll_offset_for(rows, height))
        end = len(rows) - offset
        start = max(0, end - height)
        return rows[start:end]

    def _max_activity_scroll_offset_for(self, rows: list[ActivityValue], height: int) -> int:
        return max(0, len(rows) - height)

    def _max_activity_scroll_offset(self) -> int:
        return self._max_activity_scroll_offset_for(
            self._activity_rendered_rows(),
            self._activity_height(),
        )

    def _set_activity_scroll_offset(self, offset: int) -> None:
        self._activity_scroll_offset = max(0, min(offset, self._max_activity_scroll_offset()))
        self._refresh_activity_view()

    def _activity_visible_text(self) -> str:
        return "\n".join(self._activity_line_text(line) for line in self._activity_visible_lines())

    def _render_activity_view(self):
        visible = self._activity_visible_lines()
        if not visible:
            return Text("")
        return Group(*visible)

    def _refresh_activity_view(self) -> None:
        try:
            self.query_one("#activity-log", Static).update(self._render_activity_view())
        except Exception:
            pass

    def _write_activity(self, value: ActivityValue) -> None:
        self._activity_lines.append(self._activity_text(value))
        if len(self._activity_lines) > self._activity_buffer_cap:
            self._activity_lines = self._activity_lines[-self._activity_buffer_cap:]
        self._activity_scroll_offset = min(self._activity_scroll_offset, self._max_activity_scroll_offset())
        self._refresh_activity_view()

    def _clear_activity(self) -> None:
        self._activity_lines.clear()
        self._activity_scroll_offset = 0
        self._refresh_activity_view()

    def _remove_activity_indices(self, indices: list[int]) -> None:
        for index in sorted(set(indices), reverse=True):
            if 0 <= index < len(self._activity_lines):
                del self._activity_lines[index]
        self._refresh_activity_view()

    def _remove_activity_ids(self, row_ids: set[int]) -> None:
        if not row_ids:
            return
        self._activity_lines = [
            row for row in self._activity_lines
            if id(row) not in row_ids
        ]
        self._activity_scroll_offset = min(self._activity_scroll_offset, self._max_activity_scroll_offset())
        self._refresh_activity_view()

    # ------------------------------------------------------------------
    # Log injection
    # ------------------------------------------------------------------

    def _log(self) -> _ActivitySink | None:
        try:
            self.query_one("#activity-log", Static)
            return _ActivitySink(self)
        except Exception:
            return None

    def push_log_line(
        self, time_str: str, level: str, name: str, message: str
    ) -> None:
        """Thread-safe entry for loguru sinks.

        Handles the two awkward edges around the app's lifecycle:

        - **Before mount**: services like ``cron`` and ``heartbeat``
          emit INFO logs inside ``await x.start()`` which runs *before*
          ``dashboard.run_async()`` gives control to the Textual event
          loop, so ``call_from_thread`` would raise ``RuntimeError:
          App is not running``. We buffer the record instead and flush
          in ``on_mount``.
        - **After exit**: late log records during shutdown race against
          the just-stopped Textual worker thread. ``call_from_thread``
          raises the same error; we drop the line silently.
        """
        with self._log_lock:
            if not self._log_ready:
                if len(self._log_buffer) < self._log_buffer_cap:
                    self._log_buffer.append((time_str, level, name, message))
                return
        try:
            self.call_from_thread(
                self._write_log_line, time_str, level, name, message
            )
        except RuntimeError:
            # App transitioned to not-running between the ready check
            # and the thread hand-off. Nothing to do — dropping during
            # shutdown is the correct behaviour.
            pass

    def _write_log_line(
        self, time_str: str, level: str, name: str, message: str
    ) -> None:
        log = self._log()
        if log is None:
            return
        log.write(self._system_line(level, name, message, time_str=time_str))

    def _assistant_meta_line(self, text: str) -> Text:
        return self._speaker_line(
            "syll",
            text,
            label_style=SYLL_LABEL_STYLE,
            body_style=CHAT_META_STYLE,
        )

    # ------------------------------------------------------------------
    # Ask input
    # ------------------------------------------------------------------

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        log = self._log()
        if log is None:
            return

        log.write(
            self._speaker_line(
                "you",
                text,
                label_style=USER_LABEL_STYLE,
                body_style=USER_BODY_STYLE,
            )
        )

        if self._on_ask is None and self._on_ask_events is None:
            log.write(self._meta_line("(no agent bound)"))
            return

        if self._on_ask_events is None:
            log.write(self._assistant_meta_line("thinking..."))
        self.run_worker(self._dispatch_ask(text), exclusive=False, group="ask")

    async def _dispatch_ask(self, text: str) -> None:
        log = self._log()
        if log is None:
            return
        if self._on_ask_events is not None:
            await self._dispatch_ask_events(text, log)
            return

        await self._dispatch_ask_final(text, log)

    async def _dispatch_ask_final(self, text: str, log: _ActivitySink) -> None:
        try:
            reply = await self._on_ask(text)  # type: ignore[misc]
        except Exception as e:
            self._write_error(log, str(e))
            return

        self._write_reply(log, reply or "(empty response)")

    async def _dispatch_ask_events(self, text: str, log: _ActivitySink) -> None:
        start = self._clock()
        tool_starts: dict[str, float] = {}
        streamed_parts: list[str] = []
        token_buffer = ""
        response_started = False
        last_token_flush = start
        token_activity_ids: set[int] = set()

        def write_stream_preview() -> None:
            nonlocal token_activity_ids
            if token_activity_ids:
                self._remove_activity_ids(token_activity_ids)
                token_activity_ids = set()
            before_ids = {id(row) for row in self._activity_lines}
            self._write_reply(log, "".join(streamed_parts), trailing_blank=False)
            token_activity_ids.update(
                id(row) for row in self._activity_lines
                if id(row) not in before_ids
            )

        def flush_token_buffer() -> None:
            nonlocal last_token_flush, response_started, token_buffer
            if not token_buffer:
                return
            write_stream_preview()
            response_started = True
            last_token_flush = self._clock()
            token_buffer = ""

        try:
            async for event in self._on_ask_events(text):
                kind = event.get("type")
                now = self._clock()
                elapsed = now - start
                if kind == "status":
                    flush_token_buffer()
                    log.write(self._assistant_meta_line(_format_event_line(event, elapsed)))
                elif kind == "tool_call":
                    flush_token_buffer()
                    name = str(event.get("name") or "tool")
                    tool_starts[name] = now
                    log.write(self._assistant_meta_line(_format_event_line(event, elapsed)))
                elif kind == "tool_result":
                    flush_token_buffer()
                    name = str(event.get("name") or "tool")
                    tool_elapsed = now - tool_starts.pop(name, start)
                    log.write(self._assistant_meta_line(_format_tool_result(event, tool_elapsed)))
                elif kind == "token":
                    token = str(event.get("content") or "")
                    if token:
                        streamed_parts.append(token)
                        token_buffer += token
                        should_flush = (
                            not response_started
                            or len(token_buffer) >= TOKEN_FLUSH_CHARS
                            or "\n" in token_buffer
                            or now - last_token_flush >= TOKEN_FLUSH_INTERVAL_SECONDS
                        )
                        if should_flush:
                            write_stream_preview()
                            response_started = True
                            last_token_flush = now
                            token_buffer = ""
                elif kind == "done":
                    final = str(event.get("content") or "".join(streamed_parts))
                    flush_token_buffer()
                    if final and streamed_parts:
                        self._remove_activity_ids(token_activity_ids)
                        self._write_reply(log, final, trailing_blank=False)
                    elif final and not streamed_parts:
                        self._write_reply(log, final, trailing_blank=False)
                    log.write(self._meta_line(_format_event_line({"type": "done"}, elapsed)))
                    log.write("")
                    return
                elif kind == "error":
                    flush_token_buffer()
                    self._write_error(log, str(event.get("content") or "unknown error"))
                    return
        except Exception as e:
            self._write_error(log, str(e))
            return

        if streamed_parts:
            log.write(self._meta_line(_format_event_line({"type": "done"}, self._clock() - start)))
        log.write("")

    def _write_reply(self, log: _ActivitySink, reply: str, *, trailing_blank: bool = True) -> None:
        for index, (kind, content) in enumerate(_assistant_message_lines(reply)):
            if index == 0:
                line = self._speaker_line(
                    "syll",
                    "",
                    label_style=SYLL_LABEL_STYLE,
                )
            else:
                line = self._body_line("", style=CHAT_BODY_STYLE)
            if kind == "blank":
                log.write("")
                continue
            if kind == "code":
                line.append("      " + content, style="#c8a88a")
            elif kind == "bullet":
                line.append("   • ", style="#e2a57d")
                line.append(content, style=CHAT_BODY_STYLE)
            else:
                line.append(content, style=CHAT_BODY_STYLE)
            log.write(line)
        if trailing_blank:
            log.write("")

    def _write_reply_token(self, log: _ActivitySink, token: str, *, first: bool) -> None:
        if first:
            line = self._speaker_line(
                "syll",
                token,
                label_style=SYLL_LABEL_STYLE,
            )
        else:
            line = self._body_line(token)
        log.write(line)

    def _write_error(self, log: _ActivitySink, message: str) -> None:
        log.write(
            self._speaker_line(
                "error",
                message,
                label_style="bold #d17f7c",
            )
        )
        log.write("")

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_clear_log(self) -> None:
        self._clear_activity()
        self._welcome()

    def action_focus_input(self) -> None:
        self._focus_input_deferred()

    def action_scroll_activity_up(self) -> None:
        self._set_activity_scroll_offset(self._activity_scroll_offset + self._activity_height())

    def action_scroll_activity_down(self) -> None:
        self._set_activity_scroll_offset(self._activity_scroll_offset - self._activity_height())

    def action_scroll_activity_end(self) -> None:
        self._set_activity_scroll_offset(0)

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        self._set_activity_scroll_offset(self._activity_scroll_offset + 3)
        event.stop()

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        self._set_activity_scroll_offset(self._activity_scroll_offset - 3)
        event.stop()

    async def action_quit(self) -> None:
        """Graceful farewell — write a closing line, detach loguru so
        shutdown events don't chase a dead widget, pause briefly so the
        user sees the goodbye, then exit the TUI."""
        # Stop accepting new log records into the live widget first; any
        # stragglers the sink hasn't yet noticed will land in the buffer
        # and be dropped on exit rather than crashing call_from_thread.
        with self._log_lock:
            self._log_ready = False
        detach_loguru_from_dashboard()
        log = self._log()
        if log is not None:
            line = Text()
            line.append("  ╴ ", style="#68454b")
            line.append("syll is settling down… ", style="italic #a58a7a")
            line.append("see you soon", style="italic #edbe8e")
            log.write(line)
        # Let the last frame sit on-screen long enough to read.
        import asyncio as _asyncio
        await _asyncio.sleep(0.4)
        self.exit()


# ---------------------------------------------------------------------------
# Loguru sink
# ---------------------------------------------------------------------------


def attach_loguru_to_dashboard(
    app: DashboardApp, *, level: str = "INFO"
) -> None:
    """Route all loguru output into the dashboard's activity log.

    Replaces any existing sinks so log lines don't leak behind the TUI's
    alt-screen. The sink is thread-safe — records are handed off to the
    Textual app via ``call_from_thread``.
    """
    try:
        from loguru import logger
    except ImportError:
        return

    def sink(message) -> None:
        rec = message.record
        lvl = rec["level"].name
        time_str = rec["time"].strftime("%H:%M:%S")
        name = rec["name"].rsplit(".", 1)[-1]
        msg = rec["message"]
        app.push_log_line(time_str, lvl, name, msg)

    logger.remove()
    logger.add(sink, level=level, colorize=False, backtrace=False)


def detach_loguru_from_dashboard() -> None:
    """Remove the dashboard sink so shutdown-time logs don't crash into a
    widget that's about to disappear.

    Called from ``action_quit`` (before exit) and from the gateway's
    ``finally`` block (belt-and-braces). Idempotent — safe to call twice.
    Leaves loguru silent; the command's farewell line is printed via
    Rich's console after the TUI has released the terminal.
    """
    try:
        from loguru import logger
    except ImportError:
        return
    logger.remove()
