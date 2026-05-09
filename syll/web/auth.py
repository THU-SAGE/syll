"""Web authorization for mutating endpoints (admin token + loopback gate).

Threat model and the layered defenses, from outside in:

  1. Loopback gate — `client.host` must be loopback (127.0.0.1, ::1,
     ::ffff:127.0.0.1, or "localhost") UNLESS `gateway.allow_remote_admin=true`
     is set explicitly. This is the primary boundary against LAN attackers
     when the gateway binds 0.0.0.0.
  2. Admin token — every mutating route requires `X-Syll-Admin-Token` to
     match the on-disk token at `~/.syll/admin_token`. The token is
     created on first boot, persists across restarts (so a long-lived Pet
     UI keeps working), and is rotated only on explicit
     `syll admin token --rotate` or `POST /api/v1/admin-token/rotate`.
     In remote-admin mode it is NOT auto-issued via the web; users obtain
     it via the CLI.
  3. Origin / Sec-Fetch-Site — defends against cross-origin browser-driven
     CSRF even when CORS is mis-configured downstream.

The `AdminGuardMiddleware` enforces (1)-(3) on every unsafe HTTP method on
`/api/v1/*` so individual routes do not need per-route `Depends(require_admin)`
to be the auth boundary; route-level dependencies are still useful for routes
that want a typed return value or a per-route allowlist override, but the
middleware is the source of truth.

WebSocket upgrades (`/api/v1/chat/ws` etc.) appear as HTTP GETs to a typical
middleware so the same guard would not catch them; the WS handler must call
`websocket_check_admin()` explicitly.
"""

from __future__ import annotations

import hmac
import ipaddress
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from syll.config.schema import Config, GatewayConfig

# ── Token storage ─────────────────────────────────────────────────────────

ADMIN_TOKEN_PATH = Path.home() / ".syll" / "admin_token"
_TOKEN_PREFIX = "syll-admin-"
_TOKEN_BYTES = 32


def _read_token() -> str | None:
    try:
        return ADMIN_TOKEN_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.warning(f"could not read {ADMIN_TOKEN_PATH}: {e}")
        return None


def _write_token(token: str) -> None:
    ADMIN_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    ADMIN_TOKEN_PATH.write_text(token + "\n", encoding="utf-8")
    try:
        ADMIN_TOKEN_PATH.chmod(0o600)
    except OSError as e:
        logger.warning(f"could not chmod {ADMIN_TOKEN_PATH}: {e}")


def boot_admin_token(*, force: bool = False) -> str:
    """Ensure an admin token exists on disk; optionally rotate.

    The token persists across restarts so users / scripts that hold a copy
    don't lose access on every reboot. Use `force=True` to rotate
    unconditionally; gateway startup calls with the default `force=False` so
    plain `syll wake` / `syll web` doesn't churn the token (and break the
    running Pet UI's cached value).

    Returning the token is intentional — `syll wake` may log a fingerprint
    without printing the value.
    """
    existing = _read_token()
    if existing and not force:
        return existing
    token = _TOKEN_PREFIX + secrets.token_urlsafe(_TOKEN_BYTES)
    _write_token(token)
    if existing:
        logger.info(f"admin token rotated at {ADMIN_TOKEN_PATH}")
    else:
        logger.info(f"admin token initialized at {ADMIN_TOKEN_PATH}")
    return token


def rotate_admin_token() -> str:
    """Force-regenerate the on-disk token.

    Used by `syll admin token --rotate` and POST /api/v1/admin-token/rotate to
    invalidate any leaked previous token.
    """
    return boot_admin_token(force=True)


def get_admin_token() -> str | None:
    """Read the current token without modifying it."""
    return _read_token()


# ── Loopback / Origin checks ──────────────────────────────────────────────


def _is_loopback(host: str | None) -> bool:
    """True iff `host` resolves to a loopback address.

    Handles IPv4 (127.0.0.1), IPv6 (::1), IPv4-mapped IPv6 (::ffff:127.0.0.1),
    and the literal string "localhost". Returns False for malformed input.

    Version-stable: explicitly unwraps `ipv4_mapped` so the IPv4-mapped IPv6
    case (`::ffff:127.0.0.1`) is treated as loopback on Python 3.11/3.12 too,
    not just 3.13+ where `ip_address(...).is_loopback` happens to handle it
    directly. See https://docs.python.org/3/library/ipaddress.html#ipaddress.IPv6Address.ipv4_mapped
    """
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address):
        mapped = addr.ipv4_mapped
        if mapped is not None:
            return mapped.is_loopback
    return addr.is_loopback


def _local_origins(port: int) -> set[str]:
    return {
        f"http://localhost:{port}",
        f"http://127.0.0.1:{port}",
        f"http://[::1]:{port}",
    }


def _check_origin(request: Request, gw: GatewayConfig, *, require_explicit_allowlist: bool) -> None:
    """Reject mismatched / missing Origin on browser-driven requests.

    `Sec-Fetch-Site: same-origin` is accepted as a substitute when Origin is
    absent (e.g., some GETs in older browsers). Mutating fetch() always sends
    Origin, so a missing Origin on POST/PUT/DELETE/PATCH is rejected.
    """
    origin = request.headers.get("Origin")
    sec_fetch_site = request.headers.get("Sec-Fetch-Site")
    if not origin:
        if request.method == "GET" and sec_fetch_site in (None, "same-origin"):
            return
        raise HTTPException(403, "Origin header required for mutating requests")

    if require_explicit_allowlist:
        if origin not in (gw.allow_origins or []):
            raise HTTPException(403, f"origin {origin!r} not in gateway.allow_origins")
        return

    allowed = _local_origins(gw.port) | set(gw.allow_origins or [])
    if origin not in allowed:
        raise HTTPException(403, f"origin {origin!r} not allowed")


# ── Dependency: require_admin ─────────────────────────────────────────────


def require_admin(request: Request) -> None:
    """FastAPI dependency for every mutating MCP / admin route.

    Layered enforcement: token first (constant-time compare), then origin,
    then loopback gate. Any failure short-circuits with 401/403. The token
    is read from disk on every request so `syll admin token --rotate` takes
    effect without restart.
    """
    cfg: Config = request.app.state.config
    gw = cfg.gateway

    presented = request.headers.get("X-Syll-Admin-Token") or ""
    expected = _read_token() or ""
    if not expected:
        raise HTTPException(503, "admin token not initialized")
    if not hmac.compare_digest(presented, expected):
        raise HTTPException(401, "admin token required")

    client_host = request.client.host if request.client else None
    is_loopback = _is_loopback(client_host)

    _check_origin(request, gw, require_explicit_allowlist=not is_loopback)

    if not is_loopback and not gw.allow_remote_admin:
        raise HTTPException(
            403,
            "remote admin disabled; set gateway.allow_remote_admin=true and "
            "configure gateway.allow_origins on the host",
        )


# ── Routes ────────────────────────────────────────────────────────────────


router = APIRouter()


@router.get("/admin-token")
async def get_admin_token_route(request: Request):
    """Return the admin token to the local Pet UI.

    ALWAYS loopback-only, regardless of `allow_remote_admin`. Remote operators
    must obtain the token via `syll admin token` and paste it into the UI —
    we never auto-issue tokens to non-loopback callers because the entire
    LAN is potentially same-origin once the gateway binds 0.0.0.0.
    """
    cfg: Config = request.app.state.config
    gw = cfg.gateway

    client_host = request.client.host if request.client else None
    if not _is_loopback(client_host):
        raise HTTPException(
            403,
            "admin token only retrievable from loopback; "
            "use `syll admin token` on the host machine",
        )
    _check_origin(request, gw, require_explicit_allowlist=False)

    token = _read_token()
    if not token:
        raise HTTPException(503, "admin token not initialized")
    return {"token": token}


@router.post("/admin-token/rotate", dependencies=[Depends(require_admin)])
async def rotate_admin_token_route():
    """Force-rotate the on-disk token. Caller must already hold the current one."""
    new_token = rotate_admin_token()
    return {"token": new_token, "rotated": True}


# ── Global middleware: gate every unsafe method on /api/v1/* ──────────────
#
# Per-route Depends(require_admin) is easy to forget, and a 40+ mutating-route
# surface area makes any forgotten one a potential bypass. This middleware is
# the source of truth: every POST/PUT/PATCH/DELETE under `/api/v1/*` runs the
# same checks as `require_admin` before reaching the route handler.

# Routes that are intentionally publicly callable on unsafe methods.
# Currently empty — every mutation today ought to require admin. Add an
# entry here only with explicit threat-model rationale.
_UNSAFE_PUBLIC_ALLOWLIST: set[tuple[str, str]] = set()  # {("POST", "/api/v1/foo")}

_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class AdminGuardMiddleware(BaseHTTPMiddleware):
    """Apply require_admin checks to every unsafe HTTP method on /api/v1/*."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        method = request.method.upper()
        if method in _UNSAFE_METHODS and path.startswith("/api/v1/"):
            if (method, path) not in _UNSAFE_PUBLIC_ALLOWLIST:
                try:
                    require_admin(request)
                except HTTPException as e:
                    return JSONResponse(
                        {"detail": e.detail},
                        status_code=e.status_code,
                        headers=getattr(e, "headers", None) or {},
                    )
        return await call_next(request)


# ── WebSocket admin gate ──────────────────────────────────────────────────
#
# WS upgrades arrive as HTTP GETs at the middleware layer, so the unsafe-method
# guard does not cover them. Call this from inside any WS handler that should
# not be reachable from the LAN by an unauthenticated client.


async def websocket_check_admin(websocket) -> bool:
    """Return True if the WS connection is allowed; otherwise close it.

    Threat model and auth priority:

      1. Loopback caller WITH LOCAL Origin header — allowed without
         a token. This is the primary path: the Pet UI in the user's
         browser opens `ws://127.0.0.1:<port>/api/v1/chat/ws` with
         Origin=`http://localhost:<port>` (or 127.0.0.1 / [::1]).
      2. Loopback caller with NO Origin header — allowed (CLI tools like
         wscat don't send Origin; same-host code execution is already a
         lost cause for any token-based defense).
      3. Loopback caller with FOREIGN Origin — REJECTED. Closes the
         browser-origin RCE class: a malicious page at
         http://attacker.example can resolve `127.0.0.1` and open a WS
         from the user's browser, and `client.host` would still be
         loopback. Only the Origin header tells us which page is driving
         the connection.
      4. Loopback caller with a NON-LOCAL but allowlisted Origin — treated
         like remote admin: requires `gateway.allow_remote_admin=true`, a
         valid admin token, and the Origin in `gateway.allow_origins`.
      5. Non-loopback caller — requires `gateway.allow_remote_admin=true`,
         a valid admin token (in `Sec-WebSocket-Protocol` header or
         `?token=` query), and an Origin in `gateway.allow_origins`.
    """
    cfg = getattr(getattr(websocket.app, "state", None), "config", None)
    client = getattr(websocket, "client", None)
    client_host = client.host if client else None
    origin = (
        websocket.headers.get("Origin")
        if hasattr(websocket, "headers")
        else None
    )

    if cfg is None:
        # Test harnesses sometimes stub websocket.app.state without a Config;
        # fall through to a loopback-only policy in that case.
        if _is_loopback(client_host):
            return True
        await websocket.close(code=1008, reason="config unavailable")
        return False

    gw = cfg.gateway

    def _presented_ws_token() -> str:
        header_value = websocket.headers.get("Sec-WebSocket-Protocol") or ""
        # Browsers may send a comma-separated subprotocol list. Treat any item
        # as the candidate token so callers can use either query or subprotocol.
        header_tokens = [p.strip() for p in header_value.split(",") if p.strip()]
        query_token = websocket.query_params.get("token") or ""
        return query_token or (header_tokens[0] if header_tokens else "")

    async def _require_remote_ws_admin(
        *, missing_origin_reason: str = "Origin header required"
    ) -> bool:
        if not gw.allow_remote_admin:
            await websocket.close(code=1008, reason="remote admin disabled")
            return False
        if origin is None:
            await websocket.close(code=1008, reason=missing_origin_reason)
            return False
        allowed = set(gw.allow_origins or [])
        if origin not in allowed:
            await websocket.close(code=1008, reason=f"origin {origin!r} not allowed")
            return False
        expected = _read_token() or ""
        presented = _presented_ws_token()
        if not expected or not hmac.compare_digest(presented, expected):
            await websocket.close(code=1008, reason="admin token required")
            return False
        return True

    if _is_loopback(client_host):
        # Even loopback connections must clear the Origin gate — closes the
        # browser-origin WS bypass (review pass 4 critical).
        if origin is not None:
            if origin in _local_origins(gw.port):
                return True
            # Non-local browser origins are not allowed to become tokenless
            # auth just because the TCP peer is 127.0.0.1. Treat them as
            # remote-admin callers: explicit opt-in + allowlist + token.
            return await _require_remote_ws_admin()
        # Origin absent → CLI tool / native client; loopback is sufficient.
        return True

    # Non-loopback path
    return await _require_remote_ws_admin()
