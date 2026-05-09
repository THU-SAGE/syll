"""Syll Recorder terminal UI — rich-based, no Qt dependency."""

from __future__ import annotations

import platform
import subprocess
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

# ── Branding ─────────────────────────────────────────────────────────

_SYLL = "\U0001F47B"
_GREEN = "#34d399"
_RED = "red"
_DIM = "dim"


def _notify(title: str, msg: str) -> None:
    """Send a macOS notification (non-blocking, best-effort)."""
    if platform.system() != "Darwin":
        return
    try:
        subprocess.Popen(
            ["osascript", "-e", f'display notification "{msg}" with title "{title}"'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def _open_folder(path: str | Path) -> None:
    p = str(path)
    s = platform.system()
    try:
        if s == "Darwin":
            subprocess.Popen(["open", p])
        elif s == "Windows":
            import os
            os.startfile(p)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", p])
    except Exception:
        pass


def _fmt_time(seconds: float) -> str:
    s = int(seconds)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


# ── Main flow ────────────────────────────────────────────────────────


def run_recorder(session, project_name: str) -> None:
    """Full recording workflow: info → countdown → record → stats → open folder."""

    home = str(Path.home())
    out_display = str(session.output_dir).replace(home, "~")

    # ── 1. Info panel ────────────────────────────────────────────────

    info = Table.grid(padding=(0, 2))
    info.add_column(style=_DIM)
    info.add_column()
    info.add_row("Project", f"[bold]{project_name}[/bold]")
    info.add_row("Output", out_display)
    info.add_row("FPS", str(session.fps))

    console.print()
    console.print(Panel(info, title=f"{_SYLL} Syll Recorder", border_style=_GREEN, width=52))

    # ── 2. Wait for Enter ────────────────────────────────────────────

    try:
        console.input(f"  [{_DIM}]Press Enter to start (Ctrl+C to cancel)[/{_DIM}] ")
    except (KeyboardInterrupt, EOFError):
        console.print(f"\n  [{_DIM}]Cancelled.[/{_DIM}]")
        return

    # ── 3. Countdown ─────────────────────────────────────────────────

    for i in (3, 2, 1):
        console.print(f"  [{_GREEN}]{i}[/{_GREEN}]", end="  ", highlight=False)
        time.sleep(1)
    console.print()

    # ── 4. Start recording ───────────────────────────────────────────

    try:
        screen = session.start()
    except Exception as e:
        console.print(f"\n  [red]Error:[/red] {e}")
        console.print(f"  [{_DIM}]Check Screen Recording & Accessibility in System Settings.[/{_DIM}]")
        return

    _notify(f"{_SYLL} Syll Recorder", f"Recording {project_name}")
    console.print(f"  [{_DIM}]Screen {screen.logical_width}\u00d7{screen.logical_height}[/{_DIM}]")
    console.print()

    # ── 5. Live recording indicator ──────────────────────────────────

    try:
        t0 = time.monotonic()
        while session.is_running():
            elapsed = _fmt_time(time.monotonic() - t0)
            console.print(
                f"\r  [bold {_RED}]\u25CF REC[/bold {_RED}]  {elapsed}"
                f"   [{_DIM}]Ctrl+C to stop[/{_DIM}]",
                end="",
            )
            time.sleep(1)
    except KeyboardInterrupt:
        pass

    session.stop()
    console.print()

    # ── 6. Stats ─────────────────────────────────────────────────────

    _notify(f"{_SYLL} Syll Recorder", f"Recording saved \u2014 {project_name}")

    st = session.stats()
    stats = Table.grid(padding=(0, 2))
    stats.add_column(style=_DIM)
    stats.add_column(style="bold")
    stats.add_row("Duration", _fmt_time(st.duration_s))
    stats.add_row("Events", f"{st.event_count} actions")
    stats.add_row("Video", _fmt_size(st.video_size))
    stats.add_row("Log", _fmt_size(st.log_size))

    console.print()
    console.print(Panel(stats, title=f"[{_GREEN}]\u2713 Recording saved[/{_GREEN}]", border_style=_GREEN, width=52))
    console.print()

    # ── 7. Open folder ───────────────────────────────────────────────

    _open_folder(session.output_dir)
    console.print(f"  [{_DIM}]Opened {out_display}[/{_DIM}]")
    console.print()
