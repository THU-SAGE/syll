"""Coordinate transform API routes."""

import base64
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Request
from loguru import logger
from pydantic import BaseModel

router = APIRouter(prefix="/coord", tags=["coord"])


class TransformRequest(BaseModel):
    x: int
    y: int
    monitor_index: int = 0
    actor_type: str = "ui_tars"
    api_width: int = 1024
    api_height: int = 768


class ProbeRequest(BaseModel):
    x: int
    y: int
    monitor_index: int = 0
    actor_type: str = "ui_tars"
    api_width: int = 1024
    api_height: int = 768
    dry_run: bool = True


class CalibrateRequest(BaseModel):
    monitor_index: int = 0
    offset_dx: float = 0.0
    offset_dy: float = 0.0
    scale_sx: float = 1.0
    scale_sy: float = 1.0
    enabled: bool = True


def _get_service(request: Request) -> Any:
    from syll.agent.tools.coordinate_transform import CoordinateTransformService

    workspace = request.app.state.config.workspace_path
    profiles_dir = workspace / "coord_profiles"
    return CoordinateTransformService(profiles_dir)


def _make_actor(body: TransformRequest | ProbeRequest) -> Any:
    from syll.agent.tools.coordinate_transform import ActorSpace, ActorType

    return ActorSpace(
        actor_type=ActorType(body.actor_type),
        api_width=body.api_width,
        api_height=body.api_height,
    )


@router.get("/context")
async def get_context(request: Request):
    """Return FrameContext for all monitors."""
    service = _get_service(request)
    contexts = service.get_all_frame_contexts()
    return [asdict(c) for c in contexts]


@router.get("/screenshot")
async def get_screenshot(request: Request, monitor_index: int = 0):
    """Capture a screenshot and return base64 PNG + dimensions."""
    import mss
    from PIL import Image

    with mss.mss() as sct:
        mss_idx = monitor_index + 1
        if mss_idx >= len(sct.monitors):
            mss_idx = 1
        monitor = sct.monitors[mss_idx]
        shot = sct.grab(monitor)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

        logical_w = monitor["width"]
        logical_h = monitor["height"]
        if img.width > logical_w or img.height > logical_h:
            img = img.resize((logical_w, logical_h), Image.LANCZOS)

        import io

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()

    return {
        "image_b64": b64,
        "width": img.width,
        "height": img.height,
    }


@router.post("/transform")
async def transform(request: Request, body: TransformRequest):
    """Transform coordinates through the full pipeline."""
    service = _get_service(request)
    ctx = service.get_frame_context(body.monitor_index)
    actor = _make_actor(body)
    profile = service.load_profile(body.monitor_index)
    result = service.model_to_executor(body.x, body.y, ctx, actor, profile)
    return asdict(result)


@router.post("/probe")
async def probe(request: Request, body: ProbeRequest):
    """Transform + optionally execute a real click."""
    service = _get_service(request)
    ctx = service.get_frame_context(body.monitor_index)
    actor = _make_actor(body)
    profile = service.load_profile(body.monitor_index)
    result = service.model_to_executor(body.x, body.y, ctx, actor, profile)

    executed = False
    if not body.dry_run:
        try:
            import pyautogui

            pyautogui.FAILSAFE = True
            pyautogui.moveTo(result.executor_x, result.executor_y)
            pyautogui.click(result.executor_x, result.executor_y)
            executed = True
        except Exception as e:
            logger.error(f"Probe click failed: {e}")

    return {**asdict(result), "executed": executed}


@router.get("/profiles")
async def list_profiles(request: Request):
    """List all saved calibration profiles."""
    service = _get_service(request)
    profiles = service.list_profiles()
    return [asdict(p) for p in profiles]


@router.post("/calibrate")
async def calibrate(request: Request, body: CalibrateRequest):
    """Save a calibration profile."""
    from syll.agent.tools.coordinate_transform import CalibrationProfile

    service = _get_service(request)
    profile = CalibrationProfile(
        monitor_index=body.monitor_index,
        offset_dx=body.offset_dx,
        offset_dy=body.offset_dy,
        scale_sx=body.scale_sx,
        scale_sy=body.scale_sy,
        enabled=body.enabled,
    )
    service.save_profile(profile)
    return {"ok": True, "profile": asdict(profile)}
