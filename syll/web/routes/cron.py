"""Unified cron REST API for Schedule Tab.

Supports 3 action types (message / gui_skill / recorded_skill) x 3 schedule
types (cron / every / at). Includes deliver validation, schedule validation,
and run-now with state read-back.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from syll.cron.service import _compute_next_run, _now_ms
from syll.cron.types import CronJob, CronSchedule

router = APIRouter(prefix="/cron", tags=["cron"])


# ---------- Pydantic models ----------


class CreateCronJobRequest(BaseModel):
    name: str
    action_type: Literal["message", "gui_skill", "recorded_skill", "aloha_skill"]
    # action fields
    message: str | None = None
    skill_name: str | None = None
    workflow_mode: Literal["planner", "icl-rich"] = "planner"
    workflow_actor_mode: str | None = None
    # Backward-compatible aliases for older clients.
    aloha_mode: Literal["planner", "icl-rich"] | None = None
    aloha_actor_mode: str | None = None
    # schedule fields
    schedule_type: Literal["cron", "every", "at"]
    cron_expr: str | None = None
    every_seconds: int | None = None
    at_ms: int | None = None  # epoch ms from client
    # delivery fields (gateway mode only)
    deliver: bool = False
    channel: str | None = None
    to: str | None = None


class PatchCronJobRequest(BaseModel):
    enabled: bool


# ---------- Helpers ----------


def _parse_legacy_metadata(job: CronJob) -> dict:
    """Derive action metadata from job.name prefix for legacy jobs."""
    if job.payload.metadata:
        metadata = dict(job.payload.metadata)
        if metadata.get("action_type") == "aloha_skill":
            metadata["action_type"] = "recorded_skill"
        if "workflow_mode" not in metadata and "aloha_mode" in metadata:
            metadata["workflow_mode"] = metadata["aloha_mode"]
        if "workflow_actor_mode" not in metadata and "aloha_actor_mode" in metadata:
            metadata["workflow_actor_mode"] = metadata["aloha_actor_mode"]
        return metadata
    name = job.name or ""
    if name.startswith("gui-skill:"):
        return {"action_type": "gui_skill", "skill_name": name[len("gui-skill:"):]}
    if name.startswith("recorded-skill:"):
        return {"action_type": "recorded_skill", "skill_name": name[len("recorded-skill:"):]}
    if name.startswith("aloha-skill:"):
        return {"action_type": "recorded_skill", "skill_name": name[len("aloha-skill:"):]}
    return {"action_type": "message"}


def _action_type(body: CreateCronJobRequest) -> str:
    """Return the canonical action type."""
    return "recorded_skill" if body.action_type == "aloha_skill" else body.action_type


def _workflow_mode(body: CreateCronJobRequest) -> str:
    """Return the canonical workflow mode, honoring legacy aliases."""
    return body.aloha_mode or body.workflow_mode


def _workflow_actor_mode(body: CreateCronJobRequest) -> str | None:
    """Return the canonical workflow actor mode, honoring legacy aliases."""
    return body.aloha_actor_mode or body.workflow_actor_mode


def _serialize_job(job: CronJob) -> dict:
    """Serialize a CronJob for API responses."""
    metadata = _parse_legacy_metadata(job)
    return {
        "id": job.id,
        "name": job.name,
        "enabled": job.enabled,
        "schedule": {
            "kind": job.schedule.kind,
            "at_ms": job.schedule.at_ms,
            "every_ms": job.schedule.every_ms,
            "expr": job.schedule.expr,
        },
        "payload": {
            "message": job.payload.message,
            "deliver": job.payload.deliver,
            "channel": job.payload.channel,
            "to": job.payload.to,
            "metadata": metadata,
        },
        "state": {
            "next_run_at_ms": job.state.next_run_at_ms,
            "last_run_at_ms": job.state.last_run_at_ms,
            "last_status": job.state.last_status,
            "last_error": job.state.last_error,
        },
        "created_at_ms": job.created_at_ms,
        "updated_at_ms": job.updated_at_ms,
    }


def _build_message(
    body: CreateCronJobRequest,
    skill: object | None,
) -> str:
    """Build the agent-facing message based on action_type."""
    action_type = _action_type(body)

    if action_type == "message":
        return body.message or ""

    if action_type == "gui_skill":
        desc = getattr(getattr(skill, "meta", None), "description", "") or body.skill_name
        return (
            f'Execute the GUI demonstration "{body.skill_name}": {desc}. '
            f'Use gui_action(instruction="{desc}", skill_name="{body.skill_name}") '
            "to replay the recorded steps."
        )

    if action_type == "recorded_skill":
        meta = getattr(skill, "meta", None)
        desc = getattr(meta, "description", "") or body.skill_name
        actor_mode = _workflow_actor_mode(body) or getattr(meta, "actor_mode", "") or "ui-tars"
        if _workflow_mode(body) == "planner":
            return (
                f'Execute GUI task "{desc}" with planner. '
                f'Use gui_action_planned(instruction="{desc}", '
                f'skill_name="{body.skill_name}", actor_mode="{actor_mode}").'
            )
        # icl-rich
        return (
            f'Execute the GUI demonstration "{body.skill_name}": {desc}. '
            f'Use gui_action(instruction="{desc}", skill_name="{body.skill_name}") '
            "to replay the recorded steps with full image+text ICL examples enabled. "
            "Prefer the recorded workflow context over free-form exploration."
        )

    return ""


def _build_metadata(body: CreateCronJobRequest) -> dict:
    """Build structured metadata for the job."""
    action_type = _action_type(body)
    meta = {"action_type": action_type}
    if action_type in ("gui_skill", "recorded_skill"):
        meta["skill_name"] = body.skill_name
    if action_type == "recorded_skill":
        meta["workflow_mode"] = _workflow_mode(body)
        actor_mode = _workflow_actor_mode(body)
        if actor_mode:
            meta["workflow_actor_mode"] = actor_mode
    return meta


def _build_schedule(body: CreateCronJobRequest) -> CronSchedule:
    """Build a CronSchedule from request."""
    if body.schedule_type == "cron":
        if not body.cron_expr:
            raise HTTPException(400, "cron_expr is required for schedule_type='cron'")
        return CronSchedule(kind="cron", expr=body.cron_expr)
    if body.schedule_type == "every":
        if not body.every_seconds or body.every_seconds <= 0:
            raise HTTPException(400, "every_seconds must be a positive integer")
        return CronSchedule(kind="every", every_ms=body.every_seconds * 1000)
    if body.schedule_type == "at":
        if not body.at_ms:
            raise HTTPException(400, "at_ms is required for schedule_type='at'")
        return CronSchedule(kind="at", at_ms=body.at_ms)
    raise HTTPException(400, f"Unknown schedule_type: {body.schedule_type}")


def _require_cron_service(request: Request):
    cron_service = getattr(request.app.state, "cron_service", None)
    if not cron_service:
        raise HTTPException(503, "Cron service not available")
    return cron_service


def validate_deliver_settings(
    channel_manager, channel: str | None, to: str | None
) -> None:
    """Shared delivery-settings validator. Raises HTTPException(400) on invalid.

    Reused by this module's own `_validate_deliver` and by
    `syll/web/routes/rituals.py` so the two install paths stay in sync.
    """
    if channel_manager is None:
        raise HTTPException(
            400,
            "Delivery unavailable in web-only mode. Use 'syll gateway' to enable channel delivery.",
        )
    enabled = set(getattr(channel_manager, "enabled_channels", []) or [])
    if not channel or channel not in enabled:
        raise HTTPException(
            400,
            f"Channel '{channel}' is not enabled. Available: {sorted(enabled) or '[]'}",
        )
    if not to:
        raise HTTPException(400, "Delivery target (to) is required when deliver=true")


def _validate_deliver(request: Request, body: CreateCronJobRequest) -> None:
    """Validate delivery options against runtime channel manager."""
    if not body.deliver:
        return
    cm = getattr(request.app.state, "channel_manager", None)
    validate_deliver_settings(cm, body.channel, body.to)


# ---------- Endpoints ----------


@router.get("/capabilities")
async def get_capabilities(request: Request):
    """Return deliver availability + enabled channels for frontend rendering."""
    cm = getattr(request.app.state, "channel_manager", None)
    enabled_channels = sorted(getattr(cm, "enabled_channels", []) or []) if cm else []
    return {
        "deliver_available": cm is not None and len(enabled_channels) > 0,
        "enabled_channels": enabled_channels,
    }


@router.get("/jobs")
async def list_jobs(request: Request):
    """List all cron jobs (including disabled)."""
    cron_service = _require_cron_service(request)
    jobs = cron_service.list_jobs(include_disabled=True)
    return {"jobs": [_serialize_job(j) for j in jobs]}


@router.post("/jobs")
async def create_job(body: CreateCronJobRequest, request: Request):
    """Create a new cron job."""
    cron_service = _require_cron_service(request)

    # 1. Validate action-type specific fields
    skill = None
    if body.action_type == "message":
        if not body.message:
            raise HTTPException(400, "message is required for action_type='message'")
    elif body.action_type == "gui_skill":
        if not body.skill_name:
            raise HTTPException(400, "skill_name is required for action_type='gui_skill'")
        store = getattr(request.app.state, "gui_skill_store", None)
        if store is None:
            raise HTTPException(503, "GUI skill store not available")
        skill = store.load_skill(body.skill_name)
        if not skill:
            raise HTTPException(404, f"GUI skill '{body.skill_name}' not found")
    elif _action_type(body) == "recorded_skill":
        if not body.skill_name:
            raise HTTPException(400, "skill_name is required for action_type='recorded_skill'")
        store = getattr(request.app.state, "recorded_skill_store", None) or getattr(request.app.state, "aloha_skill_store", None)
        if store is None:
            raise HTTPException(503, "Recorded skill store not available")
        skill = store.load_skill(body.skill_name)
        if not skill:
            raise HTTPException(404, f"Recorded skill '{body.skill_name}' not found")

    # 2. Validate deliver options (may raise 400)
    _validate_deliver(request, body)

    # 3. Build schedule
    schedule = _build_schedule(body)

    # 4. Validate schedule is actually executable
    next_run = _compute_next_run(schedule, _now_ms())
    if next_run is None:
        raise HTTPException(400, "Invalid or expired schedule")

    # 5. Build message + metadata
    message = _build_message(body, skill)
    metadata = _build_metadata(body)

    # 6. Create the job
    job = cron_service.add_job(
        name=body.name,
        schedule=schedule,
        message=message,
        deliver=body.deliver,
        channel=body.channel,
        to=body.to,
        metadata=metadata,
    )
    return {"job": _serialize_job(job)}


@router.delete("/jobs/{job_id}")
async def delete_job(job_id: str, request: Request):
    """Delete a cron job."""
    cron_service = _require_cron_service(request)
    ok = cron_service.remove_job(job_id)
    if not ok:
        raise HTTPException(404, f"Job '{job_id}' not found")
    return {"ok": True}


@router.patch("/jobs/{job_id}")
async def patch_job(job_id: str, body: PatchCronJobRequest, request: Request):
    """Enable or disable a job. Rolls back on zombie re-enable."""
    cron_service = _require_cron_service(request)
    updated = cron_service.enable_job(job_id, body.enabled)
    if updated is None:
        raise HTTPException(404, f"Job '{job_id}' not found")

    # v3: detect enabled-but-no-next-run zombies and roll back
    if body.enabled and updated.state.next_run_at_ms is None:
        cron_service.enable_job(job_id, False)
        raise HTTPException(
            400,
            "Cannot re-enable: schedule invalid or expired. Recreate the job with a fresh schedule.",
        )
    return {"job": _serialize_job(updated)}


@router.post("/jobs/{job_id}/run")
async def run_job_now(job_id: str, request: Request):
    """Manually trigger a job (even if disabled). Reads back state for ok/error."""
    cron_service = _require_cron_service(request)
    ok = await cron_service.run_job(job_id, force=True)
    if not ok:
        raise HTTPException(404, f"Job '{job_id}' not found")

    # Read back latest state (set by _execute_job → _save_store)
    job = cron_service.get_job(job_id)
    if job is None:
        # Deleted between run and readback (e.g. delete_after_run=True)
        return {"ok": True, "job": None}
    if job.state.last_status == "error":
        return {"ok": False, "error": job.state.last_error, "job": _serialize_job(job)}
    return {"ok": True, "job": _serialize_job(job)}
