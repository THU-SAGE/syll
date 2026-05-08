"""Phase 5 polish: tool-allowlist checkbox grid + live WS status pill.

Both are UI-side. Tests assert the Alpine helpers are present and the
HTML form renders the right shape:

  - `mcpFormDiscoveredTools()` returns the live `available_tools` array
    when the form is editing an existing server, else [].
  - `mcpFormToggleAllTools(true)` collapses to `["*"]`.
  - `mcpFormToggleAllTools(false)` seeds explicit list from discovered.
  - `mcpFormToggleTool(tool, false)` removes "*" and the named tool.
  - The HTML form has a checkbox grid (gated on discovered) AND a JSON
    fallback for unsaved/unconnected servers.
  - The `mcp_server_status` WS branch mutates the in-memory entry rather
    than always re-fetching.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APP_JS = REPO / "syll" / "web" / "static" / "app.js"
INDEX_HTML = REPO / "syll" / "web" / "static" / "index.html"


# ── JS helpers ─────────────────────────────────────────────────────────


def test_mcp_form_tool_helpers_present():
    src = APP_JS.read_text(encoding="utf-8")
    for fn in [
        "mcpFormDiscoveredTools",
        "mcpFormToolsAllowAll",
        "mcpFormToolEnabled",
        "mcpFormToggleAllTools",
        "mcpFormToggleTool",
    ]:
        assert fn in src, f"missing helper {fn}"


def test_mcp_form_toggle_all_clamps_to_star():
    """Toggling the 'All tools' checkbox ON must collapse enabled_tools to ['*']."""
    src = APP_JS.read_text(encoding="utf-8")
    body = _extract_body(src, "mcpFormToggleAllTools")
    assert "['*']" in body or "[\"*\"]" in body, (
        "mcpFormToggleAllTools must set enabled_tools = ['*'] on `checked`"
    )


def test_mcp_form_toggle_tool_strips_star_when_picking_individual():
    """When the user toggles an individual tool, '*' must be stripped so the
    list isn't simultaneously 'all' AND a specific subset."""
    src = APP_JS.read_text(encoding="utf-8")
    body = _extract_body(src, "mcpFormToggleTool")
    assert "filter" in body and "'*'" in body, (
        "mcpFormToggleTool must filter out '*' from enabled_tools"
    )


def _extract_body(src: str, fn_name: str) -> str:
    """Pull a function body by brace-matching from the first `{` after the name."""
    idx = src.index(fn_name + "(")
    open_brace = src.index("{", idx)
    depth = 0
    for i in range(open_brace, len(src)):
        c = src[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[open_brace : i + 1]
    raise AssertionError(f"no matching brace for {fn_name}")


# ── HTML form ──────────────────────────────────────────────────────────


def test_form_has_checkbox_grid_when_discovered_tools_known():
    """The form must show a grid of discovered tools as checkboxes (with an
    'All tools (*)' master checkbox)."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    # The grid is gated on `mcpFormDiscoveredTools().length`.
    assert "mcpFormDiscoveredTools().length" in html
    # And explicit "All tools (*)" master checkbox.
    assert re.search(r"All tools \(\*\)", html)


def test_form_has_json_fallback_when_no_discovered_tools():
    """For unsaved / unconnected servers, the form falls back to a JSON input."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    # The fallback uses an `x-if="!mcpFormDiscoveredTools().length"`.
    assert "!mcpFormDiscoveredTools().length" in html


def test_form_warns_about_browser_run_code_unsafe():
    """Phase 1b kept this tool out of default templates; the form should
    visibly mark it when present in discovered tools so a user picking
    explicit tools sees the warning."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    # Color cue + label addition.
    assert "browser_run_code_unsafe" in html
    assert "arbitrary JS" in html


# ── WS live-status branch ─────────────────────────────────────────────


def test_mcp_server_status_branch_mutates_in_place():
    """The chat WS handler should call `_onMcpServerStatus` rather than
    unconditionally `loadMcpServers()`. The handler must mutate the
    existing server entry in place so Alpine reactivity updates the pill
    without flickering."""
    src = APP_JS.read_text(encoding="utf-8")
    # Branch dispatch.
    assert "case 'mcp_server_status':" in src
    assert "this._onMcpServerStatus(data);" in src
    # Handler must mutate `existing.status = ...` to update the pill in place.
    handler = _extract_body(src, "_onMcpServerStatus")
    assert "existing.status" in handler, (
        "_onMcpServerStatus must mutate the in-memory entry, not always refetch"
    )
    # And debounce the refetch so a flapping server doesn't hammer the API.
    assert "setTimeout" in handler
    assert "_mcpRefetchTimer" in handler


# ── MCP UI palette parity (regression for the restyle) ─────────────────


def test_mcp_panel_uses_canonical_section_classes():
    """The MCP tab must reuse the same `.config-section` / `.section-header` /
    `.section-title` / `.section-chevron` / `.section-body` vocabulary that
    the rest of the Pet UI uses, so the visual rhythm matches the Profile
    and Config tabs."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    # The MCP panel block lives between the tab marker and the Config tab.
    panel_start = html.index('activeTab === \'mcp\'')
    panel_end = html.index("<!-- Config Tab -->", panel_start)
    panel = html[panel_start:panel_end]
    for cls in (
        "config-section",
        "section-header",
        "section-title",
        "section-chevron",
        "section-body",
    ):
        assert cls in panel, f"MCP panel missing canonical class {cls!r}"


def test_mcp_panel_does_not_use_undefined_classes():
    """An earlier version used `.config-button` / `.config-section-header` /
    `.config-section-body` which were never defined in CSS — page rendered
    flat. Catch any reintroduction."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    panel_start = html.index('activeTab === \'mcp\'')
    panel_end = html.index("<!-- Config Tab -->", panel_start)
    panel = html[panel_start:panel_end]
    for bad in ("config-button", "config-section-header", "config-section-body"):
        assert bad not in panel, (
            f"MCP panel still references undefined class {bad!r} — visual regression"
        )


def test_mcp_specific_classes_have_css_definitions():
    """Every `.mcp-*` class referenced by the MCP panel must have a matching
    CSS rule — otherwise we'd ship more invisible-class breakage."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    for cls in (
        "mcp-callout",
        "mcp-callout--warn",
        "mcp-pill",
        "mcp-pill--connected",
        "mcp-pill--failed",
        "mcp-pill--needs",
        "mcp-pill--installed",
        "mcp-card",
        "mcp-card-head",
        "mcp-card-detail",
        "mcp-button",
        "mcp-button--primary",
        "mcp-button--danger",
        "mcp-button--ghost",
        "mcp-empty",
        "mcp-template",
        "mcp-template-head",
        "mcp-template-title",
        "mcp-template-id",
        "mcp-template-link",
        "mcp-template-desc",
        "mcp-template-warn",
        "mcp-template-hint",
        "mcp-log",
        "mcp-error",
        "mcp-form-grid",
        "mcp-tools",
        "mcp-tools-master",
        "mcp-tool-row",
        "mcp-tool-row--unsafe",
        "mcp-form-foot",
        "mcp-test-result",
        "mcp-test-result--ok",
        "mcp-test-result--err",
        "mcp-modal-backdrop",
        "mcp-modal",
        "mcp-modal-actions",
    ):
        assert f".{cls} " in html or f".{cls}{{" in html or f".{cls}{{" in html.replace(" {", "{"), (
            f"class .{cls} referenced but has no CSS definition"
        )


def test_mcp_panel_uses_palette_css_variables():
    """No raw color literals like `#444` / `#888` / `rgba(180,0,0,...)` in the
    MCP panel (those don't theme). The vocabulary must reach the palette
    (var(--ink-*), var(--bg-*), var(--line), var(--accent), etc.)."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    panel_start = html.index('activeTab === \'mcp\'')
    panel_end = html.index("<!-- Config Tab -->", panel_start)
    panel = html[panel_start:panel_end]
    # Allow color literals only inside semantic helpers (status colors are
    # rendered explicitly and should be theme-checked separately). The HTML
    # body itself should not hardcode `#xxx` colors anymore.
    forbidden_inline_colors = re.findall(r"color:\s*#[0-9a-fA-F]{3,6}", panel)
    forbidden_inline_bgs = re.findall(r"background:\s*rgba?\(\d+,\s*\d+", panel)
    assert not forbidden_inline_colors, (
        f"MCP panel HTML still has hardcoded colors: {forbidden_inline_colors[:5]}"
    )
    assert not forbidden_inline_bgs, (
        f"MCP panel HTML still has hardcoded rgba backgrounds: {forbidden_inline_bgs[:5]}"
    )
