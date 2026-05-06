"""Agent execution result shared by direct/cron/CLI consumers."""

from dataclasses import dataclass, field


@dataclass
class AgentResult:
    """Unified agent turn result.

    Returned by ``AgentLoop.process_direct`` so non-channel callers
    (CLI, REST, cron, rituals) can access both the final text and any
    media produced by tool calls during the turn.
    """

    text: str = ""
    media: list[str] = field(default_factory=list)
