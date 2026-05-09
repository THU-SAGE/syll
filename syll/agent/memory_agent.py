"""Memory Agent - Automatic event classification and tagging."""

import asyncio
from datetime import date, timedelta
from pathlib import Path

from loguru import logger

from syll.agent.events import EventStore
from syll.providers.base import LLMProvider


class MemoryAgent:
    """Agent that automatically classifies and tags events."""

    def __init__(self, provider: LLMProvider, workspace: Path, model: str | None = None):
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.event_store = EventStore(workspace.parent)

    async def process_recent_events(self, days: int = 1) -> int:
        """Process recent events and generate summaries/tags.

        Args:
            days: Number of days to look back

        Returns:
            Number of events processed
        """
        logger.info(f"Memory Agent: Processing events from last {days} day(s)")

        # Get events from recent days
        end_date = date.today()
        start_date = end_date - timedelta(days=days)
        events = self.event_store.read_range(start_date, end_date)

        # Filter events without summaries
        events_to_process = [e for e in events if not e.summary]

        if not events_to_process:
            logger.info("No events to process")
            return 0

        logger.info(f"Processing {len(events_to_process)} events")

        # Process each event
        processed = 0
        for event in events_to_process:
            try:
                # Generate summary and tags
                summary, tags = await self._analyze_event(event)

                # Update event
                event.summary = summary
                event.tags.extend(tags)

                # Re-save event (append to JSONL)
                # Note: This is a simplified approach. In production, you'd want to
                # update the event in-place or use a database.
                logger.debug(f"Generated summary for event {event.id}: {summary}")
                processed += 1
            except Exception as e:
                logger.error(f"Failed to process event {event.id}: {e}")

        logger.info(f"Memory Agent: Processed {processed} events")
        return processed

    async def _analyze_event(self, event) -> tuple[str, list[str]]:
        """Analyze an event and generate summary and tags.

        Args:
            event: Event to analyze

        Returns:
            Tuple of (summary, tags)
        """
        # Build prompt
        content_text = event.content.text or "No text content"
        prompt = f"""Analyze this event and provide:
1. A one-line summary (max 100 chars)
2. Relevant tags (2-5 tags)

Event details:
- Agent: {event.agent_type}
- Type: {event.event_type}
- Platform: {event.source.platform}
- Content: {content_text[:500]}

Respond in this format:
Summary: <one-line summary>
Tags: <tag1>, <tag2>, <tag3>
"""

        messages = [{"role": "user", "content": prompt}]

        try:
            response = await self.provider.chat(messages=messages, model=self.model)
            text = response.content

            # Parse response
            summary = ""
            tags = []

            for line in text.split("\n"):
                if line.startswith("Summary:"):
                    summary = line.replace("Summary:", "").strip()
                elif line.startswith("Tags:"):
                    tags_str = line.replace("Tags:", "").strip()
                    tags = [t.strip() for t in tags_str.split(",")]

            return summary or content_text[:100], tags
        except Exception as e:
            logger.error(f"Failed to analyze event: {e}")
            return content_text[:100], ["auto-generated"]

    async def run_periodic(self, interval_minutes: int = 60):
        """Run periodic processing.

        Args:
            interval_minutes: Interval between runs
        """
        logger.info(f"Memory Agent: Starting periodic processing (every {interval_minutes} min)")

        while True:
            try:
                await self.process_recent_events(days=1)
            except Exception as e:
                logger.error(f"Memory Agent error: {e}")

            await asyncio.sleep(interval_minutes * 60)
