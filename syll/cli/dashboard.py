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

import threading
import time
from typing import Awaitable, Callable

from rich.align import Align
from rich.console import Group
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, RichLog, Static

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

AskFn = Callable[[str], Awaitable[str]]


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
        ("ctrl+c", "quit", "quit"),
        ("ctrl+l", "clear_log", "clear"),
        ("ctrl+k", "focus_input", "focus input"),
    ]

    def __init__(
        self,
        *,
        title: str,
        subtitle: str | None,
        sections: list,
        footer: str | None = None,
        on_ask: AskFn | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._title_text = title
        self._subtitle_text = subtitle
        self._sections = sections
        self._footer_text = footer
        self._on_ask = on_ask
        self._started_at = time.monotonic()
        # Log lines can arrive before the Textual app has mounted (e.g.
        # cron.start() / heartbeat.start() fire before run_async returns
        # control to us) and after it has torn down. Buffer them against
        # a lock so we never race ``call_from_thread``'s running-check.
        self._log_lock = threading.Lock()
        self._log_buffer: list[tuple[str, str, str, str]] = []
        self._log_buffer_cap = 500
        self._log_ready = False

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
                    )
                with Vertical(id="right-col"):
                    yield Static("activity —", id="activity-header")
                    yield RichLog(
                        id="activity-log",
                        markup=False,
                        highlight=False,
                        wrap=True,
                    )

    # ------------------------------------------------------------------
    # Status rendering (static panel + live footer line)
    # ------------------------------------------------------------------

    def _uptime_str(self) -> str:
        secs = int(time.monotonic() - self._started_at)
        h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
        return f"{h}:{m:02d}:{s:02d}"

    def _live_line(self) -> Text:
        """Clock + breathing pulse + uptime — refreshed every second."""
        phase = int(time.monotonic() - self._started_at)
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
        elapsed = time.monotonic() - self._started_at
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

    def _focus_input_deferred(self) -> None:
        try:
            self.query_one("#ask-input", Input).focus()
        except Exception:
            pass

    def _welcome(self) -> None:
        log = self._log()
        if log is None:
            return
        intro = Text()
        intro.append("✦  ", style="bold #e2a57d")
        intro.append("welcome back. ", style="bold #f4dcc1")
        intro.append(
            "logs and chat replies stream here — type below to ask the ghost",
            style="italic #a58a7a",
        )
        log.write(intro)
        log.write("")

    # ------------------------------------------------------------------
    # Log injection
    # ------------------------------------------------------------------

    def _log(self) -> RichLog | None:
        try:
            return self.query_one("#activity-log", RichLog)
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
        color = LOG_COLORS.get(level, "#f4dcc1")
        line = Text()
        line.append(f"{time_str}  ", style="#8a7d73")
        line.append(f"{level:<7}  ", style=f"bold {color}")
        line.append(f"{name}  ", style="#c8a88a")
        line.append(message, style="#ecebe7")
        log.write(line)

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

        user_line = Text()
        user_line.append("›  you   ", style="bold #edbe8e")
        user_line.append(text, style="#ecebe7")
        log.write(user_line)

        if self._on_ask is None:
            log.write(Text("   (no agent bound)", style="dim #a58a7a"))
            return

        thinking = Text("   ◦ thinking…", style="italic #a58a7a")
        log.write(thinking)
        self.run_worker(self._dispatch_ask(text), exclusive=False, group="ask")

    async def _dispatch_ask(self, text: str) -> None:
        log = self._log()
        if log is None:
            return
        try:
            reply = await self._on_ask(text)  # type: ignore[misc]
        except Exception as e:
            err = Text()
            err.append("×  error  ", style="bold #d17f7c")
            err.append(str(e), style="#ecebe7")
            log.write(err)
            log.write("")
            return

        reply_line = Text()
        reply_line.append("‹  syll  ", style="bold #f4dcc1")
        reply_line.append(reply or "(empty response)", style="#ecebe7")
        log.write(reply_line)
        log.write("")

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_clear_log(self) -> None:
        log = self._log()
        if log is not None:
            log.clear()
            self._welcome()

    def action_focus_input(self) -> None:
        self._focus_input_deferred()

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
