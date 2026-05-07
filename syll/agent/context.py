"""Context builder for assembling agent prompts."""

import base64
import mimetypes
import platform
from pathlib import Path
from typing import Any

from syll.agent.memory import MemoryStore
from syll.agent.skills import SkillsLoader
from syll.agent.tools.base import ToolResult
from syll.config.schema import IdentityConfig
from syll.utils.language import build_turn_language_note


class ContextBuilder:
    """
    Builds the context (system prompt + messages) for the agent.

    Assembles bootstrap files, memory, skills, and conversation history
    into a coherent prompt for the LLM.
    """

    BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md", "IDENTITY.md"]

    def __init__(self, workspace: Path, identity: IdentityConfig | None = None):
        self.workspace = workspace
        self.memory = MemoryStore(workspace)
        self.skills = SkillsLoader(workspace)
        self.identity = identity or IdentityConfig()

    def _substitute_vars(self, text: str) -> str:
        """Replace {{ghost_name}} and {{user_name}} placeholders with config values.

        Uses plain str.replace rather than str.format so literal { and } inside
        skill worked examples (code blocks, JSON, tool call args) don't break.
        """
        text = text.replace("{{ghost_name}}", self.identity.ghost_name or "Syll")
        if self.identity.user_name:
            text = text.replace("{{user_name}}", self.identity.user_name)
        else:
            text = text.replace("{{user_name}}", "the user")
        return text

    def substitute(self, text: str) -> str:
        """Public wrapper over `_substitute_vars` for callers outside this class.

        Used by the cron ritual handler (`on_cron_job`) at fire time so profile
        renames propagate into already-installed ritual prompts without needing
        to re-install the jobs. Safe to call on any string.
        """
        return self._substitute_vars(text)

    def build_system_prompt(self, skill_names: list[str] | None = None) -> str:
        """
        Build the system prompt from bootstrap files, memory, and skills.

        Args:
            skill_names: Optional list of skills to include.

        Returns:
            Complete system prompt.
        """
        parts = []

        # Core identity
        parts.append(self._get_identity())

        # Bootstrap files
        bootstrap = self._load_bootstrap_files()
        if bootstrap:
            parts.append(bootstrap)

        # Lore fragments — story beats the agent may rarely surface as asides
        # when a conversational moment naturally rhymes with one. Loaded on
        # every turn so the LLM has them in context; rules for when/how to
        # surface them live at the top of fragments.md itself.
        fragments_path = self.workspace / "lore" / "fragments.md"
        if fragments_path.exists():
            try:
                fragments_content = fragments_path.read_text(encoding="utf-8")
                parts.append(f"# Lore Fragments\n\n{fragments_content}")
            except OSError:
                pass  # graceful degradation if read fails

        # Memory context
        memory = self.memory.get_memory_context()
        if memory:
            parts.append(f"# Memory\n\n{memory}")

        # Skills - progressive loading
        # 1. Always-loaded skills: include full content
        always_skills = self.skills.get_always_skills()
        if always_skills:
            always_content = self.skills.load_skills_for_context(always_skills)
            if always_content:
                parts.append(f"# Active Skills\n\n{always_content}")

        # 2. Available skills: only show summary (agent uses read_file to load)
        skills_summary = self.skills.build_skills_summary()
        if skills_summary:
            parts.append(f"""# Skills

The following skills extend your capabilities. To use a skill, read its SKILL.md file using the read_file tool.
Skills with available="false" need dependencies installed first - you can try installing them with apt/brew.

{skills_summary}""")

        # GUI demonstration skills
        gui_skills_section = self._build_gui_skills_section()
        if gui_skills_section:
            parts.append(gui_skills_section)

        return self._substitute_vars("\n\n---\n\n".join(parts))

    def _get_identity(self) -> str:
        """Get the core identity section."""
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
        workspace_path = str(self.workspace.expanduser().resolve())
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"

        return f"""# {{{{ghost_name}}}}

You are {{{{ghost_name}}}}, a warm helpful presence living in {{{{user_name}}}}'s computer. You have access to tools that allow you to:
- Read, write, and edit files
- Execute shell commands
- Search the web and fetch web pages
- Send messages to users on chat channels
- Spawn subagents for complex background tasks

## Current Time
{now}

## Runtime
{runtime}

## Workspace
Your workspace is at: {workspace_path}
- Memory files: {workspace_path}/memory/MEMORY.md
- Daily notes: {workspace_path}/memory/YYYY-MM-DD.md
- Custom skills: {workspace_path}/skills/{{skill-name}}/SKILL.md

IMPORTANT: When responding to direct questions or conversations, reply directly with your text response.
Only use the 'message' tool when you need to send a message to a specific chat channel (like WhatsApp).
For normal conversation, just respond with text - do not call the message tool.

Always be helpful, accurate, and concise. When using tools, explain what you're doing.
When remembering something, write to {workspace_path}/memory/MEMORY.md"""

    def _load_bootstrap_files(self) -> str:
        """Load all bootstrap files from workspace."""
        parts = []

        for filename in self.BOOTSTRAP_FILES:
            file_path = self.workspace / filename
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8")
                parts.append(f"## {filename}\n\n{content}")

        return "\n\n".join(parts) if parts else ""

    def _build_gui_skills_section(self) -> str:
        """Build system prompt section listing available GUI demonstration skills."""
        from syll.agent.gui_skill import GUISkillStore

        store = GUISkillStore(self.workspace)
        gui_skills = store.list_gui_skills()

        lines: list[str] = []
        if gui_skills:
            lines = ["# GUI Demonstration Skills", ""]
            lines.append(
                "The following GUI skills have recorded demonstrations. "
                "When executing similar tasks, use "
                '`gui_action(instruction="...", skill_name="xxx")` '
                "to inject demonstration context:"
            )
            lines.append("")
            for s in gui_skills:
                lines.append(f"- **{s['name']}**: {s['description']} ({s['steps']} steps)")

        # Append Aloha skills
        try:
            from syll.agent.aloha_gui_skill import AlohaSkillStore
            aloha_store = AlohaSkillStore(self.workspace)
            aloha_skills = aloha_store.list_skills()
        except Exception:
            aloha_skills = []

        if aloha_skills:
            if not lines:
                lines = ["# GUI Demonstration Skills", ""]
            lines.append("")
            lines.append(
                "When the user asks you to perform any of these GUI tasks, "
                "you MUST use `gui_action_planned` with the matching skill_name. "
                "Do NOT use shell commands, exec, or other tools for these tasks."
            )
            lines.append("")
            for s in aloha_skills:
                desc = s.get('description') or s['name']
                aliases = s.get('aliases', [])
                alias_str = f" (also: {', '.join(aliases)})" if aliases else ""
                lines.append(
                    f"- **{s['name']}**: {desc}{alias_str}\n"
                    f"  → `gui_action_planned(instruction=\"{desc}\", skill_name=\"{s['name']}\")`"
                )

        if not lines:
            return ""
        return "\n".join(lines)

    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        skill_names: list[str] | None = None,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        language_hint_text: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Build the complete message list for an LLM call.

        Args:
            history: Previous conversation messages.
            current_message: The new user message.
            skill_names: Optional skills to include.
            media: Optional list of local file paths for images/media.
            channel: Current channel (telegram, feishu, etc.).
            chat_id: Current chat/user ID.
            language_hint_text: Optional raw user text used only for reply-language alignment.

        Returns:
            List of messages including system prompt.
        """
        messages = []

        # System prompt
        system_prompt = self.build_system_prompt(skill_names)
        turn_language_note = build_turn_language_note(language_hint_text or current_message)
        if turn_language_note:
            system_prompt += f"\n\n## This Turn\n{turn_language_note}"
        if channel and chat_id:
            system_prompt += f"\n\n## Current Session\nChannel: {channel}\nChat ID: {chat_id}"
        messages.append({"role": "system", "content": system_prompt})

        # History
        messages.extend(history)

        # Current message (with optional image attachments)
        user_content = self._build_user_content(current_message, media)
        messages.append({"role": "user", "content": user_content})

        return messages

    def _build_user_content(self, text: str, media: list[str] | None) -> str | list[dict[str, Any]]:
        """Build user message content with optional base64-encoded images."""
        if not media:
            return text

        images = []
        for path in media:
            p = Path(path)
            mime, _ = mimetypes.guess_type(path)
            if not p.is_file() or not mime or not mime.startswith("image/"):
                continue
            b64 = base64.b64encode(p.read_bytes()).decode()
            images.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})

        if not images:
            return text
        return images + [{"type": "text", "text": text}]

    def add_tool_result(
        self,
        messages: list[dict[str, Any]],
        tool_call_id: str,
        tool_name: str,
        result: str | ToolResult
    ) -> list[dict[str, Any]]:
        """
        Add a tool result to the message list.

        Args:
            messages: Current message list.
            tool_call_id: ID of the tool call.
            tool_name: Name of the tool.
            result: Tool execution result (str or ToolResult with media).

        Returns:
            Updated message list.
        """
        if isinstance(result, ToolResult) and result.media:
            # Build multimodal content: images + text
            content_parts: list[dict[str, Any]] = []
            for path in result.media:
                p = Path(path)
                mime, _ = mimetypes.guess_type(path)
                if not p.is_file() or not mime or not mime.startswith("image/"):
                    continue
                b64 = base64.b64encode(p.read_bytes()).decode()
                content_parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"}
                })
            content_parts.append({"type": "text", "text": result.text})
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "name": tool_name,
                "content": content_parts
            })
        else:
            text = result.text if isinstance(result, ToolResult) else result
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "name": tool_name,
                "content": text
            })
        return messages

    def add_assistant_message(
        self,
        messages: list[dict[str, Any]],
        content: str | None,
        tool_calls: list[dict[str, Any]] | None = None,
        *,
        reasoning_content: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Add an assistant message to the message list.

        Args:
            messages: Current message list.
            content: Message content.
            tool_calls: Optional tool calls.
            reasoning_content: Optional provider-specific reasoning text.

        Returns:
            Updated message list.
        """
        msg: dict[str, Any] = {"role": "assistant", "content": content or ""}

        if tool_calls:
            msg["tool_calls"] = tool_calls

        if reasoning_content:
            msg["reasoning_content"] = reasoning_content

        messages.append(msg)
        return messages
