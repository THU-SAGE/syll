"""Sessions API routes."""

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(tags=["sessions"])

_PUBLIC_MESSAGE_OMIT_KEYS = {"reasoning_content", "provider_extra"}


def _public_message(message: dict):
    """Return a user-facing copy without provider-private replay metadata."""
    return {
        key: value
        for key, value in message.items()
        if key not in _PUBLIC_MESSAGE_OMIT_KEYS
    }


@router.get("/sessions")
async def list_sessions(request: Request):
    """List all sessions."""
    sm = request.app.state.session_manager
    return sm.list_sessions()


@router.get("/sessions/{key:path}")
async def get_session(key: str, request: Request):
    """Get session history."""
    sm = request.app.state.session_manager
    session = sm.get_or_create(key)
    return {
        "key": session.key,
        "created_at": session.created_at.isoformat(),
        "updated_at": session.updated_at.isoformat(),
        "messages": [_public_message(message) for message in session.messages],
    }


@router.delete("/sessions/{key:path}")
async def delete_session(key: str, request: Request):
    """Delete a session."""
    sm = request.app.state.session_manager
    if sm.delete(key):
        return {"ok": True}
    raise HTTPException(status_code=404, detail="Session not found")
