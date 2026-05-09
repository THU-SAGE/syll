"""ASCII dither renderer, Syll wordmark and Rich banner for the Syll CLI."""

from __future__ import annotations

import math
import time
from pathlib import Path

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

_ICON_PATH = Path(__file__).parent / "ghost_icon.png"

# Character sets ordered by visual density (sparse → dense)
CHARSET_STANDARD = " .:-=+*#%@"
CHARSET_DETAILED = " .'`^\":;Il!i><~+_-?][}{1)(|/tfjrxnuvczXYUJCLQ0OZmwqpdbkhao*#MW&8%B@$"
CHARSET_BLOCKS = " ░▒▓█"
CHARSET_BRAILLE = " ⠁⠂⠃⠄⠅⠆⠇⡀⡁⡂⡃⡄⡅⡆⡇⠈⠉⠊⠋⠌⠍⠎⠏⡈⡉⡊⡋⡌⡍⡎⡏⣀⣁⣂⣃⣄⣅⣆⣇⣈⣉⣊⣋⣌⣍⣎⣏⠐⠑⠒⠓⠔⠕⠖⠗⡐⡑⡒⡓⡔⡕⡖⡗⣐⣑⣒⣓⣔⣕⣖⣗⣘⣙⣚⣛⣜⣝⣞⣟⠠⠡⠢⠣⠤⠥⠦⠧⡠⡡⡢⡣⡤⡥⡦⡧⣠⣡⣢⣣⣤⣥⣦⣧⣨⣩⣪⣫⣬⣭⣮⣯⠰⠱⠲⠳⠴⠵⠶⠷⡰⡱⡲⡳⡴⡵⡶⡷⣰⣱⣲⣳⣴⣵⣶⣷⣸⣹⣺⣻⣼⣽⣾⣿"

# Gentle warm gradient (top → bottom of the banner) — oat milk, peach
# blossom, dusty rose, warm mauve. A softer sibling of the web UI's
# golden --accent that keeps the CLI feeling candlelit instead of bright.
# Name kept as GRADIENT_COLD for backwards-compatibility with imports.
GRADIENT_COLD = [
    "#faeed9", "#f4d7b0", "#edbe8e", "#e2a57d",
    "#d18b75", "#b47168", "#8f5a5a", "#68454b",
]

# Scramble-phase highlight — a gentle ember accent used while each
# wordmark column is still booting. Still warm, just a touch brighter
# so the glitch frames catch the eye without shouting.
GRADIENT_ACCENT = [
    "#fff2dc", "#f5d2a8", "#e8a988", "#c67f6e",
]

# Hand-crafted "ANSI Shadow" SYLL wordmark — renders the same everywhere
# without depending on pyfiglet being installed. 6 rows tall.
SYLL_WORDMARK = [
    "███████╗██╗   ██╗██╗     ██╗     ",
    "██╔════╝╚██╗ ██╔╝██║     ██║     ",
    "███████╗ ╚████╔╝ ██║     ██║     ",
    "╚════██║  ╚██╔╝  ██║     ██║     ",
    "███████║   ██║   ███████╗███████╗",
    "╚══════╝   ╚═╝   ╚══════╝╚══════╝",
]

# Companion "ABLE" block, same ANSI-Shadow typeface so syll+able feel
# like the two halves of one word. 6 rows, 32 cols.
ABLE_WORDMARK = [
    " █████╗ ██████╗ ██╗     ███████╗",
    "██╔══██╗██╔══██╗██║     ██╔════╝",
    "███████║██████╔╝██║     █████╗  ",
    "██╔══██║██╔══██╗██║     ██╔══╝  ",
    "██║  ██║██████╔╝███████╗███████╗",
    "╚═╝  ╚═╝╚═════╝ ╚══════╝╚══════╝",
]

# Vertically-centred middle dot separator — sits between syll and able.
# 4 cols wide; only rows 2-3 carry ink.
WORDMARK_SEP = [
    "    ",
    "    ",
    " ██ ",
    " ██ ",
    "    ",
    "    ",
]

SYLL_ABLE_WORDMARK = [
    SYLL_WORDMARK[i] + WORDMARK_SEP[i] + ABLE_WORDMARK[i] for i in range(6)
]
SYLL_WIDTH = len(SYLL_WORDMARK[0])
SEP_WIDTH = len(WORDMARK_SEP[0])
ABLE_WIDTH = len(ABLE_WORDMARK[0])
SYLL_ABLE_WIDTH = SYLL_WIDTH + SEP_WIDTH + ABLE_WIDTH

# Faded sibling of GRADIENT_COLD — used for the "able" half so it reads
# as a ghosted echo of the solid "syll" half.
GRADIENT_ABLE = [
    "#8a7469", "#7a6458", "#685248", "#56433c",
    "#433330", "#322625",
]

# Characters that indicate a "lit" cell in the wordmark (used by the
# splash animation's glitch/reveal heads).
WORDMARK_LIT = set("█▀▄▐▌")


def render_ghost_ascii(
    width: int = 48,
    charset: str = CHARSET_STANDARD,
    contrast: float = 1.0,
    brightness: float = 0.0,
    invert: bool = False,
    dither: bool = True,
) -> list[str]:
    """Render ghost_icon.png to a list of ASCII art lines.

    Args:
        width: character width of the output
        charset: density-ordered character set
        contrast: >1 increases contrast
        brightness: offset (-1 to 1)
        invert: if True, dark areas → dense characters
        dither: if True, apply Floyd-Steinberg dithering
    """
    try:
        from PIL import Image
    except ImportError:
        return ["  [ghost unavailable: Pillow not installed]"]

    if not _ICON_PATH.exists():
        return ["  [ghost icon not found]"]

    img = Image.open(_ICON_PATH).convert("RGB")
    # Terminal characters are roughly 2:1 aspect ratio
    height = int(img.height / img.width * width * 0.48)
    img = img.resize((width, height), Image.LANCZOS)

    n = len(charset)
    pixels = img.load()

    # Build grayscale matrix; treat dark background as transparent
    gray_matrix = []
    bg_mask = []  # True = background (skip)
    for y in range(height):
        row_g = []
        row_bg = []
        for x in range(width):
            r, g, b = pixels[x, y]
            gray = 0.299 * r + 0.587 * g + 0.114 * b
            # Apply contrast + brightness
            gray = (gray - 128) * contrast + 128 + brightness * 255
            gray = max(0.0, min(255.0, gray))
            if invert:
                gray = 255.0 - gray
            row_g.append(gray)
            # Background detection: original pixel is dark
            raw_gray = 0.299 * r + 0.587 * g + 0.114 * b
            row_bg.append(raw_gray < 90)
        gray_matrix.append(row_g)
        bg_mask.append(row_bg)

    # Floyd-Steinberg dithering
    if dither:
        for y in range(height):
            for x in range(width):
                if bg_mask[y][x]:
                    continue
                old = gray_matrix[y][x]
                # Quantize to charset levels
                idx = max(0, min(n - 1, int(old / 256.0 * n)))
                new = (idx + 0.5) * 256.0 / n
                err = old - new
                gray_matrix[y][x] = new
                # Distribute error
                if x + 1 < width:
                    gray_matrix[y][x + 1] += err * 7 / 16
                if y + 1 < height:
                    if x - 1 >= 0:
                        gray_matrix[y + 1][x - 1] += err * 3 / 16
                    gray_matrix[y + 1][x] += err * 5 / 16
                    if x + 1 < width:
                        gray_matrix[y + 1][x + 1] += err * 1 / 16

    # Convert to characters
    lines = []
    for y in range(height):
        line = ""
        for x in range(width):
            if bg_mask[y][x]:
                line += " "
                continue
            gray = max(0.0, min(255.0, gray_matrix[y][x]))
            idx = max(0, min(n - 1, int(gray / 256.0 * n)))
            line += charset[idx]
        lines.append(line.rstrip())

    # Trim empty top/bottom
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()

    return lines


def build_syll_title_block(version: str) -> list[str]:
    """Build the shared Syll title stack for CLI banners and splash screens.

    Prefers the hand-crafted wordmark so the output is identical on every
    machine. Falls back to a compact text line only if someone has trimmed
    the wordmark to empty.
    """
    title_lines = list(SYLL_WORDMARK) if SYLL_WORDMARK else ["S Y L L"]
    tagline = "╶  your syll in the shell  ╴"
    version_line = f"                v{version}"
    return [""] + title_lines + ["", version_line, tagline]


def render_rich_banner(console: Console | None = None, width: int = 48):
    """Print a Rich-formatted banner with ghost ASCII art + Syll title block."""
    if console is None:
        console = Console()

    ghost_lines = render_ghost_ascii(width=width)

    from syll import __version__
    right_lines = build_syll_title_block(__version__)

    # Combine: ghost left, title block right
    ghost_w = max((len(l) for l in ghost_lines), default=0)
    right_h = len(right_lines)
    right_start_y = max(0, (len(ghost_lines) - right_h) // 2)

    total = max(len(ghost_lines), right_start_y + right_h)
    combined = []
    for i in range(total):
        left = ghost_lines[i].ljust(ghost_w) if i < len(ghost_lines) else " " * ghost_w
        ri = i - right_start_y
        right = "  " + right_lines[ri] if 0 <= ri < right_h else ""
        combined.append(left + right)

    # Print with gradient
    n_lines = len(combined)
    for i, line in enumerate(combined):
        ci = int(i / max(n_lines - 1, 1) * (len(GRADIENT_COLD) - 1))
        console.print(line, style=f"bold {GRADIENT_COLD[ci]}", highlight=False)
    console.print()


# ---------------------------------------------------------------------------
# Post-launch boot report
# ---------------------------------------------------------------------------


# Colour helpers shared with splash.py.
def blend_hex(color: str, factor: float) -> str:
    """Scale a ``#rrggbb`` colour by ``factor`` (0..1+) with safe clamping."""
    r = int(int(color[1:3], 16) * factor)
    g = int(int(color[3:5], 16) * factor)
    b = int(int(color[5:7], 16) * factor)
    r = max(0, min(255, r))
    g = max(0, min(255, g))
    b = max(0, min(255, b))
    return f"#{r:02x}{g:02x}{b:02x}"


# Glyphs the status dot cycles through while a row is still "booting".
SCRAMBLE_DOTS = ["⋅", "·", "•", "◦"]

# Ornamental side-rule used at section dividers + footer.
ORNAMENT_LINE = "╶"
ORNAMENT_STAR = "⋆"

# Animation timings (seconds).
ROW_STAGGER = 0.07
ROW_SCRAMBLE = 0.22
SETTLE_HOLD = 0.35


def _status_dot(kind: str) -> Text:
    """Return a small coloured dot for a service status row.

    Desaturated semantics — sage mint, soft honey, warm taupe, dusty rose
    — so the panel reads as calm status glyphs rather than dashboard
    traffic lights.
    """
    if kind == "ok":
        return Text("●", style="bold #7ec8a9")
    if kind == "warn":
        return Text("●", style="bold #e6a96b")
    if kind == "off":
        return Text("○", style="#8a7d73")
    if kind == "endpoint":
        return Text("→", style="bold #edbe8e")
    return Text("●", style="bold #d17f7c")


def _section_divider(name: str, width: int = 56) -> Text:
    """Label + trailing dashed rule for a section header."""
    label = f" {name} "
    dashes = max(4, width - len(label) - 2)
    out = Text()
    out.append("  ")
    out.append(label, style="bold #e2a57d")
    out.append("╶" + "─" * dashes + "╴", style="#68454b")
    return out


def _footer_line(text: str, width: int = 58) -> Text:
    """Centered footer with ⋆-pinned rules flanking a short message."""
    core = f"  {text}  "
    rule_len = max(3, (width - len(core) - 6) // 2)
    out = Text()
    out.append(" " * 2)
    out.append("╶" + "─" * rule_len + f" {ORNAMENT_STAR} ", style="#8f5a5a")
    out.append(core, style="italic #a58a7a")
    out.append(f"{ORNAMENT_STAR} " + "─" * rule_len + "╴", style="#8f5a5a")
    return out


def build_syll_able_text(elapsed: float = 999.0) -> list[Text]:
    """Return the 6-row SYLL·ABLE pixel wordmark as Rich Text lines.

    Three-way color split:
      - **syll** rides the warm ``GRADIENT_COLD`` top→bottom — the solid
        half, the CLI name that actually runs.
      - **·** (middle dot) breathes in peach — the only truly dynamic
        element, a 1.9 rad/s sinusoid on top of the static mark.
      - **able** rides the faded ``GRADIENT_ABLE`` — the ghosted half,
        visible but clearly half-remembered.

    Used by both the splash screen (animated column reveal) and the
    dashboard top header (steady state).
    """
    pulse = 0.55 + 0.45 * (0.5 + 0.5 * math.sin(elapsed * 1.9))
    dot_color = blend_hex("#e2a57d", pulse)

    rows: list[Text] = []
    for i in range(6):
        t = Text()
        syll_idx = int(i / max(len(SYLL_ABLE_WORDMARK) - 1, 1) * (len(GRADIENT_COLD) - 1))
        able_idx = int(i / max(len(SYLL_ABLE_WORDMARK) - 1, 1) * (len(GRADIENT_ABLE) - 1))
        syll_color = GRADIENT_COLD[syll_idx]
        able_color = GRADIENT_ABLE[able_idx]

        t.append(SYLL_WORDMARK[i], style=f"bold {syll_color}")
        if "█" in WORDMARK_SEP[i]:
            t.append(WORDMARK_SEP[i], style=f"bold {dot_color}")
        else:
            t.append(WORDMARK_SEP[i])
        t.append(ABLE_WORDMARK[i], style=f"bold {able_color}")
        rows.append(t)
    return rows


def _header_block(title: str, subtitle: str | None, elapsed: float) -> Table | Text:
    """Boot-panel header: title + italic subtitle (wordmark now lives at
    the top of the dashboard / splash as the big pixel mark, so the panel
    stays compact).

    Returns an empty ``Text`` when both title and subtitle are falsy so
    the panel renders section content flush against its top edge — used
    by the dashboard where identity is already set above.
    """
    if not title and not subtitle:
        return Text("")
    grid = Table.grid(padding=(0, 0))
    grid.add_column()
    if title:
        grid.add_row(Text(title, style="bold #f4dcc1"))
    if subtitle:
        grid.add_row(Text(subtitle, style="italic #a58a7a"))
    return grid


def _render_row(
    status: str,
    label: str,
    value: str,
    hint: str,
    elapsed: float,
    row_start: float,
) -> tuple[Text, Text, Text, Text]:
    """Render one status row at the given animation time."""
    t = elapsed - row_start
    if t < 0:
        # Hidden placeholder — same shape so panel height stays stable.
        return (Text(" "), Text(""), Text(""), Text(""))

    if t < ROW_SCRAMBLE:
        fade = 0.42 + 0.58 * (t / ROW_SCRAMBLE)
        frame = int(t / 0.05)
        dot = Text(SCRAMBLE_DOTS[frame % len(SCRAMBLE_DOTS)], style="bold #e2a57d")
        label_t = Text(label, style=f"bold {blend_hex('#f4dcc1', fade)}")
        value_t = Text(value, style=blend_hex("#ecebe7", fade))
        hint_t = Text(hint, style=blend_hex("#a58a7a", fade)) if hint else Text("")
        return (dot, label_t, value_t, hint_t)

    dot = _status_dot(status)
    label_t = Text(label, style="bold #f4dcc1")
    value_t = Text(value, style="#ecebe7")
    hint_t = Text(hint, style="#a58a7a") if hint else Text("")
    return (dot, label_t, value_t, hint_t)


def _build_boot_panel(
    title: str,
    subtitle: str | None,
    sections: list[tuple[str, list[tuple[str, str, str, str]]]],
    footer: str | None,
    elapsed: float,
) -> Panel:
    """Build the boot panel at a specific animation timestamp."""
    body: list = [_header_block(title, subtitle, elapsed)]

    # Compute max column widths across ALL rows so each section's grid
    # has stable column widths — otherwise the panel border visibly
    # jitters as rows appear during the reveal.
    all_rows = [r for _, rs in sections for r in rs]
    max_label = max((len(r[1]) for r in all_rows), default=11)
    max_value = max((len(r[2]) for r in all_rows), default=20)
    max_hint = max((len(r[3] or "") for r in all_rows), default=0)

    row_idx = 0
    for section_name, rows in sections:
        body.append(Text(""))
        body.append(_section_divider(section_name))

        tbl = Table.grid(padding=(0, 1))
        tbl.add_column(width=2)                     # left indent
        tbl.add_column(width=1)                     # status dot
        tbl.add_column(min_width=max_label)         # label
        tbl.add_column(min_width=max_value, overflow="fold")  # value
        tbl.add_column(style="#a58a7a", min_width=max_hint)   # hint

        for status, label, value, hint in rows:
            dot, label_t, value_t, hint_t = _render_row(
                status, label, value, hint or "", elapsed, row_idx * ROW_STAGGER
            )
            tbl.add_row("", dot, label_t, value_t, hint_t)
            row_idx += 1

        body.append(tbl)

    if footer:
        body.append(Text(""))
        body.append(_footer_line(footer))

    return Panel(
        Group(*body),
        border_style="#d18b75",
        padding=(1, 2),
        expand=False,
    )


def render_boot_report(
    console: Console,
    *,
    title: str,
    subtitle: str | None = None,
    sections: list[tuple[str, list[tuple[str, str, str, str]]]] | None = None,
    rows: list[tuple[str, str, str, str]] | None = None,
    footer: str | None = None,
    animate: bool = True,
) -> None:
    """Render the post-splash boot panel.

    Args:
        console: Rich console to render into.
        title: Primary header text (e.g. ``"syll · gateway"``). Emoji-free.
        subtitle: Italic second line under the title.
        sections: ``[(section_name, rows), ...]`` where each ``row`` is
            ``(status, label, value, hint)`` and ``status`` ∈
            ``ok|warn|off|endpoint|err``.
        rows: Legacy flat row list — wrapped into a single ``runtime`` section.
        footer: Short centred message shown under an ornamental rule.
        animate: If true and the console is a real TTY, reveal rows with a
            scramble-to-settled micro-animation via ``rich.live.Live``.
            Non-TTYs fall back to a single static render.
    """
    if sections is None:
        sections = [("runtime", rows or [])]

    total_rows = sum(len(r) for _, r in sections)
    total_time = max(
        (total_rows - 1) * ROW_STAGGER + ROW_SCRAMBLE + SETTLE_HOLD,
        0.95,  # minimum animation length so the wordmark pulse is visible
    )

    if not animate or not console.is_terminal:
        console.print(_build_boot_panel(title, subtitle, sections, footer, elapsed=total_time + 5))
        return

    with Live(
        _build_boot_panel(title, subtitle, sections, footer, elapsed=0.0),
        console=console,
        refresh_per_second=30,
        transient=False,
    ) as live:
        start = time.monotonic()
        frame_dt = 1 / 30
        while True:
            elapsed = time.monotonic() - start
            if elapsed > total_time:
                break
            live.update(_build_boot_panel(title, subtitle, sections, footer, elapsed=elapsed))
            time.sleep(frame_dt)
        live.update(_build_boot_panel(title, subtitle, sections, footer, elapsed=total_time + 5))


# ---------------------------------------------------------------------------
# Loguru palette helper
# ---------------------------------------------------------------------------


def configure_warm_logging(level: str = "INFO") -> None:
    """Restyle loguru output to match the CLI's warm palette.

    Replaces the default loguru sink with a format that uses sage mint
    for INFO, soft honey for WARNING, dusty rose for ERROR, so the
    service logs flowing under the boot panel visually belong to it.
    Safe to call more than once.
    """
    try:
        from loguru import logger
    except ImportError:
        return
    import sys

    logger.remove()
    logger.level("INFO", color="<fg #7ec8a9>")
    logger.level("WARNING", color="<fg #e6a96b>")
    logger.level("ERROR", color="<bold><fg #d17f7c>")
    logger.level("CRITICAL", color="<bold><fg #d17f7c>")
    logger.level("DEBUG", color="<fg #8a7d73>")
    try:
        logger.level("SUCCESS", color="<fg #7ec8a9>")
    except Exception:
        pass

    logger.add(
        sys.stderr,
        colorize=True,
        format=(
            "<fg #8a7d73>{time:HH:mm:ss}</>  "
            "<level>{level: <7}</>  "
            "<fg #a6856a>{name}</><fg #68454b>:</><fg #c8a88a>{function}</>  "
            "<fg #ecebe7>{message}</>"
        ),
        level=level,
    )
