"""Agent loop: the core processing engine."""

import asyncio
import json
from pathlib import Path

from loguru import logger

from syll.agent.context import ContextBuilder
from syll.agent.events import Event, EventContent, EventSource, EventStore
from syll.agent.result import AgentResult
from syll.agent.subagent import SubagentManager
from syll.agent.tools.attach_file import AttachFileTool
from syll.agent.tools.base import ToolResult
from syll.agent.tools.cron import CronTool
from syll.agent.tools.file_preview import FilePreviewTool
from syll.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from syll.agent.tools.find_file import FindFileTool
from syll.agent.tools.message import MessageTool
from syll.agent.tools.registry import ToolRegistry
from syll.agent.tools.screenshot import ScreenshotTool
from syll.agent.tools.shell import ExecTool
from syll.agent.tools.spawn import SpawnTool
from syll.agent.tools.web import WebFetchTool, WebSearchTool
from syll.bus.events import InboundMessage, OutboundMessage
from syll.bus.queue import MessageBus
from syll.providers.base import LLMProvider
from syll.session.manager import SessionManager


class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int = 20,
        brave_api_key: str | None = None,
        exec_config: "ExecToolConfig | None" = None,
        cron_service: "CronService | None" = None,
        restrict_to_workspace: bool = False,
        gui_config: "GuiConfig | None" = None,
        syll_config: "Config | None" = None,
    ):
        from syll.config.schema import ExecToolConfig
        self.bus = bus
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = max_iterations
        self.brave_api_key = brave_api_key
        self.exec_config = exec_config or ExecToolConfig()
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace
        self.gui_config = gui_config
        self.syll_config = syll_config

        identity = syll_config.identity if syll_config else None
        self.context = ContextBuilder(workspace, identity=identity)
        self.sessions = SessionManager(workspace)
        self.tools = ToolRegistry()
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            brave_api_key=brave_api_key,
            exec_config=self.exec_config,
            restrict_to_workspace=restrict_to_workspace,
        )
        self.event_store = EventStore(workspace.parent)

        self._running = False
        self._register_default_tools()

    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        # File tools (restrict to workspace if configured)
        allowed_dir = self.workspace if self.restrict_to_workspace else None
        self.tools.register(ReadFileTool(allowed_dir=allowed_dir))
        self.tools.register(WriteFileTool(allowed_dir=allowed_dir))
        self.tools.register(EditFileTool(allowed_dir=allowed_dir))
        self.tools.register(ListDirTool(allowed_dir=allowed_dir))

        # Shell tool
        self.tools.register(ExecTool(
            working_dir=str(self.workspace),
            timeout=self.exec_config.timeout,
            restrict_to_workspace=self.restrict_to_workspace,
        ))

        # Web tools
        self.tools.register(WebSearchTool(api_key=self.brave_api_key))
        self.tools.register(WebFetchTool())

        # Message tool
        message_tool = MessageTool(send_callback=self.bus.publish_outbound)
        self.tools.register(message_tool)

        # Spawn tool (for subagents)
        spawn_tool = SpawnTool(manager=self.subagents)
        self.tools.register(spawn_tool)

        # Cron tool (for scheduling)
        if self.cron_service:
            self.tools.register(CronTool(self.cron_service))

        # Screenshot tool (always available)
        self.tools.register(ScreenshotTool())

        # File discovery / preview / attach (Demo 3 mobile → desktop file flow)
        self.tools.register(FindFileTool())
        self.tools.register(FilePreviewTool())
        self.tools.register(AttachFileTool())

        # Voice tool — only if TTS credentials are configured. Without
        # this guard the tool would surface in the LLM prompt even when
        # ``speak`` would immediately fail, wasting a turn.
        if (
            self.syll_config
            and getattr(self.syll_config, "voice", None)
            and self.syll_config.voice.enabled
            and self.syll_config.voice.tts.appid
            and self.syll_config.voice.tts.access_token
        ):
            try:
                from syll.agent.tools.speak import SpeakTool
                from syll.providers.voice_volc import VolcengineTTSProvider
                tts_cfg = self.syll_config.voice.tts
                # Default resource_id is used ONLY as the fallback for
                # unknown voices; the builtin voice→resource_id catalog
                # covers both BigTTS 2.0 and Seed-TTS 2.0. We align the
                # fallback with the configured default_speaker so the
                # "empty resource_id + common voice" combination Just Works.
                tts = VolcengineTTSProvider(
                    appid=tts_cfg.appid,
                    access_token=tts_cfg.access_token,
                    default_speaker=self.syll_config.voice.default_speaker,
                    resource_id=tts_cfg.resource_id or "seed-tts-2.0",
                    voice_resources=getattr(tts_cfg, "voice_resources", None) or None,
                )
                self.tools.register(SpeakTool(tts))
                logger.info("SpeakTool registered (voice.tts.provider=volcengine)")
            except Exception as e:
                logger.warning(f"SpeakTool registration failed: {e}")

        # GUI tool (for UI automation via UI-TARS)
        if self.gui_config and self.gui_config.enabled:
            from syll.agent.aloha_gui_skill import AlohaSkillStore
            from syll.agent.gui_skill import GUISkillStore
            from syll.agent.tools.ui_tars import UITarsTool

            gui_skill_store = GUISkillStore(self.workspace)
            aloha_skill_store = AlohaSkillStore(self.workspace)
            ui_tars_tool = UITarsTool(
                self.gui_config,
                gui_skill_store=gui_skill_store,
                aloha_skill_store=aloha_skill_store,
                syll_config=self.syll_config,
            )
            ui_tars_tool._event_store = self.event_store
            self.tools.register(ui_tars_tool)

            # Planner+Actor tool for Aloha skills
            from syll.agent.tools.aloha_planner_tool import AlohaPlannerTool

            planner_tool = AlohaPlannerTool(
                self.gui_config, aloha_skill_store, syll_config=self.syll_config
            )
            planner_tool._event_store = self.event_store
            self.tools.register(planner_tool)

    async def run(self) -> None:
        """Run the agent loop, processing messages from the bus."""
        self._running = True
        logger.info("Agent loop started")

        while self._running:
            try:
                # Wait for next message
                msg = await asyncio.wait_for(
                    self.bus.consume_inbound(),
                    timeout=1.0
                )

                # Process it
                try:
                    response = await self._process_message(msg)
                    if response:
                        await self.bus.publish_outbound(response)
                except Exception as e:
                    logger.error(f"Error processing message: {e}")
                    # Send error response
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=f"Sorry, I encountered an error: {str(e)}"
                    ))
            except asyncio.TimeoutError:
                continue

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")

    async def _process_message(
        self,
        msg: InboundMessage,
        *,
        prompt_content: str | None = None,
        language_hint_text: str | None = None,
    ) -> OutboundMessage | None:
        """
        Process a single inbound message.

        Args:
            msg: The inbound message to process.

        Returns:
            The response message, or None if no response needed.
        """
        # Handle system messages (subagent announces)
        # The chat_id contains the original "channel:chat_id" to route back to
        if msg.channel == "system":
            return await self._process_system_message(msg)

        logger.info(f"Processing message from {msg.channel}:{msg.sender_id}")

        # Get or create session
        session = self.sessions.get_or_create(msg.session_key)

        # Update tool contexts
        message_tool = self.tools.get("message")
        if isinstance(message_tool, MessageTool):
            message_tool.set_context(msg.channel, msg.chat_id)

        spawn_tool = self.tools.get("spawn")
        if isinstance(spawn_tool, SpawnTool):
            spawn_tool.set_context(msg.channel, msg.chat_id)

        cron_tool = self.tools.get("cron")
        if isinstance(cron_tool, CronTool):
            cron_tool.set_context(msg.channel, msg.chat_id)

        # Build initial messages (use get_history for LLM-formatted messages)
        messages = self.context.build_messages(
            history=session.get_history(),
            current_message=prompt_content or msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel,
            chat_id=msg.chat_id,
            language_hint_text=language_hint_text,
        )

        # Agent loop
        iteration = 0
        final_content = None
        collected_media: list[str] = []

        while iteration < self.max_iterations:
            iteration += 1

            # Call LLM
            response = await self.provider.chat(
                messages=messages,
                tools=self.tools.get_definitions(),
                model=self.model
            )

            # Handle tool calls
            if response.has_tool_calls:
                # Add assistant message with tool calls
                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments)  # Must be JSON string
                        }
                    }
                    for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(
                    messages, response.content, tool_call_dicts
                )

                # Execute tools
                for tool_call in response.tool_calls:
                    args_str = json.dumps(tool_call.arguments)
                    logger.debug(f"Executing tool: {tool_call.name} with arguments: {args_str}")
                    result = await self.tools.execute(tool_call.name, tool_call.arguments)
                    if isinstance(result, ToolResult) and result.media:
                        collected_media.extend(result.media)
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )
            else:
                # No tool calls, we're done
                final_content = response.content
                break

        if final_content is None:
            final_content = "I've completed processing but have no response to give."

        # Save to session
        session.add_message("user", msg.content)
        session.add_message("assistant", final_content)
        self.sessions.save(session)

        # Log event to event store
        event = Event(
            agent_type="im_agent",
            event_type="message",
            source=EventSource(
                platform=msg.channel,
                chat_id=msg.chat_id,
                user_id=msg.sender_id,
            ),
            content=EventContent(
                text=f"User: {msg.content}\nAssistant: {final_content}",
                media=collected_media,
                metadata={
                    "iterations": iteration,
                    "session_key": msg.session_key,
                },
            ),
        )
        self.event_store.log_event(event)

        # Append to daily memory
        try:
            from datetime import datetime
            summary = f"- [{datetime.now().strftime('%H:%M')}] User: {msg.content[:100]}\n"
            self.context.memory.append_today(summary)
        except Exception as e:
            logger.debug(f"Failed to append daily memory: {e}")

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            media=collected_media,
        )

    async def _process_system_message(self, msg: InboundMessage) -> OutboundMessage | None:
        """
        Process a system message (e.g., subagent announce).

        The chat_id field contains "original_channel:original_chat_id" to route
        the response back to the correct destination.
        """
        logger.info(f"Processing system message from {msg.sender_id}")

        # Parse origin from chat_id (format: "channel:chat_id")
        if ":" in msg.chat_id:
            parts = msg.chat_id.split(":", 1)
            origin_channel = parts[0]
            origin_chat_id = parts[1]
        else:
            # Fallback
            origin_channel = "cli"
            origin_chat_id = msg.chat_id

        # Use the origin session for context
        session_key = f"{origin_channel}:{origin_chat_id}"
        session = self.sessions.get_or_create(session_key)

        # Update tool contexts
        message_tool = self.tools.get("message")
        if isinstance(message_tool, MessageTool):
            message_tool.set_context(origin_channel, origin_chat_id)

        spawn_tool = self.tools.get("spawn")
        if isinstance(spawn_tool, SpawnTool):
            spawn_tool.set_context(origin_channel, origin_chat_id)

        cron_tool = self.tools.get("cron")
        if isinstance(cron_tool, CronTool):
            cron_tool.set_context(origin_channel, origin_chat_id)

        # Build messages with the announce content
        messages = self.context.build_messages(
            history=session.get_history(),
            current_message=msg.content,
            channel=origin_channel,
            chat_id=origin_chat_id,
        )

        # Agent loop (limited for announce handling)
        iteration = 0
        final_content = None
        collected_media: list[str] = []

        while iteration < self.max_iterations:
            iteration += 1

            response = await self.provider.chat(
                messages=messages,
                tools=self.tools.get_definitions(),
                model=self.model
            )

            if response.has_tool_calls:
                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments)
                        }
                    }
                    for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(
                    messages, response.content, tool_call_dicts
                )

                for tool_call in response.tool_calls:
                    args_str = json.dumps(tool_call.arguments)
                    logger.debug(f"Executing tool: {tool_call.name} with arguments: {args_str}")
                    result = await self.tools.execute(tool_call.name, tool_call.arguments)
                    if isinstance(result, ToolResult) and result.media:
                        collected_media.extend(result.media)
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )
            else:
                final_content = response.content
                break

        if final_content is None:
            final_content = "Background task completed."

        # Save to session (mark as system message in history)
        session.add_message("user", f"[System: {msg.sender_id}] {msg.content}")
        session.add_message("assistant", final_content)
        self.sessions.save(session)

        return OutboundMessage(
            channel=origin_channel,
            chat_id=origin_chat_id,
            content=final_content,
            media=collected_media,
        )

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        *,
        language_hint_text: str | None = None,
        inject_skill_hints: bool = False,
    ) -> AgentResult:
        """
        Process a message directly (for CLI / REST / cron / ritual usage).

        Returns an ``AgentResult`` carrying both the final text and any
        media produced during the turn (e.g. ``speak`` TTS output).
        """
        prompt_content = content
        if inject_skill_hints:
            from syll.web.skill_router import inject_skill_hint

            prompt_content = inject_skill_hint(self, content)

        msg = InboundMessage(
            channel=channel,
            sender_id="user",
            chat_id=chat_id,
            content=content
        )

        response = await self._process_message(
            msg,
            prompt_content=prompt_content,
            language_hint_text=language_hint_text or content,
        )
        if response is None:
            return AgentResult(text="")
        return AgentResult(
            text=response.content or "",
            media=list(response.media or []),
        )
