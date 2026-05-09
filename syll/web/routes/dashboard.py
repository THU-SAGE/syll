"""Dashboard API routes for Lynx Memory Dashboard."""

from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from syll.agent.events import EventStore

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def get_event_store() -> EventStore:
    """Get EventStore instance."""
    base_dir = Path.home() / ".syll"
    return EventStore(base_dir)


@router.get("/heatmap")
async def get_heatmap_data(year: int = Query(default=None, description="Target year")):
    """Get heatmap data for calendar visualization.

    Returns:
        Dict mapping date (YYYY-MM-DD) to event count
    """
    if year is None:
        year = datetime.now().year

    store = get_event_store()
    data = store.get_heatmap_data(year)
    return JSONResponse(content=data)


@router.get("/stats")
async def get_stats():
    """Get overall statistics for dashboard cards.

    Returns:
        Dict with today's stats, total events, active agents, last update time
    """
    store = get_event_store()
    today = date.today()

    # Today's stats
    today_stats = store.get_stats(today)
    today_count = today_stats.total if today_stats else 0

    # Total events (sum from all daily stats)
    total_count = 0
    active_agents = set()
    last_update = None
    agent_distribution = {}

    if store.stats_file.exists():
        import json

        with store.stats_file.open("r") as f:
            all_stats = json.load(f)
            for date_str, stats in all_stats.items():
                total_count += stats["total"]
                for agent, count in stats["by_agent"].items():
                    active_agents.add(agent)
                    agent_distribution[agent] = agent_distribution.get(agent, 0) + count
                # Track most recent date
                try:
                    event_date = datetime.fromisoformat(date_str)
                    if last_update is None or event_date > last_update:
                        last_update = event_date
                except ValueError:
                    pass

    return JSONResponse(
        content={
            "today_count": today_count,
            "total_count": total_count,
            "active_agents": len(active_agents),
            "last_update": last_update.isoformat() if last_update else None,
            "agent_distribution": agent_distribution,
        }
    )


@router.get("/timeline")
async def get_timeline(limit: int = Query(default=10, ge=1, le=100)):
    """Get recent events for timeline display.

    Args:
        limit: Maximum number of events to return

    Returns:
        List of recent events with id, timestamp, agent_type, summary
    """
    store = get_event_store()
    events = store.get_timeline(limit)

    return JSONResponse(
        content=[
            {
                "id": e.id,
                "timestamp": e.timestamp.isoformat(),
                "agent_type": e.agent_type,
                "event_type": e.event_type,
                "summary": e.summary or e.content.text[:100] if e.content.text else "No content",
                "platform": e.source.platform,
            }
            for e in events
        ]
    )


@router.get("/day/{date_str}")
async def get_day_detail(
    date_str: str,
    agent_type: str | None = Query(default=None, description="Filter by agent type"),
):
    """Get detailed events for a specific day.

    Args:
        date_str: Date in YYYY-MM-DD format
        agent_type: Optional agent type filter

    Returns:
        Dict with hourly distribution and event list
    """
    try:
        target_date = date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    store = get_event_store()
    events = store.read_day(target_date, agent_type)

    # Calculate hourly distribution
    hourly_dist = [0] * 24
    for event in events:
        hour = event.timestamp.hour
        hourly_dist[hour] += 1

    # Format events for display
    event_list = [
        {
            "id": e.id,
            "timestamp": e.timestamp.isoformat(),
            "agent_type": e.agent_type,
            "event_type": e.event_type,
            "summary": e.summary or (e.content.text[:100] if e.content.text else "No content"),
            "platform": e.source.platform,
            "has_media": len(e.content.media) > 0,
        }
        for e in events
    ]

    return JSONResponse(
        content={
            "date": date_str,
            "total": len(events),
            "hourly_distribution": hourly_dist,
            "events": event_list,
        }
    )


@router.get("/events/{event_id}")
async def get_event_detail(event_id: str):
    """Get full details for a specific event.

    Args:
        event_id: Event UUID

    Returns:
        Full event data including content and metadata
    """
    store = get_event_store()
    event = store.get_event(event_id)

    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    return JSONResponse(
        content={
            "id": event.id,
            "timestamp": event.timestamp.isoformat(),
            "agent_type": event.agent_type,
            "event_type": event.event_type,
            "source": {
                "platform": event.source.platform,
                "chat_id": event.source.chat_id,
                "user_id": event.source.user_id,
            },
            "content": {
                "text": event.content.text,
                "media": event.content.media,
                "metadata": event.content.metadata,
            },
            "tags": event.tags,
            "summary": event.summary,
        }
    )
