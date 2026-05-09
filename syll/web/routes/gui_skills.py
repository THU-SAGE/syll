"""API routes for GUI Skill CRUD, execution, and scheduling."""

import base64
import mimetypes
from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from loguru import logger
from pydantic import BaseModel

from syll.agent.gui_skill import GUIAction, GUISkill, GUISkillMeta, GUIStep

router = APIRouter(prefix="/gui-skills", tags=["gui-skills"])


# ---------- Request models ----------


class GUIActionModel(BaseModel):
    type: str
    coordinates: list[int] | None = None
    end_coordinates: list[int] | None = None
    content: str = ""
    description: str = ""


class CreateGUISkillRequest(BaseModel):
    name: str
    description: str = ""
    app_context: str = ""


class GUIStepCreate(BaseModel):
    action: GUIActionModel
    screenshot_b64: str
    screenshot_mime: str = "image/webp"


class UpdateStepRequest(BaseModel):
    action: GUIActionModel | None = None
    description: str | None = None


class ScheduleRequest(BaseModel):
    cron_expr: str | None = None
    every_seconds: int | None = None


# ---------- Helpers ----------


def _get_store(request: Request):
    return request.app.state.gui_skill_store


def _mime_to_ext(mime: str) -> str:
    ext = mimetypes.guess_extension(mime)
    return ext or ".webp"


# ---------- Skill CRUD ----------


@router.post("")
async def create_gui_skill(body: CreateGUISkillRequest, request: Request):
    """Create an empty GUI skill."""
    store = _get_store(request)
    skill = GUISkill(
        meta=GUISkillMeta(
            name=body.name,
            description=body.description,
            app_context=body.app_context,
        ),
        steps=[],
    )
    store.save_skill(skill)
    return {"name": body.name, "status": "created"}


@router.get("")
async def list_gui_skills(request: Request):
    """List all GUI skills."""
    store = _get_store(request)
    return store.list_gui_skills()


@router.get("/{name}")
async def get_gui_skill(name: str, request: Request):
    """Get a GUI skill with all steps."""
    store = _get_store(request)
    skill = store.load_skill(name)
    if not skill:
        raise HTTPException(404, f"GUI skill '{name}' not found")
    return {
        "meta": asdict(skill.meta),
        "steps": [asdict(s) for s in skill.steps],
    }


@router.delete("/{name}")
async def delete_gui_skill(name: str, request: Request):
    """Delete a GUI skill."""
    store = _get_store(request)
    if not store.delete_skill(name):
        raise HTTPException(404, f"GUI skill '{name}' not found")
    return {"status": "deleted"}


# ---------- Step CRUD ----------


@router.post("/{name}/steps")
async def add_step(name: str, body: GUIStepCreate, request: Request):
    """Add a step with a screenshot and action annotation."""
    store = _get_store(request)
    skill = store.load_skill(name)
    if not skill:
        raise HTTPException(404, f"GUI skill '{name}' not found")

    idx = len(skill.steps) + 1
    image_bytes = base64.b64decode(body.screenshot_b64)
    ext = _mime_to_ext(body.screenshot_mime)
    filename = store.save_keyframe(name, idx, image_bytes, ext)

    action = GUIAction(
        type=body.action.type,
        coordinates=body.action.coordinates,
        end_coordinates=body.action.end_coordinates,
        content=body.action.content,
        description=body.action.description,
    )
    step = GUIStep(index=idx, screenshot_before=filename, action=action)
    skill.steps.append(step)
    store.save_skill(skill)

    return {"index": idx, "filename": filename}


@router.put("/{name}/steps/{idx}")
async def update_step(name: str, idx: int, body: UpdateStepRequest, request: Request):
    """Update a step's action or description."""
    store = _get_store(request)
    skill = store.load_skill(name)
    if not skill:
        raise HTTPException(404, f"GUI skill '{name}' not found")

    target = next((s for s in skill.steps if s.index == idx), None)
    if not target:
        raise HTTPException(404, f"Step {idx} not found")

    if body.action:
        target.action = GUIAction(
            type=body.action.type,
            coordinates=body.action.coordinates,
            end_coordinates=body.action.end_coordinates,
            content=body.action.content,
            description=body.action.description,
        )
    if body.description is not None:
        target.action.description = body.description

    store.save_skill(skill)
    return {"status": "updated"}


@router.delete("/{name}/steps/{idx}")
async def delete_step(name: str, idx: int, request: Request):
    """Delete a step and re-index."""
    store = _get_store(request)
    skill = store.delete_step(name, idx)
    if not skill:
        raise HTTPException(404, f"GUI skill '{name}' not found")
    return {"status": "deleted", "remaining_steps": len(skill.steps)}


# ---------- Keyframe serving ----------


@router.get("/{name}/keyframes/{filename}")
async def get_keyframe(name: str, filename: str, request: Request):
    """Serve a keyframe image."""
    store = _get_store(request)
    path = store.get_keyframe_path(name, filename)
    if not path:
        raise HTTPException(404, "Keyframe not found")
    return FileResponse(path)


# ---------- Execute & Schedule ----------


@router.post("/{name}/execute")
async def execute_gui_skill(name: str, request: Request):
    """Trigger immediate execution of a GUI skill via the agent loop."""
    store = _get_store(request)
    skill = store.load_skill(name)
    if not skill:
        raise HTTPException(404, f"GUI skill '{name}' not found")

    agent_loop = request.app.state.agent_loop
    message = (
        f'Execute the GUI demonstration "{name}": {skill.meta.description}. '
        f'Use gui_action(instruction="{skill.meta.description}", skill_name="{name}") '
        f"to replay the recorded steps."
    )
    try:
        result = await agent_loop.process_direct(
            content=message,
            session_key=f"gui-skill:{name}",
        )
        return {
            "status": "executed",
            "response": result.text,
            "media": list(result.media),
        }
    except Exception as e:
        logger.error(f"GUI skill execution failed: {e}")
        raise HTTPException(500, str(e))


@router.post("/{name}/schedule")
async def schedule_gui_skill(name: str, body: ScheduleRequest, request: Request):
    """Schedule a GUI skill for cron execution."""
    store = _get_store(request)
    skill = store.load_skill(name)
    if not skill:
        raise HTTPException(404, f"GUI skill '{name}' not found")

    cron_service = getattr(request.app.state, "cron_service", None)
    if not cron_service:
        raise HTTPException(503, "Cron service not available")

    from syll.cron.service import _compute_next_run, _now_ms
    from syll.cron.types import CronSchedule

    if body.cron_expr:
        schedule = CronSchedule(kind="cron", expr=body.cron_expr)
    elif body.every_seconds:
        schedule = CronSchedule(kind="every", every_ms=body.every_seconds * 1000)
    else:
        raise HTTPException(400, "Provide cron_expr or every_seconds")

    # v3: validate the schedule is actually executable
    if _compute_next_run(schedule, _now_ms()) is None:
        raise HTTPException(400, "Invalid or expired schedule")

    message = (
        f'Execute the GUI demonstration "{name}": {skill.meta.description}. '
        f'Use gui_action(instruction="{skill.meta.description}", skill_name="{name}") '
        f"to replay the recorded steps."
    )
    job = cron_service.add_job(
        name=f"gui-skill:{name}",
        schedule=schedule,
        message=message,
        metadata={"action_type": "gui_skill", "skill_name": name},
    )
    return {"status": "scheduled", "job_id": job.id}
