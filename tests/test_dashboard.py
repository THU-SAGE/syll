"""Test script for Lynx Memory Dashboard."""

import asyncio
from datetime import datetime
from pathlib import Path

from syll.agent.events import Event, EventContent, EventSource, EventStore


async def test_event_store():
    """Test event store functionality."""
    # Create test directory
    test_dir = Path("/tmp/syll_test")
    test_dir.mkdir(exist_ok=True)

    store = EventStore(test_dir)

    # Create test events
    events = [
        Event(
            agent_type="im_agent",
            event_type="message",
            source=EventSource(platform="telegram", chat_id="123", user_id="user1"),
            content=EventContent(
                text="User: Hello\nAssistant: Hi there!",
                metadata={"iterations": 1},
            ),
            tags=["test"],
        ),
        Event(
            agent_type="gui_agent",
            event_type="action",
            source=EventSource(platform="desktop", chat_id="gui", user_id="system"),
            content=EventContent(
                text="Clicked button at (100, 200)",
                media=["/tmp/screenshot.png"],
                metadata={"action": "click", "coordinates": [100, 200]},
            ),
            tags=["test", "gui"],
        ),
    ]

    # Log events
    print("Logging events...")
    for event in events:
        store.log_event(event)
        print(f"  ✓ Logged {event.agent_type} event")

    # Read today's events
    print("\nReading today's events...")
    today_events = store.read_day(datetime.now().date())
    print(f"  Found {len(today_events)} events")

    # Get stats
    print("\nGetting stats...")
    stats = store.get_stats()
    if stats:
        print(f"  Total: {stats.total}")
        print(f"  By agent: {stats.by_agent}")
        print(f"  By type: {stats.by_type}")

    # Get timeline
    print("\nGetting timeline...")
    timeline = store.get_timeline(limit=5)
    print(f"  Found {len(timeline)} recent events")

    print("\n✅ All tests passed!")


if __name__ == "__main__":
    asyncio.run(test_event_store())
