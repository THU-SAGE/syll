"""CLI commands for Syll."""

import asyncio
from pathlib import Path

import typer
from loguru import logger
from rich.console import Console
from rich.table import Table

from syll import __logo__, __version__

app = typer.Typer(
    name="syll",
    help=f"{__logo__} Syll - a small, self-hosted AI companion",
    no_args_is_help=True,
)

console = Console()


def version_callback(value: bool):
    if value:
        console.print(f"{__logo__} Syll v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        None, "--version", "-v", callback=version_callback, is_eager=True
    ),
):
    """Syll — a small, self-hosted AI companion."""
    pass


# ============================================================================
# Onboard / Setup
# ============================================================================


def _print_legacy_migration_notice(migrated_path: Path) -> None:
    """Print a one-time notice when ~/.nanobot/ was migrated to ~/.syll/."""
    console.print(
        f"[yellow]Migrated your workspace from ~/.nanobot/ to {migrated_path}.[/yellow]"
    )
    console.print(
        "[dim]The old directory is kept as an automatic backup. Delete it when "
        "you're sure everything works: rm -rf ~/.nanobot[/dim]"
    )


@app.command()
def onboard():
    """Initialize Syll configuration and workspace."""
    from syll.config.loader import get_config_path, migrate_legacy_workspace, save_config
    from syll.config.schema import Config
    from syll.utils.helpers import get_workspace_path

    # Non-destructive: copy ~/.nanobot → ~/.syll on first run if applicable
    migrated = migrate_legacy_workspace()
    if migrated is not None:
        _print_legacy_migration_notice(migrated)

    config_path = get_config_path()

    if config_path.exists():
        console.print(f"[yellow]Config already exists at {config_path}[/yellow]")
        if not typer.confirm("Overwrite?"):
            raise typer.Exit()

    # Create default config
    config = Config()
    save_config(config)
    console.print(f"[green]✓[/green] Created config at {config_path}")

    # Create workspace
    workspace = get_workspace_path()
    console.print(f"[green]✓[/green] Created workspace at {workspace}")

    # Create default bootstrap files
    _create_workspace_templates(workspace)

    console.print(f"\n{__logo__} Syll is ready!")
    console.print("\nNext steps:")
    console.print("  1. Add your API key to [cyan]~/.syll/config.json[/cyan]")
    console.print("     Get one at: https://openrouter.ai/keys")
    console.print("  2. Chat: [cyan]syll agent -m \"Hello!\"[/cyan]")
    console.print("\n[dim]Want Telegram/WhatsApp? See: https://github.com/THU-SAGE/syll#-chat-apps[/dim]")




def _create_workspace_templates(workspace: Path, verbose: bool = True):
    """Copy missing template files from the shipped template tree into the
    user workspace. Never overwrites existing files — user customizations
    are preserved. Safe to call on every startup (idempotent)."""
    from syll.templates import WORKSPACE_TEMPLATE_ROOT

    if not WORKSPACE_TEMPLATE_ROOT.exists():
        logger.warning(f"Template tree not found at {WORKSPACE_TEMPLATE_ROOT}")
        return

    created: list[str] = []
    _copy_tree_missing_only(WORKSPACE_TEMPLATE_ROOT, workspace, created)

    if verbose and created:
        for rel in created:
            console.print(f"  [dim]Created {rel}[/dim]")

    # Also ensure the memory directory + MEMORY.md exist (these are not in
    # the template tree because MEMORY.md is user-generated over time).
    memory_dir = workspace / "memory"
    memory_dir.mkdir(exist_ok=True)
    memory_file = memory_dir / "MEMORY.md"
    if not memory_file.exists():
        memory_file.write_text("""# Long-term Memory

This file stores important information that should persist across sessions.

## User Information

(Important facts about the user)

## Preferences

(User preferences learned over time)

## Important Notes

(Things to remember)
""")
        if verbose:
            console.print("  [dim]Created memory/MEMORY.md[/dim]")


def _copy_tree_missing_only(src: Path, dst: Path, created: list[str], _base: Path | None = None) -> None:
    """Recursively copy every file under `src` into `dst` only if the
    destination does not already exist. Appends each created file's
    path (relative to `dst`) to the `created` list for logging."""
    base = _base or dst
    dst.mkdir(parents=True, exist_ok=True)
    for entry in sorted(src.iterdir()):
        target = dst / entry.name
        if entry.is_dir():
            _copy_tree_missing_only(entry, target, created, base)
        elif entry.is_file():
            if not target.exists():
                target.write_text(entry.read_text(encoding="utf-8"), encoding="utf-8")
                try:
                    rel = target.relative_to(base)
                except ValueError:
                    rel = target
                created.append(str(rel))


def _migrate_workspace_templates(workspace: Path) -> None:
    """Ensure new template files are present in an existing workspace.
    Called on every gateway/web startup. Missing-only; never overwrites
    existing files. Silent except for log lines on creation."""
    from syll.templates import WORKSPACE_TEMPLATE_ROOT

    if not WORKSPACE_TEMPLATE_ROOT.exists():
        return

    created: list[str] = []
    _copy_tree_missing_only(WORKSPACE_TEMPLATE_ROOT, workspace, created)
    if created:
        logger.info(f"Workspace migration created {len(created)} new file(s): {created}")


# ============================================================================
# Gateway / Server
# ============================================================================


@app.command()
def wake(
    port: int = typer.Option(18790, "--port", "-p", help="Listening port"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    enable_memory_agent: bool = typer.Option(False, "--memory-agent", help="Enable Memory Agent"),
    enable_monitor_agent: bool = typer.Option(False, "--monitor-agent", help="Enable Monitor Agent"),
):
    """Wake the ghost — bring syll to life (agent loop, channels, cron, heartbeat, web UI, dashboard)."""
    from syll.agent.loop import AgentLoop
    from syll.bus.queue import MessageBus
    from syll.channels.manager import ChannelManager
    from syll.config.loader import get_data_dir, load_config, migrate_legacy_workspace
    from syll.cron.service import CronService
    from syll.cron.types import CronJob
    from syll.heartbeat.service import HeartbeatService
    from syll.providers.litellm_provider import LiteLLMProvider

    if verbose:
        import logging
        logging.basicConfig(level=logging.DEBUG)

    # Non-destructive upgrade path: copy ~/.nanobot → ~/.syll if the new
    # directory is missing. Runs before anything reads config.
    _migrated = migrate_legacy_workspace()
    if _migrated is not None:
        _print_legacy_migration_notice(_migrated)

    from syll.cli.splash import run_splash

    run_splash()

    config = load_config()

    # Missing-only migration: ensure shipped template files (IDENTITY.md,
    # lore/fragments.md, etc.) are present. Safe to call on every start.
    _migrate_workspace_templates(config.workspace_path)

    # Create components
    bus = MessageBus()

    # Create provider (supports OpenRouter, Anthropic, OpenAI, Bedrock)
    chat = config.models.chat
    model = chat.model
    api_key = chat.api_key
    api_base = chat.api_base
    is_bedrock = model.startswith("bedrock/")

    # Boot report is grouped into three named sections:
    #   runtime  — how syll is thinking
    #   services — what it's listening to / running
    #   endpoints — where the user can reach it
    runtime_rows: list[tuple[str, str, str, str]] = []
    services_rows: list[tuple[str, str, str, str]] = []
    endpoints_rows: list[tuple[str, str, str, str]] = []

    if api_key or is_bedrock:
        runtime_rows.append(("ok", "model", model, "bedrock" if is_bedrock else "api key set"))
    else:
        runtime_rows.append(("warn", "model", model, "no api key — set models.chat in Pet tab"))

    provider = LiteLLMProvider(
        api_key=api_key,
        api_base=api_base,
        default_model=model
    )

    # Create cron service first (callback set after agent creation)
    cron_store_path = get_data_dir() / "cron" / "jobs.json"
    cron = CronService(cron_store_path)

    # Create agent with cron service
    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=model,
        max_iterations=config.agents.defaults.max_tool_iterations,
        brave_api_key=config.tools.web.search.api_key or None,
        exec_config=config.tools.exec,
        cron_service=cron,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        gui_config=config.tools.gui,
        syll_config=config,
    )

    # Set cron callback (needs agent)
    async def on_cron_job(job: CronJob) -> str | None:
        """Execute a cron job through the agent.

        Ritual-aware: jobs with metadata.action_type=='ritual' honor the
        runtime kill switch (identity.rituals_enabled) and have their
        message substituted at fire time so profile renames propagate
        without reinstalling the job.
        """
        is_ritual = (job.payload.metadata or {}).get("action_type") == "ritual"

        # Runtime kill switch for proactive rituals (hot-reloaded via /identity)
        if is_ritual and not agent.context.identity.rituals_enabled:
            logger.debug(f"Ritual '{job.name}' skipped: identity.rituals_enabled is False")
            return None

        # Substitute {{ghost_name}} / {{user_name}} at fire time
        prompt = agent.context.substitute(job.payload.message)

        result = await agent.process_direct(
            prompt,
            session_key=f"cron:{job.id}",
            channel=job.payload.channel or "cli",
            chat_id=job.payload.to or "direct",
        )

        # Only publish non-empty responses — rituals rely on being able to
        # stay silent by returning empty.
        if (
            job.payload.deliver
            and job.payload.to
            and result.text
            and result.text.strip()
        ):
            from syll.bus.events import OutboundMessage
            await bus.publish_outbound(OutboundMessage(
                channel=job.payload.channel or "cli",
                chat_id=job.payload.to,
                content=result.text.strip(),
                media=list(result.media),
            ))
        # Return the full AgentResult so CronService can persist media
        # onto job.state.last_media for cron_triggered broadcasts.
        return result
    cron.on_job = on_cron_job

    # Create heartbeat service
    async def on_heartbeat(prompt: str) -> str:
        """Execute heartbeat through the agent."""
        result = await agent.process_direct(prompt, session_key="heartbeat")
        return result.text

    heartbeat = HeartbeatService(
        workspace=config.workspace_path,
        on_heartbeat=on_heartbeat,
        interval_s=30 * 60,  # 30 minutes
        enabled=True
    )

    # Create channel manager
    channels = ChannelManager(config, bus)

    cron_status = cron.status()
    if cron_status["jobs"] > 0:
        runtime_rows.append(("ok", "cron", f"{cron_status['jobs']} scheduled jobs", ""))
    else:
        runtime_rows.append(("off", "cron", "idle", "no jobs scheduled"))

    runtime_rows.append(("ok", "heartbeat", "every 30m", "keeps the ghost warm"))

    if channels.enabled_channels:
        services_rows.append(("ok", "channels", ", ".join(channels.enabled_channels), ""))
    else:
        services_rows.append(("warn", "channels", "none", "configure in ~/.syll/config.json"))

    # Create Memory Agent (optional)
    memory_agent = None
    if enable_memory_agent:
        from syll.agent.memory_agent import MemoryAgent
        memory_agent = MemoryAgent(provider, config.workspace_path, model)
        services_rows.append(("ok", "memory", "on", "every 60m"))
    else:
        services_rows.append(("off", "memory", "off", "--memory-agent to enable"))

    # Create Monitor Agent (optional)
    monitor_agent = None
    if enable_monitor_agent:
        from syll.agent.monitor_agent import MonitorAgent
        monitor_agent = MonitorAgent(provider, config.workspace_path, bus, model, interval_minutes=5)
        services_rows.append(("ok", "monitor", "on", "every 5m"))
    else:
        services_rows.append(("off", "monitor", "off", "--monitor-agent to enable"))

    endpoints_rows.append(("endpoint", "web ui", f"http://localhost:{config.gateway.port}", "dashboard + Pet tab"))

    sections = [
        ("runtime", runtime_rows),
        ("services", services_rows),
        ("endpoints", endpoints_rows),
    ]

    async def handle_ask(text: str) -> str:
        """Invoked when the user submits in the dashboard's input box."""
        result = await agent.process_direct(text, session_key="cli:dashboard")
        return result.text or "(empty response)"

    from syll.cli.dashboard import (
        DashboardApp,
        attach_loguru_to_dashboard,
        detach_loguru_from_dashboard,
    )

    dashboard = DashboardApp(
        title="syll · awake",
        subtitle=f"v{__version__}  ·  a small ghost at the edge of your screen",
        sections=sections,
        footer="Ctrl+C rest  ·  Ctrl+L clear  ·  Ctrl+K focus input",
        on_ask=handle_ask,
    )
    # Route loguru through the dashboard so service logs appear in the
    # right-hand activity pane instead of leaking behind the alt-screen.
    attach_loguru_to_dashboard(dashboard, level="DEBUG" if verbose else "INFO")

    async def run():
        # v3: create_app MUST come before cron.start() so the broadcast
        # wrapping is installed before any job can fire.
        import uvicorn

        from syll.web.app import create_app
        web_app = create_app(
            config=config,
            agent_loop=agent,
            session_manager=agent.sessions,
            skills_loader=agent.context.skills,
            memory_store=agent.context.memory,
            cron_service=cron,
        )
        # v3: attach channel_manager so POST /api/v1/cron/jobs can validate deliver
        web_app.state.channel_manager = channels

        await cron.start()
        await heartbeat.start()

        web_config = uvicorn.Config(
            web_app,
            host=config.gateway.host,
            port=config.gateway.port,
            log_level="warning",
        )
        web_server = uvicorn.Server(web_config)

        # Spin services up as background tasks. The dashboard is the
        # "main" foreground — when it exits (Ctrl+C inside the TUI),
        # we cancel everything else.
        service_coros = [
            agent.run(),
            channels.start_all(),
            web_server.serve(),
        ]
        if memory_agent:
            service_coros.append(memory_agent.run_periodic())
        if monitor_agent:
            service_coros.append(monitor_agent.run_periodic())

        service_tasks = [asyncio.create_task(c) for c in service_coros]

        try:
            await dashboard.run_async()
        finally:
            # First: silence loguru so shutdown-time logs don't chase the
            # widget that just disappeared. action_quit already calls this,
            # but it's idempotent — safe to do again on any exit path.
            detach_loguru_from_dashboard()
            # Tell long-lived services to wind down.
            heartbeat.stop()
            cron.stop()
            agent.stop()
            web_server.should_exit = True
            try:
                await asyncio.wait_for(channels.stop_all(), timeout=3)
            except Exception:
                pass
            for task in service_tasks:
                task.cancel()
            await asyncio.gather(*service_tasks, return_exceptions=True)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    # Terminal is restored here; the dashboard + loguru sinks are already
    # detached in the async finally block. Print one warm line and exit.
    console.print()
    console.print("  [italic #a58a7a]╴ syll rests for now — wake with[/italic #a58a7a] [bold #edbe8e]syll wake[/bold #edbe8e] [italic #a58a7a]when you're ready ╶[/italic #a58a7a]")
    console.print()


@app.command("gateway", hidden=True)
def gateway(
    port: int = typer.Option(18790, "--port", "-p", help="Listening port"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    enable_memory_agent: bool = typer.Option(False, "--memory-agent"),
    enable_monitor_agent: bool = typer.Option(False, "--monitor-agent"),
):
    """Deprecated alias for ``syll wake`` — kept for muscle memory."""
    wake(
        port=port,
        verbose=verbose,
        enable_memory_agent=enable_memory_agent,
        enable_monitor_agent=enable_monitor_agent,
    )


# ============================================================================
# Agent Commands
# ============================================================================


@app.command()
def agent(
    message: str = typer.Option(None, "--message", "-m", help="Message to send to the agent"),
    session_id: str = typer.Option("cli:default", "--session", "-s", help="Session ID"),
):
    """Interact with the agent directly."""
    from syll.agent.loop import AgentLoop
    from syll.bus.queue import MessageBus
    from syll.config.loader import load_config
    from syll.providers.litellm_provider import LiteLLMProvider

    config = load_config()

    chat = config.models.chat
    model = chat.model
    api_key = chat.api_key
    api_base = chat.api_base
    is_bedrock = model.startswith("bedrock/")

    if not api_key and not is_bedrock:
        console.print("[red]Error: No API key configured.[/red]")
        raise typer.Exit(1)

    bus = MessageBus()
    provider = LiteLLMProvider(
        api_key=api_key,
        api_base=api_base,
        default_model=model
    )

    agent_loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        brave_api_key=config.tools.web.search.api_key or None,
        exec_config=config.tools.exec,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        gui_config=config.tools.gui,
        syll_config=config,
    )

    if message:
        # Single message mode
        async def run_once():
            result = await agent_loop.process_direct(message, session_id)
            console.print(f"\n{__logo__} {result.text}")
            if result.media:
                console.print(f"[dim]media: {', '.join(result.media)}[/dim]")

        asyncio.run(run_once())
    else:
        # Interactive mode
        console.print(f"{__logo__} Interactive mode (Ctrl+C to exit)\n")

        async def run_interactive():
            while True:
                try:
                    user_input = console.input("[bold blue]You:[/bold blue] ")
                    if not user_input.strip():
                        continue

                    result = await agent_loop.process_direct(user_input, session_id)
                    console.print(f"\n{__logo__} {result.text}\n")
                    if result.media:
                        console.print(f"[dim]media: {', '.join(result.media)}[/dim]\n")
                except KeyboardInterrupt:
                    console.print("\nGoodbye!")
                    break

        asyncio.run(run_interactive())


# ============================================================================
# Web Command
# ============================================================================


@app.command()
def web(
    port: int = typer.Option(None, "--port", "-p", help="Web server port"),
    host: str = typer.Option(None, "--host", help="Web server host"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
):
    """Start the Syll web UI."""
    import uvicorn

    from syll.agent.loop import AgentLoop
    from syll.bus.queue import MessageBus
    from syll.config.loader import get_data_dir, load_config, migrate_legacy_workspace
    from syll.cron.service import CronService
    from syll.cron.types import CronJob
    from syll.providers.litellm_provider import LiteLLMProvider
    from syll.web.app import create_app

    if verbose:
        import logging
        logging.basicConfig(level=logging.DEBUG)

    # Non-destructive upgrade path: copy ~/.nanobot → ~/.syll if the new
    # directory is missing. Runs before anything reads config.
    _migrated = migrate_legacy_workspace()
    if _migrated is not None:
        _print_legacy_migration_notice(_migrated)

    config = load_config()
    _migrate_workspace_templates(config.workspace_path)
    host = host or config.gateway.host
    port = port or config.gateway.port

    chat = config.models.chat
    model = chat.model
    api_key = chat.api_key
    api_base = chat.api_base
    is_bedrock = model.startswith("bedrock/")

    web_runtime_rows: list[tuple[str, str, str, str]] = []
    if api_key or is_bedrock:
        web_runtime_rows.append(("ok", "model", model, "bedrock" if is_bedrock else "api key set"))
    else:
        web_runtime_rows.append(("warn", "model", model, "no api key — chat disabled"))

    bus = MessageBus()
    provider = LiteLLMProvider(
        api_key=api_key,
        api_base=api_base,
        default_model=model,
    )
    agent_loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=model,
        brave_api_key=config.tools.web.search.api_key or None,
        exec_config=config.tools.exec,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        gui_config=config.tools.gui,
        syll_config=config,
    )

    # Create cron service for GUI skill scheduling
    cron_store_path = get_data_dir() / "cron" / "jobs.json"
    cron = CronService(cron_store_path)

    async def on_cron_job(job: CronJob):
        # Return the full AgentResult so CronService._execute_job can
        # persist any media produced during the turn onto
        # job.state.last_media for downstream broadcasts.
        return await agent_loop.process_direct(
            job.payload.message,
            session_key=f"cron:{job.id}",
            channel=job.payload.channel or "cli",
            chat_id=job.payload.to or "direct",
        )
    cron.on_job = on_cron_job

    web_app = create_app(
        config=config,
        agent_loop=agent_loop,
        session_manager=agent_loop.sessions,
        skills_loader=agent_loop.context.skills,
        memory_store=agent_loop.context.memory,
        cron_service=cron,
    )

    from syll.cli.banner import configure_warm_logging, render_boot_report
    from syll.cli.splash import run_splash

    run_splash()

    configure_warm_logging(level="DEBUG" if verbose else "INFO")
    render_boot_report(
        console,
        title="syll · web",
        subtitle=f"v{__version__}  ·  dashboard + Pet tab",
        sections=[
            ("runtime", web_runtime_rows),
            ("endpoints", [("endpoint", "web ui", f"http://{host}:{port}", "")]),
        ],
        footer="press Ctrl+C to stop",
    )

    uvicorn.run(web_app, host=host, port=port, log_level="info")


# ============================================================================
# Splash Screen
# ============================================================================


@app.command()
def splash():
    """Show the TUI splash screen (ghost ASCII art)."""
    from syll.cli.splash import run_splash

    run_splash()


# ============================================================================
# Ghost Desktop Mascot
# ============================================================================


def _launch_ghost_desktop(port: int | None) -> None:
    """Shared launcher for the desktop ghost commands."""
    try:
        from syll.desktop.ghost import run_ghost
    except ImportError as e:
        console.print(
            f"[red]Missing dependency:[/red] {e}\n"
            "[dim]Install with: pip install PyQt6 PyQt6-WebEngine[/dim]"
        )
        raise typer.Exit(1)

    if port is None:
        from syll.config.loader import load_config

        try:
            config = load_config()
            port = config.gateway.port
        except Exception:
            port = None

    from syll.cli.banner import configure_warm_logging, render_boot_report
    from syll.cli.splash import run_splash

    run_splash()

    configure_warm_logging()
    if port:
        connection_rows = [("ok", "poll", f"http://localhost:{port}", "live syll connection")]
    else:
        connection_rows = [("off", "poll", "standalone", "no syll gateway running")]

    render_boot_report(
        console,
        title="syll · desktop",
        subtitle=f"v{__version__}  ·  a small ghost on your screen edge",
        sections=[("connection", connection_rows)],
        footer="close the window or press Ctrl+C to quit",
    )

    run_ghost(port=port)


@app.command()
def ghost(
    port: int = typer.Option(None, "--port", "-p", help="Syll web server port to poll"),
):
    """Launch the desktop ghost mascot (PyQt6)."""
    _launch_ghost_desktop(port)


@app.command("syll", hidden=True)
def syll_desktop(
    port: int = typer.Option(None, "--port", "-p", help="Syll web server port to poll"),
):
    """Hidden alias for the desktop ghost mascot."""
    _launch_ghost_desktop(port)


# ============================================================================
# Recorder
# ============================================================================


@app.command()
def record(
    project: str = typer.Argument(help="Project name used for the capture folder and files"),
    output_dir: str = typer.Option(
        None,
        "--output",
        "-o",
        help="Output directory (default: ~/.syll/recordings/{project})",
    ),
    fps: int = typer.Option(15, "--fps", help="Video capture frame rate"),
    monitor: int = typer.Option(0, "--monitor", "-m", help="Monitor index to record"),
):
    """Record screen + input events for the workflow capture pipeline."""
    from syll.recorder.core import RecordingSession
    from syll.recorder.ui import run_recorder

    session = RecordingSession(project, output_dir=output_dir, fps=fps, monitor_idx=monitor)
    run_recorder(session, project)


# ============================================================================
# Channel Commands
# ============================================================================


channels_app = typer.Typer(help="Manage channels")
app.add_typer(channels_app, name="channels")


@channels_app.command("status")
def channels_status():
    """Show channel status."""
    from syll.config.loader import load_config

    config = load_config()

    table = Table(title="Channel Status")
    table.add_column("Channel", style="cyan")
    table.add_column("Enabled", style="green")
    table.add_column("Configuration", style="yellow")

    # WhatsApp
    wa = config.channels.whatsapp
    table.add_row(
        "WhatsApp",
        "✓" if wa.enabled else "✗",
        wa.bridge_url
    )

    dc = config.channels.discord
    table.add_row(
        "Discord",
        "✓" if dc.enabled else "✗",
        dc.gateway_url
    )

    # Telegram
    tg = config.channels.telegram
    tg_config = f"token: {tg.token[:10]}..." if tg.token else "[dim]not configured[/dim]"
    table.add_row(
        "Telegram",
        "✓" if tg.enabled else "✗",
        tg_config
    )

    console.print(table)


def _get_bridge_dir() -> Path:
    """Get the bridge directory, setting it up if needed."""
    import shutil
    import subprocess

    # User's bridge location
    user_bridge = Path.home() / ".syll" / "bridge"

    # Check if already built
    if (user_bridge / "dist" / "index.js").exists():
        return user_bridge

    # Check for npm
    if not shutil.which("npm"):
        console.print("[red]npm not found. Please install Node.js >= 18.[/red]")
        raise typer.Exit(1)

    # Find source bridge: first check package data, then source dir
    pkg_bridge = Path(__file__).parent.parent / "bridge"  # syll/bridge (installed)
    src_bridge = Path(__file__).parent.parent.parent / "bridge"  # repo root/bridge (dev)

    source = None
    if (pkg_bridge / "package.json").exists():
        source = pkg_bridge
    elif (src_bridge / "package.json").exists():
        source = src_bridge

    if not source:
        console.print("[red]Bridge source not found.[/red]")
        console.print("Try reinstalling: pip install --force-reinstall syll")
        raise typer.Exit(1)

    console.print(f"{__logo__} Setting up bridge...")

    # Copy to user directory
    user_bridge.parent.mkdir(parents=True, exist_ok=True)
    if user_bridge.exists():
        shutil.rmtree(user_bridge)
    shutil.copytree(source, user_bridge, ignore=shutil.ignore_patterns("node_modules", "dist"))

    # Install and build
    try:
        console.print("  Installing dependencies...")
        subprocess.run(["npm", "install"], cwd=user_bridge, check=True, capture_output=True)

        console.print("  Building...")
        subprocess.run(["npm", "run", "build"], cwd=user_bridge, check=True, capture_output=True)

        console.print("[green]✓[/green] Bridge ready\n")
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Build failed: {e}[/red]")
        if e.stderr:
            console.print(f"[dim]{e.stderr.decode()[:500]}[/dim]")
        raise typer.Exit(1)

    return user_bridge


@channels_app.command("login")
def channels_login():
    """Link device via QR code."""
    import subprocess

    bridge_dir = _get_bridge_dir()

    console.print(f"{__logo__} Starting bridge...")
    console.print("Scan the QR code to connect.\n")

    try:
        subprocess.run(["npm", "start"], cwd=bridge_dir, check=True)
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Bridge failed: {e}[/red]")
    except FileNotFoundError:
        console.print("[red]npm not found. Please install Node.js.[/red]")


# ============================================================================
# Cron Commands
# ============================================================================

cron_app = typer.Typer(help="Manage scheduled tasks")
app.add_typer(cron_app, name="cron")


@cron_app.command("list")
def cron_list(
    all: bool = typer.Option(False, "--all", "-a", help="Include disabled jobs"),
):
    """List scheduled jobs."""
    from syll.config.loader import get_data_dir
    from syll.cron.service import CronService

    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)

    jobs = service.list_jobs(include_disabled=all)

    if not jobs:
        console.print("No scheduled jobs.")
        return

    table = Table(title="Scheduled Jobs")
    table.add_column("ID", style="cyan")
    table.add_column("Name")
    table.add_column("Schedule")
    table.add_column("Status")
    table.add_column("Next Run")

    import time
    for job in jobs:
        # Format schedule
        if job.schedule.kind == "every":
            sched = f"every {(job.schedule.every_ms or 0) // 1000}s"
        elif job.schedule.kind == "cron":
            sched = job.schedule.expr or ""
        else:
            sched = "one-time"

        # Format next run
        next_run = ""
        if job.state.next_run_at_ms:
            next_time = time.strftime("%Y-%m-%d %H:%M", time.localtime(job.state.next_run_at_ms / 1000))
            next_run = next_time

        status = "[green]enabled[/green]" if job.enabled else "[dim]disabled[/dim]"

        table.add_row(job.id, job.name, sched, status, next_run)

    console.print(table)


@cron_app.command("add")
def cron_add(
    name: str = typer.Option(..., "--name", "-n", help="Job name"),
    message: str = typer.Option(..., "--message", "-m", help="Message for agent"),
    every: int = typer.Option(None, "--every", "-e", help="Run every N seconds"),
    cron_expr: str = typer.Option(None, "--cron", "-c", help="Cron expression (e.g. '0 9 * * *')"),
    at: str = typer.Option(None, "--at", help="Run once at time (ISO format)"),
    deliver: bool = typer.Option(False, "--deliver", "-d", help="Deliver response to channel"),
    to: str = typer.Option(None, "--to", help="Recipient for delivery"),
    channel: str = typer.Option(None, "--channel", help="Channel for delivery (e.g. 'telegram', 'whatsapp')"),
):
    """Add a scheduled job."""
    from syll.config.loader import get_data_dir
    from syll.cron.service import CronService
    from syll.cron.types import CronSchedule

    # Determine schedule type
    if every:
        schedule = CronSchedule(kind="every", every_ms=every * 1000)
    elif cron_expr:
        schedule = CronSchedule(kind="cron", expr=cron_expr)
    elif at:
        import datetime
        dt = datetime.datetime.fromisoformat(at)
        schedule = CronSchedule(kind="at", at_ms=int(dt.timestamp() * 1000))
    else:
        console.print("[red]Error: Must specify --every, --cron, or --at[/red]")
        raise typer.Exit(1)

    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)

    job = service.add_job(
        name=name,
        schedule=schedule,
        message=message,
        deliver=deliver,
        to=to,
        channel=channel,
    )

    console.print(f"[green]✓[/green] Added job '{job.name}' ({job.id})")


@cron_app.command("remove")
def cron_remove(
    job_id: str = typer.Argument(..., help="Job ID to remove"),
):
    """Remove a scheduled job."""
    from syll.config.loader import get_data_dir
    from syll.cron.service import CronService

    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)

    if service.remove_job(job_id):
        console.print(f"[green]✓[/green] Removed job {job_id}")
    else:
        console.print(f"[red]Job {job_id} not found[/red]")


@cron_app.command("enable")
def cron_enable(
    job_id: str = typer.Argument(..., help="Job ID"),
    disable: bool = typer.Option(False, "--disable", help="Disable instead of enable"),
):
    """Enable or disable a job."""
    from syll.config.loader import get_data_dir
    from syll.cron.service import CronService

    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)

    job = service.enable_job(job_id, enabled=not disable)
    if job:
        status = "disabled" if disable else "enabled"
        console.print(f"[green]✓[/green] Job '{job.name}' {status}")
    else:
        console.print(f"[red]Job {job_id} not found[/red]")


@cron_app.command("run")
def cron_run(
    job_id: str = typer.Argument(..., help="Job ID to run"),
    force: bool = typer.Option(False, "--force", "-f", help="Run even if disabled"),
):
    """Manually run a job."""
    from syll.config.loader import get_data_dir
    from syll.cron.service import CronService

    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)

    async def run():
        return await service.run_job(job_id, force=force)

    if asyncio.run(run()):
        console.print("[green]✓[/green] Job executed")
    else:
        console.print(f"[red]Failed to run job {job_id}[/red]")


# ============================================================================
# Status Commands
# ============================================================================


@app.command()
def status():
    """Show Syll status."""
    from syll.config.loader import get_config_path, load_config

    config_path = get_config_path()
    config = load_config()
    workspace = config.workspace_path

    console.print(f"{__logo__} Syll Status\n")

    console.print(f"Config: {config_path} {'[green]✓[/green]' if config_path.exists() else '[red]✗[/red]'}")
    console.print(f"Workspace: {workspace} {'[green]✓[/green]' if workspace.exists() else '[red]✗[/red]'}")

    if config_path.exists():
        console.print(f"Model: {config.models.chat.model}")

        # Check model endpoints
        has_chat = bool(config.models.chat.api_key)
        has_planner = bool(config.models.planner.model)
        has_actor = bool(config.models.actor.model)
        has_trace = bool(config.models.trace.model)
        has_stt = bool(config.models.stt.model)

        console.print(f"Chat: {'[green]✓[/green]' if has_chat else '[dim]not set[/dim]'} {config.models.chat.model}")
        console.print(f"Planner: {'[green]✓[/green] ' + config.models.planner.model if has_planner else '[dim]→ chat[/dim]'}")
        console.print(f"Actor: {'[green]✓[/green] ' + config.models.actor.model if has_actor else '[dim]→ chat[/dim]'}")
        console.print(f"Trace: {'[green]✓[/green] ' + config.models.trace.model if has_trace else '[dim]→ planner[/dim]'}")
        console.print(f"STT: {'[green]✓[/green] ' + config.models.stt.model if has_stt else '[dim]not set[/dim]'}")


if __name__ == "__main__":
    app()
