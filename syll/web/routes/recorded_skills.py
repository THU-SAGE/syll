"""API routes for recorded workflow skill CRUD, import, execution, and scheduling.

Parallel to gui_skills.py — handles recorded demonstration skills.
"""

import base64
import mimetypes
from dataclasses import asdict
from pathlib import Path
from typing import Literal

import cv2
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from loguru import logger
from pydantic import BaseModel

from syll.agent.recorded_skill import (
    RecordedAction,
    RecordedSkill,
    RecordedSkillMeta,
    RecordedStep,
    RecordedTrace,
)
from syll.agent.tools.base import ToolResult
from syll.web.recorded_skill_imports import generate_recorded_trace, import_recorded_workflow

router = APIRouter(prefix="/recorded-skills", tags=["recorded-skills"])


# ---------- Crop / marker helpers ----------


def _crop_with_black_padding(
    frame, x: int, y: int, crop_size: int = 256
):
    """Crop a region centred on (x, y) with black padding for out-of-bounds areas."""
    h, w = frame.shape[:2]
    half = crop_size // 2
    x1 = int(x) - half
    y1 = int(y) - half
    x2 = x1 + crop_size
    y2 = y1 + crop_size

    pad_left = max(0, -x1)
    pad_top = max(0, -y1)
    pad_right = max(0, x2 - w)
    pad_bottom = max(0, y2 - h)

    x1c, y1c = max(0, x1), max(0, y1)
    x2c, y2c = min(w, x2), min(h, y2)
    crop = frame[y1c:y2c, x1c:x2c]

    if pad_left or pad_top or pad_right or pad_bottom:
        crop = cv2.copyMakeBorder(
            crop, pad_top, pad_bottom, pad_left, pad_right,
            borderType=cv2.BORDER_CONSTANT, value=(0, 0, 0),
        )
    if crop.shape[:2] != (crop_size, crop_size):
        crop = cv2.resize(crop, (crop_size, crop_size), interpolation=cv2.INTER_AREA)
    return crop


def _draw_x_marker(
    crop, x_size: int = 30, x_thick: int = 6
):
    """Draw a red X with white outline at the centre of a crop image."""
    crop_size = crop.shape[0]
    cx = cy = crop_size // 2
    outline = x_thick + 2
    max_half = (crop_size // 2) - 1 - outline
    half_x = max(1, min(x_size // 2, max_half))

    overlay = crop.copy()
    cv2.line(overlay, (cx - half_x, cy - half_x), (cx + half_x, cy + half_x),
             (255, 255, 255), outline)
    cv2.line(overlay, (cx - half_x, cy + half_x), (cx + half_x, cy - half_x),
             (255, 255, 255), outline)
    cv2.line(overlay, (cx - half_x, cy - half_x), (cx + half_x, cy + half_x),
             (0, 0, 255), x_thick)
    cv2.line(overlay, (cx - half_x, cy + half_x), (cx + half_x, cy - half_x),
             (0, 0, 255), x_thick)
    return cv2.addWeighted(overlay, 0.5, crop, 0.5, 0)


# ---------- Request models ----------


class RecordedActionModel(BaseModel):
    type: str
    coordinates: list[int] | None = None
    end_coordinates: list[int] | None = None
    content: str = ""
    description: str = ""


class RecordedTraceModel(BaseModel):
    observation: str = ""
    think: str = ""
    action: str = ""
    expectation: str = ""


class CreateRecordedSkillRequest(BaseModel):
    name: str
    description: str = ""
    app_context: str = ""
    source: str = "manual"
    actor_mode: str = "ui-tars"


class ImportRecordingRequest(BaseModel):
    project_path: str
    description: str = ""
    auto_trace: bool = False


class RecordedStepCreate(BaseModel):
    action: RecordedActionModel
    screenshot_b64: str
    screenshot_mime: str = "image/jpeg"
    trace: RecordedTraceModel | None = None


class UpdateStepRequest(BaseModel):
    action: RecordedActionModel | None = None
    trace: RecordedTraceModel | None = None
    description: str | None = None


class ExecuteRequest(BaseModel):
    instruction: str = ""
    mode: Literal["planner", "icl-rich"] = "planner"
    actor_mode: str = ""  # empty = fallback to skill meta


class ScheduleRequest(BaseModel):
    cron_expr: str | None = None
    every_seconds: int | None = None
    instruction: str = ""
    mode: Literal["planner", "icl-rich"] = "planner"
    actor_mode: str = ""  # empty = fallback to skill meta


# ---------- Helpers ----------


def _get_store(request: Request):
    return getattr(request.app.state, "recorded_skill_store", None) or getattr(
        request.app.state, "aloha_skill_store", None
    )


def _mime_to_ext(mime: str) -> str:
    ext = mimetypes.guess_extension(mime)
    return ext or ".jpg"


def _encode_screenshots(paths: list[str]) -> list[dict]:
    """Convert screenshot file paths to base64 dicts for the frontend."""
    result = []
    for p in paths:
        path = Path(p)
        if path.exists():
            data = base64.b64encode(path.read_bytes()).decode()
            mime = mimetypes.guess_type(str(path))[0] or "image/png"
            result.append({"data": data, "mime": mime, "filename": path.name})
    return result


def _auto_fill_description(skill: RecordedSkill) -> str:
    """Derive a short description from trace data when meta.description is empty."""
    if skill.steps and skill.steps[0].trace:
        return skill.steps[0].trace.think or skill.steps[0].trace.action or skill.meta.name
    return skill.meta.name


def _build_icl_rich_message(name: str, instruction: str) -> str:
    """Build a deterministic prompt for rich recorded-skill ICL replay."""
    return (
        f'Execute the GUI demonstration "{name}": {instruction}. '
        f'Use gui_action(instruction="{instruction}", skill_name="{name}") '
        "to replay the recorded steps with full image+text ICL examples enabled. "
        "Prefer the recorded workflow context over free-form exploration."
    )


# ---------- Skill CRUD ----------


@router.post("")
async def create_recorded_skill(body: CreateRecordedSkillRequest, request: Request):
    """Create an empty recorded skill."""
    store = _get_store(request)
    # Issue 12: Auto-fill description from name if empty
    description = body.description or body.name
    skill = RecordedSkill(
        meta=RecordedSkillMeta(
            name=body.name,
            description=description,
            app_context=body.app_context,
            source=body.source,
            actor_mode=body.actor_mode,
        ),
        steps=[],
    )
    store.save_skill(skill)
    return {"name": body.name, "status": "created"}


@router.get("")
async def list_recorded_skills(request: Request):
    """List all recorded skills."""
    store = _get_store(request)
    skills = store.list_skills()
    # Auto-fill empty descriptions from trace data
    for s in skills:
        if not s.get("description"):
            skill = store.load_skill(s["name"])
            if skill:
                s["description"] = _auto_fill_description(skill)
    return skills


@router.get("/{name}")
async def get_recorded_skill(name: str, request: Request):
    """Get a recorded skill with all steps."""
    store = _get_store(request)
    skill = store.load_skill(name)
    if not skill:
        raise HTTPException(404, f"Recorded skill '{name}' not found")
    return {
        "meta": asdict(skill.meta),
        "steps": [asdict(s) for s in skill.steps],
        "trajectory": skill.trajectory,
    }


@router.delete("/{name}")
async def delete_recorded_skill(name: str, request: Request):
    """Delete a recorded skill."""
    store = _get_store(request)
    if not store.delete_skill(name):
        raise HTTPException(404, f"Recorded skill '{name}' not found")
    return {"status": "deleted"}


# ---------- Import ----------


@router.post("/{name}/import")
async def import_recording(name: str, body: ImportRecordingRequest, request: Request):
    """Import a recorded workflow project."""
    store = _get_store(request)

    # Issue 12: Auto-fill description from name if empty
    description = body.description or name

    try:
        skill = await import_recorded_workflow(
            store=store,
            config=getattr(request.app.state, "config", None),
            project_path=body.project_path,
            skill_name=name,
            description=description,
            auto_trace=body.auto_trace,
        )
        return {
            "name": name,
            "status": "imported",
            "steps": len(skill.steps),
            "trace_generated": skill.meta.trace_generated,
        }
    except Exception as e:
        logger.error(f"Import failed: {e}")
        raise HTTPException(500, f"Import failed: {e}")


@router.post("/{name}/generate-trace")
async def generate_trace(name: str, request: Request):
    """Generate or regenerate traces for an existing skill."""
    store = _get_store(request)

    try:
        skill = await generate_recorded_trace(
            store=store,
            config=getattr(request.app.state, "config", None),
            skill_name=name,
        )
        return {
            "name": name,
            "status": "trace_generated",
            "steps": len(skill.trajectory),
        }
    except Exception as e:
        logger.error(f"Trace generation failed: {e}")
        raise HTTPException(500, f"Trace generation failed: {e}")


# ---------- Step CRUD ----------


@router.post("/{name}/steps")
async def add_step(name: str, body: RecordedStepCreate, request: Request):
    """Add a step with a screenshot and action annotation."""
    store = _get_store(request)
    skill = store.load_skill(name)
    if not skill:
        raise HTTPException(404, f"Recorded skill '{name}' not found")

    idx = len(skill.steps) + 1
    image_bytes = base64.b64decode(body.screenshot_b64)
    ext = _mime_to_ext(body.screenshot_mime)
    filename = store.save_keyframe(name, idx, image_bytes, f"_full{ext}")

    action = RecordedAction(
        type=body.action.type,
        coordinates=body.action.coordinates,
        end_coordinates=body.action.end_coordinates,
        content=body.action.content,
        description=body.action.description,
    )

    trace = None
    if body.trace:
        trace = RecordedTrace(
            observation=body.trace.observation,
            think=body.trace.think,
            action=body.trace.action,
            expectation=body.trace.expectation,
        )

    step = RecordedStep(index=idx, screenshot=filename, action=action, trace=trace)
    skill.steps.append(step)
    store.save_skill(skill)

    return {"index": idx, "filename": filename}


@router.put("/{name}/steps/{idx}")
async def update_step(name: str, idx: int, body: UpdateStepRequest, request: Request):
    """Update a step's action, trace, or description."""
    store = _get_store(request)
    skill = store.load_skill(name)
    if not skill:
        raise HTTPException(404, f"Recorded skill '{name}' not found")

    target = next((s for s in skill.steps if s.index == idx), None)
    if not target:
        raise HTTPException(404, f"Step {idx} not found")

    old_coords = target.action.coordinates

    if body.action:
        target.action = RecordedAction(
            type=body.action.type,
            coordinates=body.action.coordinates,
            end_coordinates=body.action.end_coordinates,
            content=body.action.content,
            description=body.action.description,
        )
    if body.trace:
        target.trace = RecordedTrace(
            observation=body.trace.observation,
            think=body.trace.think,
            action=body.trace.action,
            expectation=body.trace.expectation,
        )
    if body.description is not None:
        target.action.description = body.description

    # Regenerate crop when coordinates changed
    new_coords = target.action.coordinates
    if body.action and new_coords and new_coords != old_coords:
        full_path = store.get_keyframe_path(name, f"step_{idx:02d}_full.jpg")
        if full_path:
            frame = cv2.imread(str(full_path))
            if frame is not None:
                crop = _crop_with_black_padding(frame, new_coords[0], new_coords[1])
                # Draw X marker for click-type actions
                if target.action.type in ("click", "left_click", "right_click", "double_click"):
                    crop = _draw_x_marker(crop)
                _, buf = cv2.imencode(".jpg", crop)
                crop_filename = store.save_keyframe(name, idx, buf.tobytes(), "_crop.jpg")
                target.screenshot_crop = crop_filename

    store.save_skill(skill)
    return {"status": "updated", "screenshot_crop": target.screenshot_crop}


@router.delete("/{name}/steps/{idx}")
async def delete_step(name: str, idx: int, request: Request):
    """Delete a step and re-index."""
    store = _get_store(request)
    skill = store.load_skill(name)
    if not skill:
        raise HTTPException(404, f"Recorded skill '{name}' not found")

    skill.steps = [s for s in skill.steps if s.index != idx]
    for i, s in enumerate(skill.steps):
        s.index = i + 1
    store.save_skill(skill)
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
async def execute_recorded_skill(name: str, request: Request, body: ExecuteRequest | None = None):
    """Execute a recorded skill via the agent loop."""
    store = _get_store(request)
    skill = store.load_skill(name)
    if not skill:
        raise HTTPException(404, f"Recorded skill '{name}' not found")

    instruction = ""
    mode = "planner"
    actor_mode = ""

    if body:
        instruction = body.instruction
        mode = body.mode
        actor_mode = body.actor_mode

    if not instruction:
        instruction = skill.meta.description or skill.meta.name

    # Issue 3: Fallback actor_mode to skill meta
    if not actor_mode:
        actor_mode = getattr(skill.meta, 'actor_mode', '') or "ui-tars"

    agent_loop = request.app.state.agent_loop

    if mode == "planner":
        # Issue 6: Use ToolRegistry.execute() for schema validation & error wrapping
        params = {
            "instruction": instruction,
            "skill_name": name,
            "actor_mode": actor_mode,
        }
        result = await agent_loop.tools.execute("gui_action_planned", params)
        if isinstance(result, str) and result.startswith("Error:"):
            # ToolRegistry returns error strings for missing tools or validation failures
            if "not found" in result:
                raise HTTPException(
                    503,
                    "GUI tools not enabled. Please enable gui.enabled in config "
                    "and restart (or save config to trigger hot reload).",
                )
            raise HTTPException(500, result)
        if isinstance(result, ToolResult):
            screenshots_b64 = _encode_screenshots(result.media)
            return {
                "status": "executed",
                "mode": mode,
                "response": result.text,
                "screenshots": screenshots_b64,
            }
        return {"status": "executed", "mode": mode, "response": str(result)}
    else:
        message = _build_icl_rich_message(name, instruction)
        try:
            agent_result = await agent_loop.process_direct(
                content=message,
                session_key=f"recorded-skill:{name}",
            )
            return {
                "status": "executed",
                "mode": mode,
                "response": agent_result.text,
                "media": list(agent_result.media),
            }
        except Exception as e:
            logger.error(f"Recorded skill execution failed: {e}")
            raise HTTPException(500, str(e))


@router.post("/{name}/schedule")
async def schedule_recorded_skill(name: str, body: ScheduleRequest, request: Request):
    """Schedule a recorded skill for cron execution."""
    store = _get_store(request)
    skill = store.load_skill(name)
    if not skill:
        raise HTTPException(404, f"Recorded skill '{name}' not found")

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

    instruction = body.instruction or skill.meta.description
    # Issue 3: Resolve actor_mode from request or skill meta
    actor_mode = body.actor_mode or getattr(skill.meta, 'actor_mode', '') or "ui-tars"

    if body.mode == "planner":
        message = (
            f'Execute GUI task "{instruction}" with planner. '
            f'Use gui_action_planned(instruction="{instruction}", '
            f'skill_name="{name}", actor_mode="{actor_mode}").'
        )
    else:
        message = _build_icl_rich_message(name, instruction)

    job = cron_service.add_job(
        name=f"recorded-skill:{name}",
        schedule=schedule,
        message=message,
        metadata={
            "action_type": "recorded_skill",
            "skill_name": name,
            "workflow_mode": body.mode,
            "workflow_actor_mode": actor_mode,
        },
    )
    return {"status": "scheduled", "job_id": job.id}
