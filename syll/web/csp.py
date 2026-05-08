"""Content-Security-Policy middleware (Phase 1a — pragmatic posture).

Pragmatic stance: vendored scripts close the CDN supply-chain hole (rev. 4
finding C2), but standard Alpine 3 needs `'unsafe-eval'` because its
expression evaluator uses Function() under the hood. Index.html has many
complex object-literal `x-data` blocks (e.g., index.html:5458+) that the
@alpinejs/csp build's restricted grammar does not accept; migrating to that
build is a non-mechanical rewrite scoped as Phase 1c.

Google Fonts CSS lives on `fonts.googleapis.com` and the woff2 files on
`fonts.gstatic.com`; they are allowed here pending Phase 1d (vendor fonts).

`connect-src` is intentionally NOT scheme-wide for `ws:` / `wss:` — only the
exact local-origin WebSocket targets plus user-configured `gateway.allow_origins`
are whitelisted. This shuts the future-XSS WebSocket exfil channel.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware

from syll.config.schema import GatewayConfig


def _to_ws(origin: str) -> str:
    """http(s)://host:port -> ws(s)://host:port."""
    if origin.startswith("https://"):
        return "wss://" + origin[len("https://") :]
    if origin.startswith("http://"):
        return "ws://" + origin[len("http://") :]
    return origin


def _ws_origins(gw: GatewayConfig) -> list[str]:
    port = gw.port
    base = [
        f"ws://localhost:{port}",
        f"ws://127.0.0.1:{port}",
        f"ws://[::1]:{port}",
    ]
    # `getattr` default tolerates test stubs that pre-date the new field.
    base.extend(_to_ws(o) for o in (getattr(gw, "allow_origins", []) or []))
    return base


def build_csp(gw: GatewayConfig) -> str:
    ws_origins = " ".join(_ws_origins(gw))
    return (
        "default-src 'self'; "
        # 'unsafe-eval' required by standard Alpine 3 (Phase 1a). Phase 1c
        # migrates to @alpinejs/csp and drops this directive.
        "script-src 'self' 'unsafe-eval'; "
        # Google Fonts CSS (Phase 1d will vendor and drop the host).
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "img-src 'self' data: blob:; "
        f"connect-src 'self' {ws_origins}; "
        # Google Fonts woff2 (Phase 1d).
        "font-src 'self' data: https://fonts.gstatic.com; "
        "object-src 'none'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )


class CSPMiddleware(BaseHTTPMiddleware):
    """Apply CSP + tightening headers to every response."""

    def __init__(self, app, gateway: GatewayConfig):
        super().__init__(app)
        self.policy = build_csp(gateway)

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = self.policy
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response
