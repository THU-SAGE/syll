"""Unified event storage system for Lynx Memory Dashboard."""

import json
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from loguru import logger
from pydantic import BaseModel, Field


class EventSource(BaseModel):
    """Event source information."""

    platform: str = Field(..., description="Platform name (telegram, feishu, discord, web)")
    chat_id: str | None = Field(None, description="Chat/channel ID")
    user_id: str | None = Field(None, description="User ID")


class EventContent(BaseModel):
    """Event content."""

    text: str | None = Field(None, description="Text content")
    media: list[str] = Field(default_factory=list, description="Media file paths")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Agent-specific data")


class Event(BaseModel):
    """Unified event model."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    agent_type: Literal["im_agent", "gui_agent", "memory_agent", "monitor_agent"]
    event_type: Literal["message", "action", "memory", "alert"]
    source: EventSource
    content: EventContent
    tags: list[str] = Field(default_factory=list)
    summary: str | None = Field(None, description="AI-generated one-line summary")


class DailyStats(BaseModel):
    """Daily statistics."""

    date: str
    total: int
    by_agent: dict[str, int] = Field(default_factory=dict)
    by_type: dict[str, int] = Field(default_factory=dict)


class EventStore:
    """Event storage manager using JSONL files."""

    def __init__(self, base_dir: Path | None = None, *, log_mode: str = "full"):
        """Initialize event store.

        Args:
            base_dir: Base directory for event storage (default: ~/.syll)
            log_mode: Conversation-text retention for ``message`` events —
                ``"full"`` (verbatim, default), ``"summary"`` (redact bodies to
                the one-line summary), or ``"off"`` (do not persist message
                events at all). See ``PrivacyConfig.event_log_mode``.
        """
        if base_dir is None:
            base_dir = Path.home() / ".syll"
        self.log_mode = log_mode if log_mode in ("full", "summary", "off") else "full"
        self.base_dir = Path(base_dir)
        self.events_dir = self.base_dir / "events"
        self.stats_dir = self.base_dir / "stats"

        # Create directories
        self.events_dir.mkdir(parents=True, exist_ok=True)
        self.stats_dir.mkdir(parents=True, exist_ok=True)

        self.stats_file = self.stats_dir / "daily_stats.json"

    def log_event(self, event: Event) -> None:
        """Log an event to daily JSONL file.

        Args:
            event: Event to log
        """
        # Privacy: redact / drop verbatim conversation bodies for message events
        # according to the configured retention mode. Non-message events
        # (memory/action/alert) are unaffected.
        if self.log_mode != "full" and event.event_type == "message":
            if self.log_mode == "off":
                return  # do not persist conversation bodies at all
            # "summary": keep the event shell (so stats/heatmap still count it)
            # but replace the verbatim text with the one-line summary.
            event = event.model_copy(
                update={
                    "content": event.content.model_copy(
                        update={"text": event.summary or None}
                    )
                }
            )

        event_date = event.timestamp.date()
        event_file = self.events_dir / f"{event_date.isoformat()}.jsonl"

        try:
            with event_file.open("a", encoding="utf-8") as f:
                f.write(event.model_dump_json() + "\n")
            logger.debug(f"Logged event {event.id} to {event_file}")

            # Update stats
            self._update_stats(event_date, event)
        except Exception as e:
            logger.error(f"Failed to log event: {e}")

    def read_day(
        self, target_date: date, agent_type: str | None = None
    ) -> list[Event]:
        """Read all events for a specific day.

        Args:
            target_date: Target date
            agent_type: Optional agent type filter

        Returns:
            List of events
        """
        event_file = self.events_dir / f"{target_date.isoformat()}.jsonl"
        if not event_file.exists():
            return []

        events = []
        try:
            with event_file.open("r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        event = Event.model_validate_json(line)
                        if agent_type is None or event.agent_type == agent_type:
                            events.append(event)
        except Exception as e:
            logger.error(f"Failed to read events from {event_file}: {e}")

        return events

    def read_range(
        self, start_date: date, end_date: date, agent_type: str | None = None
    ) -> list[Event]:
        """Read events within a date range.

        Args:
            start_date: Start date (inclusive)
            end_date: End date (inclusive)
            agent_type: Optional agent type filter

        Returns:
            List of events
        """
        events = []
        current_date = start_date
        while current_date <= end_date:
            events.extend(self.read_day(current_date, agent_type))
            current_date += timedelta(days=1)
        return events

    def get_event(self, event_id: str) -> Event | None:
        """Get a specific event by ID.

        Args:
            event_id: Event ID

        Returns:
            Event or None if not found
        """
        # Search recent 30 days
        end_date = date.today()
        end_date - timedelta(days=30)

        for event_file in self.events_dir.glob("*.jsonl"):
            try:
                with event_file.open("r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            event = Event.model_validate_json(line)
                            if event.id == event_id:
                                return event
            except Exception as e:
                logger.error(f"Failed to read {event_file}: {e}")

        return None

    def get_stats(self, target_date: date | None = None) -> DailyStats | None:
        """Get statistics for a specific date.

        Args:
            target_date: Target date (default: today)

        Returns:
            Daily statistics or None if not found
        """
        if target_date is None:
            target_date = date.today()

        if not self.stats_file.exists():
            return None

        try:
            with self.stats_file.open("r", encoding="utf-8") as f:
                all_stats = json.load(f)
                date_key = target_date.isoformat()
                if date_key in all_stats:
                    return DailyStats.model_validate(all_stats[date_key])
        except Exception as e:
            logger.error(f"Failed to read stats: {e}")

        return None

    def get_heatmap_data(self, year: int) -> dict[str, int]:
        """Get heatmap data for a specific year.

        Args:
            year: Target year

        Returns:
            Dict mapping date (YYYY-MM-DD) to event count
        """
        if not self.stats_file.exists():
            return {}

        try:
            with self.stats_file.open("r", encoding="utf-8") as f:
                all_stats = json.load(f)
                return {
                    date_str: stats["total"]
                    for date_str, stats in all_stats.items()
                    if date_str.startswith(str(year))
                }
        except Exception as e:
            logger.error(f"Failed to read heatmap data: {e}")
            return {}

    def get_timeline(self, limit: int = 10) -> list[Event]:
        """Get recent events for timeline.

        Args:
            limit: Maximum number of events

        Returns:
            List of recent events
        """
        events = []
        # Search recent 7 days
        for i in range(7):
            target_date = date.today() - timedelta(days=i)
            day_events = self.read_day(target_date)
            events.extend(day_events)
            if len(events) >= limit:
                break

        # Sort by timestamp descending and limit
        events.sort(key=lambda e: e.timestamp, reverse=True)
        return events[:limit]

    def _update_stats(self, target_date: date, event: Event) -> None:
        """Update daily statistics.

        Args:
            target_date: Target date
            event: Event to add to stats
        """
        # Load existing stats
        all_stats = {}
        if self.stats_file.exists():
            try:
                with self.stats_file.open("r", encoding="utf-8") as f:
                    all_stats = json.load(f)
            except Exception as e:
                logger.error(f"Failed to load stats: {e}")

        # Update stats for target date
        date_key = target_date.isoformat()
        if date_key not in all_stats:
            all_stats[date_key] = {
                "date": date_key,
                "total": 0,
                "by_agent": {},
                "by_type": {},
            }

        stats = all_stats[date_key]
        stats["total"] += 1
        stats["by_agent"][event.agent_type] = stats["by_agent"].get(event.agent_type, 0) + 1
        stats["by_type"][event.event_type] = stats["by_type"].get(event.event_type, 0) + 1

        # Save stats
        try:
            with self.stats_file.open("w", encoding="utf-8") as f:
                json.dump(all_stats, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save stats: {e}")
