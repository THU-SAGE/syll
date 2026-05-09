"""Phase 1a CSP tests: header on every response, no scheme-wide ws:, no remote
scripts in index.html, vendored sha256 matches manifest, renderMarkdown wired
to DOMPurify."""

from __future__ import annotations

import hashlib
import re
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from syll.web import auth as auth_module
from syll.web.app import create_app
from syll.web.csp import build_csp
from tests.test_app_factory import _make_agent_loop, _make_config

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = REPO_ROOT / "syll" / "web" / "static" / "index.html"
VENDOR_DIR = REPO_ROOT / "syll" / "web" / "static" / "vendor"
APP_JS = REPO_ROOT / "syll" / "web" / "static" / "app.js"
VERSIONS_MD = VENDOR_DIR / "VERSIONS.md"


@pytest.fixture(autouse=True)
def _isolate_token(monkeypatch, tmp_path):
    monkeypatch.setattr(auth_module, "ADMIN_TOKEN_PATH", tmp_path / "admin_token")
    yield


def _make_app(**gw_overrides):
    tmp = Path(tempfile.mkdtemp())
    cfg = _make_config(tmp)
    cfg.gateway.allow_remote_admin = gw_overrides.get("allow_remote_admin", False)
    cfg.gateway.allow_origins = gw_overrides.get("allow_origins", [])
    return create_app(
        config=cfg,
        agent_loop=_make_agent_loop(),
        session_manager=SimpleNamespace(),
        skills_loader=SimpleNamespace(),
        memory_store=SimpleNamespace(),
        cron_service=None,
    )


# ── (a) every response carries CSP header ────────────────────────────────


def test_csp_header_on_admin_token_route():
    app = _make_app()
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        r = client.get(
            "/api/v1/admin-token",
            headers={"Origin": "http://localhost:18790"},
        )
        assert "Content-Security-Policy" in r.headers
        assert "X-Frame-Options" in r.headers
        assert r.headers["X-Frame-Options"] == "DENY"
        assert r.headers["Referrer-Policy"] == "no-referrer"


def test_csp_header_on_root():
    app = _make_app()
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        r = client.get("/")
        assert "Content-Security-Policy" in r.headers


# ── (b) no remote <script> or <link> tags in index.html (except fonts) ────


def test_index_html_no_remote_scripts():
    text = INDEX_HTML.read_text(encoding="utf-8")
    remote_scripts = re.findall(
        r'<script[^>]*\ssrc=["\'](https?://[^"\']+)["\']', text, flags=re.I
    )
    assert remote_scripts == [], (
        f"Phase 1a forbids remote <script> tags; found: {remote_scripts}"
    )


def test_index_html_no_remote_links_except_fonts():
    text = INDEX_HTML.read_text(encoding="utf-8")
    remote_links = re.findall(
        r'<link[^>]*\shref=["\'](https?://[^"\']+)["\']', text, flags=re.I
    )
    # Phase 1a allows only fonts.googleapis.com (vendored in Phase 1d).
    bad = [u for u in remote_links if not u.startswith("https://fonts.googleapis.com")]
    assert bad == [], (
        f"Phase 1a allows only fonts.googleapis.com remote <link>; found: {bad}"
    )


# ── (c) CSP header contains expected directives ──────────────────────────


def test_csp_contains_pragmatic_script_src():
    gw = SimpleNamespace(port=18790, allow_origins=[])
    csp = build_csp(gw)
    # Phase 1a pragmatic: 'unsafe-eval' is documented and required for stdlib Alpine.
    assert "script-src 'self' 'unsafe-eval'" in csp
    assert "default-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp


# ── (d) connect-src is NOT scheme-wide ws:/wss: ──────────────────────────


def test_connect_src_not_scheme_wide():
    gw = SimpleNamespace(port=18790, allow_origins=[])
    csp = build_csp(gw)
    m = re.search(r"connect-src ([^;]+);", csp)
    assert m, "connect-src directive missing"
    directive = m.group(1)
    tokens = set(directive.split())
    # Bare scheme tokens would be a hole.
    assert "ws:" not in tokens
    assert "wss:" not in tokens
    # Local-origin WebSocket targets must be present.
    assert f"ws://localhost:{gw.port}" in tokens
    assert f"ws://127.0.0.1:{gw.port}" in tokens
    assert f"ws://[::1]:{gw.port}" in tokens


def test_connect_src_includes_user_allow_origins():
    gw = SimpleNamespace(
        port=18790,
        allow_origins=["http://example.com", "https://app.example.com:9000"],
    )
    csp = build_csp(gw)
    assert "ws://example.com" in csp
    assert "wss://app.example.com:9000" in csp


# ── (e) vendored sha256 matches manifest ─────────────────────────────────


def _parse_versions_md() -> dict[str, str]:
    """Return {filename: sha256} from VERSIONS.md table rows."""
    text = VERSIONS_MD.read_text(encoding="utf-8")
    out = {}
    for line in text.splitlines():
        # Rows look like: | `name.js` | url | version | `sha256` |
        m = re.match(r"\|\s*`([^`]+)`\s*\|.*\|.*\|\s*`([0-9a-f]{64})`\s*\|", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def test_vendored_files_match_versions_manifest():
    expected = _parse_versions_md()
    assert expected, "VERSIONS.md parse returned empty — manifest table changed?"
    for name, want in expected.items():
        path = VENDOR_DIR / name
        assert path.exists(), f"vendored file missing: {name}"
        got = _sha256(path)
        assert got == want, (
            f"{name} sha256 mismatch:\n  manifest: {want}\n  on-disk:  {got}\n"
            f"If this is intentional, update VERSIONS.md."
        )


# ── (f) app.js extracted properly + structural integrity ─────────────────


def test_app_js_exists_and_defines_syll_factory():
    assert APP_JS.exists(), "static/app.js was not created"
    src = APP_JS.read_text(encoding="utf-8")
    assert "function syllApp()" in src, (
        "app.js missing syllApp() factory — extraction may have grabbed wrong range"
    )
    # Sanity: extraction baseline was ~4500 lines (8270-12774 in the original
    # index.html). Phase additions (auth fetch wrapper, MCP tab methods, etc.)
    # are expected to grow this. Wide band catches accidental wholesale rewrite
    # without rejecting legitimate growth.
    line_count = src.count("\n")
    assert 4400 < line_count < 6000, (
        f"app.js line count unexpected: {line_count} "
        "(expected 4400-6000 — extraction baseline + phase growth)"
    )


def test_index_html_loads_app_js_locally():
    text = INDEX_HTML.read_text(encoding="utf-8")
    assert '<script src="/static/app.js"></script>' in text


# ── (g) renderMarkdown is wrapped in DOMPurify.sanitize ──────────────────


def test_render_markdown_is_dompurify_wrapped():
    src = APP_JS.read_text(encoding="utf-8")
    # The sanitize call should appear inside renderMarkdown.
    assert "DOMPurify.sanitize" in src, (
        "renderMarkdown must wrap marked.parse in DOMPurify.sanitize"
    )
    # And renderMarkdown should still call marked.parse (we wrap, not replace).
    assert "marked.parse" in src


def test_dompurify_loaded_before_alpine_in_index():
    text = INDEX_HTML.read_text(encoding="utf-8")
    dom_idx = text.index("dompurify.min.js")
    alpine_idx = text.index("alpinejs.min.js")
    assert dom_idx < alpine_idx, (
        "DOMPurify must load before Alpine so renderMarkdown is callable on first paint"
    )


# ── Sanity: all vendored files referenced by index.html exist ────────────


@pytest.mark.parametrize("relpath", [
    "vendor/d3.min.js",
    "vendor/cal-heatmap.min.js",
    "vendor/cal-heatmap.css",
    "vendor/chart.umd.min.js",
    "vendor/marked.min.js",
    "vendor/highlight.min.js",
    "vendor/github-dark.min.css",
    "vendor/dompurify.min.js",
    "vendor/alpinejs.min.js",
])
def test_vendor_files_referenced_in_index(relpath):
    text = INDEX_HTML.read_text(encoding="utf-8")
    assert f"/static/{relpath}" in text, f"index.html does not reference {relpath}"


# ── Phase 1a review-pass-2: app.js must not contain CDN URLs ─────────────


def test_app_js_has_no_cdn_runtime_dependencies():
    """Theme switcher / runtime helpers in app.js once pointed highlight.js
    CSS at cdnjs; CSP would block these. Phase 1a: every runtime URL must
    be same-origin or a Google Fonts host (Phase 1d work item)."""
    src = APP_JS.read_text(encoding="utf-8")
    forbidden_hosts = (
        "cdnjs.cloudflare.com",
        "cdn.jsdelivr.net",
        "unpkg.com",
        "esm.sh",
        "cdn.skypack.dev",
        "code.jquery.com",
    )
    found = [host for host in forbidden_hosts if host in src]
    assert found == [], (
        f"app.js still references CDN hosts (CSP would block these): {found}. "
        "Vendor any missing assets to syll/web/static/vendor/ and update VERSIONS.md."
    )


def test_light_theme_css_is_vendored():
    """Light-mode toggle must point hljs-theme to /static/vendor/github.min.css."""
    src = APP_JS.read_text(encoding="utf-8")
    assert "/static/vendor/github.min.css" in src
    assert "/static/vendor/github-dark.min.css" in src


def test_admin_token_fetch_wrapper_present_in_app_js():
    """The window.fetch wrapper must be installed before any data load fires."""
    src = APP_JS.read_text(encoding="utf-8")
    assert "_installAuthFetch" in src
    assert "_bootstrapAdminToken" in src
    assert "X-Syll-Admin-Token" in src
    # The bootstrap call must run inside init() before loadStatus.
    init_idx = src.index("init() {")
    boot_idx = src.index("_bootstrapAdminToken")
    load_status_idx = src.index("this.loadStatus()")
    assert init_idx < boot_idx < load_status_idx, (
        "bootstrap must occur inside init() and BEFORE loadStatus() so mutating "
        "fetches can use the token from the first load"
    )


# ── Phase 1a review-pass-2: malicious markdown must be neutralized ───────


def test_malicious_markdown_payloads_rendered_safely():
    """Read the renderMarkdown body from app.js and verify the wrap is
    structurally correct: DOMPurify.sanitize is called BEFORE the result is
    returned, USE_PROFILES.html is on, and the catch arm returns escaped
    text — never raw content (review pass 2: fail-closed)."""
    src = APP_JS.read_text(encoding="utf-8")
    # Locate the renderMarkdown method body.
    render_idx = src.index("renderMarkdown(content)")
    body = src[render_idx:render_idx + 2000]
    # Sanitization is applied.
    assert "DOMPurify.sanitize" in body
    assert "USE_PROFILES" in body
    assert "ADD_ATTR: []" in body
    # Fail-closed: the catch arm escapes and wraps, not raw return.
    assert "return content;" not in body, (
        "review-pass-2 fix: catch arm must not return raw content with x-html"
    )
    assert "&lt;" in body and "&gt;" in body, (
        "catch arm must HTML-escape before returning"
    )
