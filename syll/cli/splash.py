"""Textual TUI splash screen for Syll."""

import math
import random
import time

from rich.text import Text
from textual.app import App, ComposeResult
from textual.reactive import reactive
from textual.widget import Widget

from syll import __version__
from syll.cli.banner import (
    CHARSET_STANDARD,
    GRADIENT_ABLE,
    GRADIENT_ACCENT,
    GRADIENT_COLD,
    SEP_WIDTH,
    SYLL_ABLE_WORDMARK,
    SYLL_WIDTH,
    WORDMARK_LIT,
    render_ghost_ascii,
)

# Characters used while a wordmark column is still "booting" — quick cycle
# through block glyphs before the real character lands.
SCRAMBLE_GLYPHS = "█▓▒░▚▞▛▜▟▙▀▄▐▌"

# Tagline shown underneath the wordmark.
TAGLINE = "╶  your syll in the shell  ╴"
VERSION_LINE = f"v{__version__}"
HINT_LINE = "press any key to continue"

# Timings (seconds). Tuned so the "press any key" hint appears within
# ~3s — the splash now blocks until the user dismisses it, but we still
# want the call to action visible quickly.
GHOST_LINE_STAGGER = 0.03
GHOST_LINE_FADE = 0.24
WORDMARK_DELAY = 0.35           # after splash start
WORDMARK_COL_STAGGER = 0.020    # between columns (69 cols ≈ 1.38s)
WORDMARK_SCRAMBLE = 0.12        # scramble duration per column
TAGLINE_DELAY_AFTER_WORDMARK = 0.18
SHIMMER_PERIOD = 4.2            # seconds between shimmer sweeps


def _blend_hex(color: str, factor: float) -> str:
    """Scale a "#rrggbb" by ``factor`` (0..1+) with safe clamping."""
    r = int(int(color[1:3], 16) * factor)
    g = int(int(color[3:5], 16) * factor)
    b = int(int(color[5:7], 16) * factor)
    r = max(0, min(255, r))
    g = max(0, min(255, g))
    b = max(0, min(255, b))
    return f"#{r:02x}{g:02x}{b:02x}"


class GhostAsciiWidget(Widget):
    """Renders animated ghost ASCII + cinematic Syll wordmark reveal."""

    DEFAULT_CSS = """
    GhostAsciiWidget {
        width: 1fr;
        height: 1fr;
        content-align: center middle;
    }
    """

    tick = reactive(0.0)
    mouse_x = reactive(-1)
    mouse_y = reactive(-1)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._ghost_lines: list[str] = []
        self._ghost_chars: list[list[str]] = []
        self._reveal_start = time.monotonic()
        # Deterministic RNG for per-column/per-frame scramble — keeps glitch
        # characters stable within a tick instead of jittering chaotically.
        self._rng = random.Random(0xC0FFEE)

    def on_mount(self):
        # Use app console size for accurate terminal dimensions
        import shutil
        term_w, term_h = shutil.get_terminal_size((120, 40))

        wordmark_w = max((len(line) for line in SYLL_ABLE_WORDMARK), default=0)
        gap = 3
        max_ghost_w = term_w - wordmark_w - gap - 2
        # Ghost fills full terminal height, width derived from aspect —
        # clamped low because the wordmark is now much wider (syll·able).
        ghost_w = int(term_h / 0.24)
        ghost_w = max(28, min(ghost_w, max_ghost_w))

        self._ghost_lines = render_ghost_ascii(
            width=ghost_w, charset=CHARSET_STANDARD, contrast=1.0, dither=True,
        )
        self._ghost_chars = [list(line) for line in self._ghost_lines]
        self._term_w = term_w
        self._term_h = term_h
        self.set_interval(1 / 30, self._tick)

    def _tick(self):
        self.tick = time.monotonic()

    def on_mouse_move(self, event):
        self.mouse_x = event.x
        self.mouse_y = event.y

    def on_leave(self, event):
        self.mouse_x = -1
        self.mouse_y = -1

    def watch_tick(self, value: float):
        self.refresh()

    # ------------------------------------------------------------------
    # Wordmark reveal helpers
    # ------------------------------------------------------------------

    def _wordmark_column(
        self, col: int, elapsed: float, breathe: float
    ) -> tuple[list[str], list[str | None]]:
        """Return (chars, styles) for one column of the wordmark at ``elapsed``.

        ``styles`` is parallel to ``chars``; ``None`` means a plain space.
        """
        chars: list[str] = []
        styles: list[str | None] = []

        col_start = WORDMARK_DELAY + col * WORDMARK_COL_STAGGER
        col_t = elapsed - col_start

        # Scramble seed: stable within each frame, differs across columns.
        frame = int(elapsed * 24)
        rng = random.Random((col << 12) ^ frame)

        # Which region does this column belong to? Splits drive color.
        if col < SYLL_WIDTH:
            region = "syll"
        elif col < SYLL_WIDTH + SEP_WIDTH:
            region = "sep"
        else:
            region = "able"

        for row, line in enumerate(SYLL_ABLE_WORDMARK):
            ch = line[col] if col < len(line) else " "
            if col_t < 0:
                chars.append(" ")
                styles.append(None)
                continue

            is_lit = ch in WORDMARK_LIT or ch in "╗╔╝╚═║"
            if not is_lit:
                chars.append(ch)
                styles.append(None)
                continue

            if col_t < WORDMARK_SCRAMBLE:
                # Scramble phase — tint picks from a region-matched accent
                # so even mid-scramble you can sense which half is which.
                glyph = rng.choice(SCRAMBLE_GLYPHS)
                chars.append(glyph)
                if region == "able":
                    accent = GRADIENT_ABLE[row % len(GRADIENT_ABLE)]
                else:
                    accent = GRADIENT_ACCENT[row % len(GRADIENT_ACCENT)]
                fade = 0.55 + 0.45 * (col_t / WORDMARK_SCRAMBLE)
                styles.append(f"bold {_blend_hex(accent, fade)}")
            else:
                # Settled — region-specific palette.
                rows_total = len(SYLL_ABLE_WORDMARK) - 1
                if region == "syll":
                    base = GRADIENT_COLD[
                        int(row / max(rows_total, 1) * (len(GRADIENT_COLD) - 1))
                    ]
                    factor = 1.0 + 0.18 * breathe
                elif region == "sep":
                    # The only live element — the breathing middle dot.
                    base = "#e2a57d"
                    factor = 0.65 + 0.35 * breathe
                else:  # able — muted, ghosted
                    base = GRADIENT_ABLE[
                        int(row / max(rows_total, 1) * (len(GRADIENT_ABLE) - 1))
                    ]
                    factor = 1.0 + 0.08 * breathe  # very subtle motion on able
                # Brief brightening right after the column lands.
                land_boost = max(0.0, 1.2 - (col_t - WORDMARK_SCRAMBLE) * 4)
                factor += land_boost * 0.28
                chars.append(ch)
                styles.append(f"bold {_blend_hex(base, factor)}")
        return chars, styles

    def _append_wordmark_block(
        self, result: Text, elapsed: float, indent: int
    ) -> float:
        """Append the 6-row wordmark to ``result``. Returns 0..1 progress."""
        cols = max(len(line) for line in SYLL_ABLE_WORDMARK)
        # Settled progress: 1.0 when every column has finished scrambling.
        total_time = WORDMARK_DELAY + cols * WORDMARK_COL_STAGGER + WORDMARK_SCRAMBLE
        progress = max(0.0, min(1.0, elapsed / total_time))

        # Breathing glow kicks in after everything settles.
        settled_at = WORDMARK_DELAY + cols * WORDMARK_COL_STAGGER + WORDMARK_SCRAMBLE
        if elapsed > settled_at:
            breathe = (math.sin((elapsed - settled_at) * 2.1) + 1) / 2
        else:
            breathe = 0.0

        # Pre-compute every column, then transpose into rows.
        columns = [self._wordmark_column(c, elapsed, breathe) for c in range(cols)]

        for row in range(len(SYLL_ABLE_WORDMARK)):
            result.append(" " * indent)
            for c in range(cols):
                chars, styles = columns[c]
                ch = chars[row]
                style = styles[row]
                if style is None:
                    result.append(ch)
                else:
                    result.append(ch, style=style)
            result.append("\n")
        return progress

    # ------------------------------------------------------------------
    # Render
    # ------------------------------------------------------------------

    def render(self):
        if not self._ghost_chars:
            return ""

        elapsed = time.monotonic() - self._reveal_start
        chars = self._ghost_chars
        ghost_h = len(chars)

        term_w = getattr(self, "_term_w", self.size.width)
        term_h = getattr(self, "_term_h", self.size.height)
        ghost_w = max((len(row) for row in chars), default=0)

        wordmark_w = max(len(line) for line in SYLL_ABLE_WORDMARK)
        right_w = max(wordmark_w, len(TAGLINE))
        gap = 4

        combined_w = ghost_w + gap + right_w
        pad_left = max(0, (term_w - combined_w) // 2)

        wordmark_h = len(SYLL_ABLE_WORDMARK)
        # Right column: wordmark + blank + version + tagline + blank + hint.
        right_block_h = wordmark_h + 4
        content_h = max(ghost_h, right_block_h)
        pad_top = max(0, (term_h - content_h) // 2)

        # Centre the wordmark vertically within the ghost column.
        right_start_y = max(0, (ghost_h - right_block_h) // 2)

        # Shimmer: a moving bright band that crosses the ghost every few
        # seconds. Period ≫ sweep, so it's a quick highlight, not constant.
        cycle = elapsed % SHIMMER_PERIOD
        shimmer_progress = cycle / 0.9  # sweep finishes in 0.9s
        shimmer_row = int(shimmer_progress * ghost_h) if shimmer_progress <= 1 else -1

        result = Text()
        for _ in range(pad_top):
            result.append("\n")

        total_lines = max(ghost_h, right_start_y + right_block_h)

        for y in range(total_lines):
            line_reveal = min(1.0, max(0.0, (elapsed - y * GHOST_LINE_STAGGER) / GHOST_LINE_FADE))

            # Left: ghost ASCII
            if y < ghost_h and line_reveal > 0:
                row_chars = chars[y]
                ci = int(y / max(total_lines - 1, 1) * (len(GRADIENT_COLD) - 1))
                base_color = GRADIENT_COLD[ci]
                if line_reveal < 1.0:
                    base_color = _blend_hex(base_color, line_reveal)

                # Build ghost line cell-by-cell so we can tint shimmer rows
                # and the mouse-ripple without losing per-char styling.
                result.append(" " * pad_left)
                for x, ch in enumerate(row_chars):
                    if ch == " ":
                        result.append(" ")
                        continue
                    cell_color = base_color
                    # Mouse ripple: distort density + brighten nearby cells.
                    if self.mouse_x >= 0 and self.mouse_y >= 0:
                        dx = (x + pad_left) - self.mouse_x
                        dy = (y + pad_top) - self.mouse_y
                        dist = math.sqrt(dx * dx + dy * dy)
                        if dist < 15:
                            wave = math.sin(dist * 0.6 - self.tick * 3) * (1 - dist / 15)
                            idx = CHARSET_STANDARD.find(ch)
                            if idx >= 0:
                                new_idx = max(0, min(len(CHARSET_STANDARD) - 1, idx + int(wave * 3)))
                                ch = CHARSET_STANDARD[new_idx]
                            cell_color = _blend_hex(cell_color, 1.0 + 0.35 * (1 - dist / 15))
                    # Shimmer sweep row — brighten whole row briefly.
                    if y == shimmer_row:
                        cell_color = _blend_hex(cell_color, 1.6)
                    result.append(ch, style=f"bold {cell_color}")
                # Pad out the ghost column so the wordmark aligns.
                written = len(row_chars)
                if written < ghost_w:
                    result.append(" " * (ghost_w - written))
            else:
                result.append(" " * (pad_left + ghost_w))

            # Right column: wordmark / version / tagline / hint.
            ri = y - right_start_y
            if 0 <= ri < right_block_h:
                result.append(" " * gap)
                if ri < wordmark_h:
                    self._render_wordmark_row(result, ri, elapsed)
                elif ri == wordmark_h + 1:
                    self._render_subline(result, VERSION_LINE, elapsed, delay=0.05, center_width=wordmark_w)
                elif ri == wordmark_h + 2:
                    self._render_subline(result, TAGLINE, elapsed, delay=0.25, center_width=wordmark_w)
                elif ri == wordmark_h + 3:
                    self._render_subline(result, HINT_LINE, elapsed, delay=0.55, center_width=wordmark_w, dim=True)

            result.append("\n")

        return result

    # ------------------------------------------------------------------
    # Right-column row helpers
    # ------------------------------------------------------------------

    def _render_wordmark_row(self, result: Text, row: int, elapsed: float) -> None:
        cols = max(len(line) for line in SYLL_ABLE_WORDMARK)
        settled_at = WORDMARK_DELAY + cols * WORDMARK_COL_STAGGER + WORDMARK_SCRAMBLE
        breathe = (math.sin((elapsed - settled_at) * 2.1) + 1) / 2 if elapsed > settled_at else 0.0

        for c in range(cols):
            chars, styles = self._wordmark_column(c, elapsed, breathe)
            ch = chars[row]
            style = styles[row]
            if style is None:
                result.append(ch)
            else:
                result.append(ch, style=style)

    def _render_subline(
        self,
        result: Text,
        text: str,
        elapsed: float,
        *,
        delay: float,
        center_width: int,
        dim: bool = False,
    ) -> None:
        cols = max(len(line) for line in SYLL_ABLE_WORDMARK)
        tag_start = WORDMARK_DELAY + cols * WORDMARK_COL_STAGGER + WORDMARK_SCRAMBLE + TAGLINE_DELAY_AFTER_WORDMARK + delay
        t = elapsed - tag_start
        if t <= 0:
            result.append(" " * center_width)
            return
        fade = min(1.0, t / 0.4)
        pad = max(0, (center_width - len(text)) // 2)
        result.append(" " * pad)
        # Warm cream / warm taupe — pairs with the soft peach wordmark gradient.
        color = "#f4dcc1" if not dim else "#a58a7a"
        result.append(text, style=f"{'dim ' if dim else 'bold '}{_blend_hex(color, fade)}")
        # Trailing pad so width stays stable.
        trail = center_width - pad - len(text)
        if trail > 0:
            result.append(" " * trail)


class SplashScreen(App):
    """Full-screen TUI splash with ghost ASCII art."""

    CSS = """
    Screen {
        background: #14100c;
    }
    #splash {
        width: 1fr;
        height: 1fr;
    }
    """

    BINDINGS = [
        ("escape", "quit", "Quit"),
    ]

    def __init__(self, auto_dismiss_seconds: float | None = None, **kwargs):
        super().__init__(**kwargs)
        self._auto_dismiss_seconds = auto_dismiss_seconds

    def compose(self) -> ComposeResult:
        yield GhostAsciiWidget(id="splash")

    def on_mount(self):
        if self._auto_dismiss_seconds and self._auto_dismiss_seconds > 0:
            self.set_timer(self._auto_dismiss_seconds, self.exit)

    def on_key(self, event):
        self.exit()

    def on_click(self, event):
        self.exit()


def run_splash(auto_dismiss_seconds: float | None = None):
    """Show the splash screen.

    Args:
        auto_dismiss_seconds: If set, auto-dismiss after this many seconds.
            Otherwise, blocks until user presses a key.
    """
    app = SplashScreen(auto_dismiss_seconds=auto_dismiss_seconds)
    app.run()


if __name__ == "__main__":
    run_splash()
