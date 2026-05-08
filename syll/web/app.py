"""FastAPI application factory for the Syll web frontend."""

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from syll import __version__

STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    config,
    agent_loop,
    session_manager,
    skills_loader,
    memory_store,
    cron_service=None,
) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        config: Syll config object.
        agent_loop: AgentLoop instance for processing messages.
        session_manager: SessionManager instance.
        skills_loader: SkillsLoader instance.
        memory_store: MemoryStore instance.
        cron_service: Optional CronService. If provided, its lifecycle will be
            managed by the FastAPI lifespan, and its on_job / on_complete
            callbacks will be wrapped to broadcast WebSocket events.

    Returns:
        Configured FastAPI app.
    """

    @asynccontextmanager
    async def lifespan(fastapi_app: FastAPI):
        # Boot the admin token once per process. Idempotent across re-entries
        # (e.g., uvicorn reload). `syll wake`'s gateway calls this earlier;
        # the lifespan call is the safety net for `syll web` and tests.
        from syll.web.auth import boot_admin_token, get_admin_token

        if not get_admin_token():
            boot_admin_token()

        # Phase 1c: bring up MCP BEFORE cron — cron callbacks invoke the
        # agent loop, and the agent must already see MCP tools by then.
        # `manage_mcp_lifecycle` is set to True by entry points that don't
        # bring up MCP themselves (e.g. `syll web`); `syll wake` keeps it
        # False because it bootstraps MCP outside the lifespan to surface
        # boot_validate failures before the dashboard takes over.
        mcp_manager = getattr(fastapi_app.state, "mcp_manager", None)
        manage_mcp = getattr(fastapi_app.state, "manage_mcp_lifecycle", False)
        agent_loop = getattr(fastapi_app.state, "agent_loop", None)
        if manage_mcp and mcp_manager is not None:
            try:
                await mcp_manager.boot_validate()
                await mcp_manager.start()
                if agent_loop is not None and hasattr(agent_loop, "reload_mcp_tools"):
                    agent_loop.reload_mcp_tools()
            except Exception as e:
                logger.warning(f"mcp_manager start failed in lifespan: {e}")

        if cron_service:
            try:
                await cron_service.start()
            except Exception as e:
                logger.warning(f"cron_service.start() failed in lifespan: {e}")
        yield
        # Shutdown
        if cron_service:
            try:
                cron_service.stop()
            except Exception as e:
                logger.warning(f"cron_service.stop() failed in lifespan: {e}")
        if manage_mcp and mcp_manager is not None:
            try:
                await asyncio.wait_for(mcp_manager.stop(), timeout=10)
            except Exception as e:
                logger.warning(f"mcp_manager.stop() failed in lifespan: {e}")

    app = FastAPI(
        title="Syll",
        version=__version__,
        description="Syll Web API",
        lifespan=lifespan,
    )

    # ── Middleware order matters ─────────────────────────────────────────
    # Starlette's `add_middleware` prepends; the LAST registered middleware
    # is the OUTERMOST. We want:
    #
    #     CORS (outer)  →  CSP  →  AdminGuard (inner)  →  app
    #
    # so when AdminGuard short-circuits a 401 / 403 (without calling
    # call_next), the response unwinds back through CSP and CORS and picks
    # up Content-Security-Policy, X-Frame-Options, Referrer-Policy, and the
    # CORS Access-Control-* headers. Review pass 4 (Medium): without this
    # order, rejected mutators leaked unhardened responses to browsers.
    #
    # add_middleware order = INNERMOST → OUTERMOST.

    # ── Innermost: AdminGuard ────────────────────────────────────────────
    # Source of truth for mutation auth on every unsafe method on /api/v1/*.
    # Per-route Depends(require_admin) is supplementary. WebSocket handlers
    # must call `websocket_check_admin` explicitly (see routes/chat.py).
    from syll.web.auth import AdminGuardMiddleware
    app.add_middleware(AdminGuardMiddleware)

    # ── Middle: CSP + frame/referrer hardening ──────────────────────────
    from syll.web.csp import CSPMiddleware
    app.add_middleware(CSPMiddleware, gateway=config.gateway)

    # ── Outermost: CORS ─────────────────────────────────────────────────
    # rev. 4 finding H4 / R2: was `allow_origins=["*"]` — replaced with the
    # exact local-origin set computed from gateway.port plus the user's
    # configured allow_origins. CORS sits outside CSP and AdminGuard so
    # preflights and rejections both carry Access-Control-* headers.
    _gw_port = config.gateway.port
    _gw_allow_origins = getattr(config.gateway, "allow_origins", []) or []
    _cors_origins = list({
        f"http://localhost:{_gw_port}",
        f"http://127.0.0.1:{_gw_port}",
        f"http://[::1]:{_gw_port}",
        *_gw_allow_origins,
    })
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*", "X-Syll-Admin-Token"],
    )

    # Store shared objects on app.state
    app.state.config = config
    app.state.agent_loop = agent_loop
    app.state.session_manager = session_manager
    app.state.skills_loader = skills_loader
    app.state.memory_store = memory_store

    # GUI Skill store
    from syll.agent.gui_skill import GUISkillStore

    app.state.gui_skill_store = GUISkillStore(config.workspace_path)

    # Recorded workflow skill store
    from syll.agent.recorded_skill import RecordedSkillStore

    app.state.recorded_skill_store = RecordedSkillStore(config.workspace_path)
    # Backward-compat alias for older integrations that still expect the legacy name.
    app.state.aloha_skill_store = app.state.recorded_skill_store
    from syll.recorder.manager import RecorderManager

    app.state.recorder_manager = RecorderManager()
    app.state.cron_service = cron_service

    # Intent clarifier for the Syll panel. Reuses the live
    # provider from agent_loop so we don't re-init LiteLLM from raw config.
    from syll.agent.intent_clarifier import IntentClarifier

    app.state.intent_clarifier = IntentClarifier(
        getattr(agent_loop, "provider", None)
    )

    # ChannelManager is attached by gateway after create_app; default None for web mode.
    if not hasattr(app.state, "channel_manager"):
        app.state.channel_manager = None

    # ── Voice: ASR provider for /api/v1/voice/asr ───────────
    # TTS registration lives inside AgentLoop so it can expose the
    # ``speak`` tool; ASR lives here because the HTTP route owns the
    # upload path and ffmpeg fallback.
    app.state.asr_provider = None
    voice_cfg = getattr(config, "voice", None)
    if voice_cfg and voice_cfg.enabled:
        asr_cfg = voice_cfg.asr
        try:
            if asr_cfg.provider == "volcengine" and asr_cfg.appid and asr_cfg.access_token:
                from syll.providers.voice_volc import VolcengineASRProvider
                app.state.asr_provider = VolcengineASRProvider(
                    appid=asr_cfg.appid,
                    access_token=asr_cfg.access_token,
                    resource_id=asr_cfg.resource_id or "volc.seedasr.sauc.duration",
                    cluster=asr_cfg.cluster,
                )
                logger.info("asr_provider=volcengine")
            elif asr_cfg.provider == "groq":
                from syll.providers.transcription import GroqTranscriptionProvider
                app.state.asr_provider = GroqTranscriptionProvider(
                    api_key=asr_cfg.access_token or None,
                )
                logger.info("asr_provider=groq")
        except Exception as e:
            logger.warning(f"ASR provider init failed: {e}")

    # ── WebSocket broadcast infrastructure ──────────────────
    app.state.ws_clients = set()

    async def broadcast_ws(event: dict) -> None:
        """Send an event to every connected chat WebSocket client."""
        dead = set()
        for ws in list(app.state.ws_clients):
            try:
                await ws.send_json(event)
            except Exception:
                dead.add(ws)
        app.state.ws_clients -= dead

    app.state.broadcast_ws = broadcast_ws

    # ── Wrap cron callbacks for broadcast ───────────────────
    if cron_service:
        _orig_on_job = cron_service.on_job

        async def _wrapped_on_job(job):
            # Broadcast "running" BEFORE executing (state not yet updated).
            try:
                await broadcast_ws(
                    {
                        "type": "cron_triggered",
                        "job_id": job.id,
                        "job_name": job.name,
                        "status": "running",
                    }
                )
            except Exception as e:
                logger.warning(f"cron broadcast (running) failed: {e}")
            if _orig_on_job:
                return await _orig_on_job(job)
            return None

        cron_service.on_job = _wrapped_on_job

        async def _on_complete(job):
            # Fires AFTER _save_store, so job.state is authoritative.
            from syll.web.streaming import _encode_media

            status = job.state.last_status or "ok"
            media_payload: list[dict] = []
            if status == "ok" and job.state.last_media:
                try:
                    media_payload = _encode_media(list(job.state.last_media))
                except Exception as e:
                    logger.warning(f"cron media encode failed: {e}")
            try:
                await broadcast_ws(
                    {
                        "type": "cron_triggered",
                        "job_id": job.id,
                        "job_name": job.name,
                        "status": status,  # "ok" or "error"
                        "error": job.state.last_error,
                        "next_run_at_ms": job.state.next_run_at_ms,
                        "last_run_at_ms": job.state.last_run_at_ms,
                        "media": media_payload,
                    }
                )
            except Exception as e:
                logger.warning(f"cron broadcast (complete) failed: {e}")

        cron_service.on_complete = _on_complete

    # Register API routes
    from syll.web.auth import router as auth_router
    from syll.web.routes.activity import router as activity_router
    from syll.web.routes.aloha_skills import legacy_router as legacy_aloha_skills_router
    from syll.web.routes.aloha_skills import router as aloha_skills_router
    from syll.web.routes.chat import router as chat_router
    from syll.web.routes.config import router as config_router
    from syll.web.routes.coord import router as coord_router
    from syll.web.routes.cron import router as cron_router
    from syll.web.routes.dashboard import router as dashboard_router
    from syll.web.routes.ghost import legacy_router as ghost_router
    from syll.web.routes.ghost import router as syll_router
    from syll.web.routes.gui_skills import router as gui_skills_router
    from syll.web.routes.intent import router as intent_router
    from syll.web.routes.mcp import router as mcp_router
    from syll.web.routes.memory import router as memory_router
    from syll.web.routes.recorded_skills import router as recorded_skills_router
    from syll.web.routes.recorder import router as recorder_router
    from syll.web.routes.rituals import router as rituals_router
    from syll.web.routes.sessions import router as sessions_router
    from syll.web.routes.skills import router as skills_router
    from syll.web.routes.status import router as status_router
    from syll.web.routes.voice import router as voice_router

    app.state.agent_activity = "idle"
    app.state.agent_activity_detail = ""
    app.state.agent_start_time = time.time()

    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(mcp_router, prefix="/api/v1")
    app.include_router(activity_router, prefix="/api/v1")
    app.include_router(syll_router, prefix="/api/v1")
    app.include_router(ghost_router, prefix="/api/v1")
    app.include_router(chat_router, prefix="/api/v1")
    app.include_router(status_router, prefix="/api/v1")
    app.include_router(config_router, prefix="/api/v1")
    app.include_router(sessions_router, prefix="/api/v1")
    app.include_router(skills_router, prefix="/api/v1")
    app.include_router(memory_router, prefix="/api/v1")
    app.include_router(recorder_router, prefix="/api/v1")
    app.include_router(gui_skills_router, prefix="/api/v1")
    app.include_router(aloha_skills_router, prefix="/api/v1")
    app.include_router(legacy_aloha_skills_router, prefix="/api/v1")
    app.include_router(recorded_skills_router, prefix="/api/v1")
    app.include_router(dashboard_router, prefix="/api/v1")
    app.include_router(coord_router, prefix="/api/v1")
    app.include_router(cron_router, prefix="/api/v1")
    app.include_router(rituals_router, prefix="/api/v1")
    app.include_router(voice_router, prefix="/api/v1")
    app.include_router(intent_router, prefix="/api/v1")

    # Static asset cache-busting: inject `?v=<mtime>-<size>` into known
    # `<script>` and `<link>` `src/href` so the browser always picks up a
    # changed file without a hard refresh. Cheap (one stat() per asset on
    # each `GET /`), and avoids the "I edited app.js but the page still
    # runs old code" trap.
    _CACHE_BUSTED_ASSETS = (
        "/static/app.js",
        "/static/vendor/marked.min.js",
        "/static/vendor/highlight.min.js",
        "/static/vendor/dompurify.min.js",
        "/static/vendor/alpinejs.min.js",
        "/static/vendor/d3.min.js",
        "/static/vendor/cal-heatmap.min.js",
        "/static/vendor/cal-heatmap.css",
        "/static/vendor/chart.umd.min.js",
        "/static/vendor/github-dark.min.css",
        "/static/vendor/github.min.css",
    )

    def _bust_cache(html: str) -> str:
        for url in _CACHE_BUSTED_ASSETS:
            rel = url[len("/static/"):]
            p = STATIC_DIR / rel
            if not p.exists():
                continue
            try:
                stat = p.stat()
                tag = f"{stat.st_mtime_ns}-{stat.st_size}"
            except OSError:
                continue
            # Replace `"/static/<rel>"` → `"/static/<rel>?v=<tag>"`. Quote
            # both sides so we don't match prefix-overlapping URLs (e.g.,
            # `app.js` substring of `app.json`).
            html = html.replace(f'"{url}"', f'"{url}?v={tag}"')
        return html

    # Serve index.html at root with cache-busted asset URLs
    @app.get("/")
    async def index():
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(_bust_cache(html))

    @app.get("/coord-lab")
    async def coord_lab():
        return FileResponse(STATIC_DIR / "coord_lab.html")

    # Static files (CSS, JS if any)
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    return app
