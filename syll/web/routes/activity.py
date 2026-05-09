"""Agent activity state endpoint for ghost mascot polling."""

import time

from fastapi import APIRouter, Request

router = APIRouter(tags=["activity"])


@router.get("/agent/activity")
async def get_agent_activity(request: Request):
    """Return the current agent activity state.

    Used by the desktop ghost mascot to poll agent status.
    """
    state = getattr(request.app.state, "agent_activity", "idle")
    detail = getattr(request.app.state, "agent_activity_detail", "")
    start_time = getattr(request.app.state, "agent_start_time", None)

    result = {"state": state, "detail": detail}

    # Uptime
    if start_time:
        result["uptime_minutes"] = round((time.time() - start_time) / 60, 1)

    # Today's message count from event store
    try:
        from datetime import date
        from pathlib import Path

        from syll.agent.events import EventStore

        store = EventStore(Path.home() / ".syll")
        stats = store.get_day_stats(date.today().isoformat())
        result["today_count"] = stats.get("count", 0) if stats else 0
    except Exception:
        pass

    return result
