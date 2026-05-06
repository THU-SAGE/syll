"""Intent clarification route for the ghost functional dashboard.

Single POST endpoint that drives a short clarification dialogue via
``app.state.intent_clarifier`` and returns a structured pre-fill payload
the frontend can feed into the existing Skills / Schedule create modals.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from loguru import logger
from pydantic import BaseModel

router = APIRouter(prefix="/intent", tags=["intent"])


class ClarifyRequest(BaseModel):
    session_id: str | None = None
    text: str


@router.post("/clarify")
async def clarify(body: ClarifyRequest, request: Request):
    clarifier = getattr(request.app.state, "intent_clarifier", None)
    if clarifier is None or clarifier.provider is None:
        raise HTTPException(503, "intent clarifier not available (no LLM provider)")
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(422, "text must be non-empty")
    try:
        result = await clarifier.clarify(body.session_id, text)
    except Exception as e:
        logger.error(f"intent clarifier failed: {e}")
        raise HTTPException(500, f"intent clarifier failed: {e}") from e
    return result.model_dump()
