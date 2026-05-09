"""Recorder API routes for the Syll capture editor."""

from __future__ import annotations

import asyncio
import copy
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from syll.agent.aloha.learn.log_processor import LogProcessor
from syll.recorder.manager import RecorderConflict
from syll.web.recorded_skill_imports import import_recorded_workflow

router = APIRouter(prefix="/recorder", tags=["recorder"])

_DRAFT_FILENAME = "workflow_draft.json"
_BROWSER_VIDEO_SUFFIX = ".browser.mp4"
_video_transcode_locks: dict[str, threading.Lock] = {}
_video_transcode_locks_guard = threading.Lock()


class StartRecorderRequest(BaseModel):
    project: str = Field(min_length=1)
    output_dir: str | None = None
    fps: int = Field(default=15, ge=1, le=60)
    monitor: int = Field(default=0, ge=0)


class ImportRecorderRequest(BaseModel):
    name: str = Field(min_length=1)
    description: str = ""
    auto_trace: bool = False


class RecorderDraftStep(BaseModel):
    id: str | None = None
    index: int | None = None
    timestamp: float = Field(default=0.0, ge=0)
    keyframe_timestamp: float = Field(default=0.0, ge=0)
    action: str = Field(min_length=1)
    label: str = ""
    note: str = ""
    window: str = ""
    coordinates: list[int] | None = None
    end_coordinates: list[int] | None = None
    path: list[list[int]] | None = None
    scroll_count: int = Field(default=0, ge=0)
    deleted: bool = False
    edited: bool = False


class RecorderDraftRequest(BaseModel):
    trajectory: list[RecorderDraftStep] = Field(default_factory=list)


def _get_manager(request: Request):
    return request.app.state.recorder_manager


def _sse_message(event: str, data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def _latest_project_path(manager) -> Path:
    return manager.get_project_path()


def _draft_path(project_path: Path) -> Path:
    return project_path / _DRAFT_FILENAME


def _load_draft(project_path: Path) -> dict[str, Any] | None:
    path = _draft_path(project_path)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _write_draft(project_path: Path, draft: dict[str, Any]) -> None:
    path = _draft_path(project_path)
    path.write_text(json.dumps(draft, indent=2, ensure_ascii=False), encoding="utf-8")


def _delete_draft(project_path: Path) -> None:
    path = _draft_path(project_path)
    if path.exists():
        path.unlink()


def _find_recording_artifacts(project_path: Path) -> tuple[Path, Path]:
    inputs_dir = project_path / "inputs"
    if not inputs_dir.exists():
        inputs_dir = project_path

    video_files = sorted(inputs_dir.glob("*.mp4"))
    log_files = sorted(inputs_dir.glob("*.txt")) + sorted(inputs_dir.glob("*.log"))
    if not video_files:
        raise FileNotFoundError(f"No video file found in {inputs_dir}")
    if not log_files:
        raise FileNotFoundError(f"No log file found in {inputs_dir}")
    return video_files[0], log_files[0]


def _browser_video_path(video_path: Path) -> Path:
    return video_path.with_name(f"{video_path.stem}{_BROWSER_VIDEO_SUFFIX}")


def _get_video_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _video_transcode_locks_guard:
        lock = _video_transcode_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _video_transcode_locks[key] = lock
        return lock


def _probe_video_codec(video_path: Path) -> str:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return ""

    proc = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip().lower()


def _is_browser_safe_video(video_path: Path) -> bool:
    codec_name = _probe_video_codec(video_path)
    return codec_name in {"h264", "avc1"}


def _transcode_video_for_browser(source_path: Path, output_path: Path) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg is not installed, so the recorded video cannot be transcoded")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_name(f"{output_path.stem}.tmp{output_path.suffix}")
    proc = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(source_path),
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(temp_path),
        ],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-300:]
        raise RuntimeError(f"ffmpeg failed to prepare a browser-safe video: {tail}")

    temp_path.replace(output_path)
    return output_path


def _ensure_browser_video(video_path: Path) -> Path:
    if _is_browser_safe_video(video_path):
        return video_path

    browser_path = _browser_video_path(video_path)
    if browser_path.exists() and browser_path.stat().st_mtime >= video_path.stat().st_mtime:
        return browser_path

    lock = _get_video_lock(browser_path)
    with lock:
        if browser_path.exists() and browser_path.stat().st_mtime >= video_path.stat().st_mtime:
            return browser_path
        return _transcode_video_for_browser(video_path, browser_path)


def _load_processed_actions(log_path: Path) -> list[dict[str, Any]]:
    processor = LogProcessor()
    return processor.process_log_file(log_path)


def _split_processed_actions(
    actions: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    config_action: dict[str, Any] | None = None
    trajectory: list[dict[str, Any]] = []

    for action in actions:
        if str(action.get("action") or "") == "CONFIG" and config_action is None:
            config_action = copy.deepcopy(action)
            continue
        trajectory.append(copy.deepcopy(action))

    if config_action is None:
        raise ValueError("Processed actions are missing the CONFIG header")
    return config_action, trajectory


def _kind_from_action(action_name: str) -> str:
    lowered = (action_name or "").strip().lower()
    if lowered.startswith("type:"):
        return "type"
    if lowered.startswith("press "):
        return "press"
    if lowered.startswith("hotkey:"):
        return "hotkey"
    if lowered.startswith("dragstart"):
        return "drag"
    if "dblclick" in lowered or "doubleclick" in lowered or "double click" in lowered:
        return "double_click"
    if "rclick" in lowered or "right click" in lowered:
        return "right_click"
    if "scroll" in lowered:
        return "scroll"
    if "click" in lowered:
        return "click"
    return "other"


def _kind_label(kind: str) -> str:
    labels = {
        "click": "Click",
        "double_click": "Double Click",
        "drag": "Drag",
        "hotkey": "Hotkey",
        "press": "Key Press",
        "right_click": "Right Click",
        "scroll": "Scroll",
        "type": "Type",
        "other": "Other",
    }
    return labels.get(kind, "Other")


def _point_from_pair(value: Any) -> list[int] | None:
    if not isinstance(value, list) or len(value) < 2:
        return None
    try:
        return [int(value[0]), int(value[1])]
    except (TypeError, ValueError):
        return None


def _pair_from_coords(value: Any) -> list[int] | None:
    if not isinstance(value, list) or not value:
        return None
    first = value[0]
    if not isinstance(first, dict):
        return None
    try:
        return [int(first.get("x", 0)), int(first.get("y", 0))]
    except (TypeError, ValueError):
        return None


def _pairs_from_path(value: Any) -> list[list[int]] | None:
    if not isinstance(value, list) or not value:
        return None
    result: list[list[int]] = []
    for point in value:
        if not isinstance(point, dict):
            continue
        try:
            result.append([int(point.get("x", 0)), int(point.get("y", 0))])
        except (TypeError, ValueError):
            continue
    return result or None


def _path_dicts_from_pairs(value: list[list[int]] | None) -> list[dict[str, int]] | None:
    if not value:
        return None
    result: list[dict[str, int]] = []
    for point in value:
        pair = _point_from_pair(point)
        if pair is None:
            continue
        result.append({"x": pair[0], "y": pair[1]})
    return result or None


def _ensure_step_path(
    kind: str,
    coordinates: list[int] | None,
    end_coordinates: list[int] | None,
    path: list[list[int]] | None,
) -> list[list[int]] | None:
    normalized = []
    if path:
        for point in path:
            pair = _point_from_pair(point)
            if pair is not None:
                normalized.append(pair)
    if kind == "drag":
        if coordinates and not normalized:
            normalized.append(coordinates)
        if end_coordinates:
            if not normalized or normalized[-1] != end_coordinates:
                normalized.append(end_coordinates)
    return normalized or None


def _serialize_processed_step(action: dict[str, Any], index: int) -> dict[str, Any]:
    action_name = str(action.get("action", "") or "")
    kind = _kind_from_action(action_name)
    timestamp = float(action.get("timestamp", 0.0) or 0.0)
    coordinates = _pair_from_coords(action.get("coords"))
    path = _pairs_from_path(action.get("path"))
    end_coordinates = path[-1] if path else None
    if kind == "drag" and coordinates and not path:
        path = [coordinates]
    return {
        "id": f"step-{index:04d}",
        "index": index,
        "timestamp": timestamp,
        "keyframe_timestamp": float(action.get("keyframe_timestamp", timestamp) or timestamp),
        "action": action_name,
        "label": action_name,
        "note": "",
        "kind": kind,
        "kind_label": _kind_label(kind),
        "coordinates": coordinates,
        "end_coordinates": end_coordinates,
        "path": path,
        "window": str(action.get("current_software") or ""),
        "scroll_count": int(action.get("scroll_count", 0) or 0),
        "deleted": False,
        "edited": False,
    }


def _normalize_draft_step(step: dict[str, Any], index: int) -> dict[str, Any]:
    action_name = str(step.get("action", "") or "").strip()
    if not action_name:
        raise ValueError(f"Draft step {index} is missing an action")

    kind = _kind_from_action(action_name)
    timestamp = float(step.get("timestamp", 0.0) or 0.0)
    keyframe_timestamp = float(step.get("keyframe_timestamp", timestamp) or timestamp)
    coordinates = _point_from_pair(step.get("coordinates"))
    end_coordinates = _point_from_pair(step.get("end_coordinates"))
    path = _ensure_step_path(kind, coordinates, end_coordinates, step.get("path"))
    if coordinates is None and path:
        coordinates = path[0]
    if end_coordinates is None and path:
        end_coordinates = path[-1]

    return {
        "id": str(step.get("id") or f"step-{index:04d}"),
        "index": index,
        "timestamp": timestamp,
        "keyframe_timestamp": keyframe_timestamp,
        "action": action_name,
        "label": str(step.get("label") or action_name),
        "note": str(step.get("note") or ""),
        "kind": kind,
        "kind_label": _kind_label(kind),
        "coordinates": coordinates,
        "end_coordinates": end_coordinates,
        "path": path,
        "window": str(step.get("window") or ""),
        "scroll_count": int(step.get("scroll_count", 0) or 0),
        "deleted": bool(step.get("deleted", False)),
        "edited": bool(step.get("edited", False)),
    }


def _normalize_draft_trajectory(trajectory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_normalize_draft_step(step, index) for index, step in enumerate(trajectory, 1)]


def _summarize_trajectory(trajectory: list[dict[str, Any]]) -> dict[str, Any]:
    active_steps = [step for step in trajectory if not step.get("deleted")]
    action_counts: dict[str, int] = {}
    window_counts: dict[str, int] = {}
    edited_count = 0

    for step in trajectory:
        if step.get("edited"):
            edited_count += 1
    for step in active_steps:
        kind = step.get("kind") or "other"
        action_counts[kind] = action_counts.get(kind, 0) + 1
        window = (step.get("window") or "").strip()
        if window:
            window_counts[window] = window_counts.get(window, 0) + 1

    action_types = [
        {"key": key, "label": _kind_label(key), "count": count}
        for key, count in sorted(action_counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    windows = [
        {"name": name, "count": count}
        for name, count in sorted(window_counts.items(), key=lambda item: (-item[1], item[0]))
    ]

    return {
        "total_count": len(trajectory),
        "active_count": len(active_steps),
        "deleted_count": len(trajectory) - len(active_steps),
        "edited_count": edited_count,
        "action_types": action_types,
        "windows": windows,
    }


def _draft_to_processed_actions(draft: dict[str, Any]) -> list[dict[str, Any]]:
    config_action = draft.get("config_action")
    if not isinstance(config_action, dict):
        raise ValueError("Draft is missing the CONFIG header needed for import")

    actions = [copy.deepcopy(config_action)]
    for step in _normalize_draft_trajectory(draft.get("trajectory") or []):
        if step.get("deleted"):
            continue

        action = {
            "timestamp": float(step.get("timestamp", 0.0) or 0.0),
            "action": step.get("action") or "",
            "coords": None,
            "current_software": step.get("window") or "",
            "keyframe_timestamp": float(
                step.get("keyframe_timestamp", step.get("timestamp", 0.0)) or 0.0
            ),
        }

        coordinates = step.get("coordinates")
        if coordinates:
            action["coords"] = [{"x": int(coordinates[0]), "y": int(coordinates[1])}]

        if step.get("kind") == "drag":
            path = _path_dicts_from_pairs(step.get("path"))
            if path:
                action["path"] = path

        scroll_count = int(step.get("scroll_count", 0) or 0)
        if scroll_count:
            action["scroll_count"] = scroll_count

        actions.append(action)

    actions.sort(key=lambda item: float(item.get("timestamp", 0.0) or 0.0))
    return actions


def _build_preview_payload(
    manager,
    project_path: Path,
    *,
    status: dict[str, Any] | None = None,
    artifacts: tuple[Path, Path] | None = None,
    processed_actions: list[dict[str, Any]] | None = None,
    draft_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    status = status or manager.get_status()
    video_path, log_path = artifacts or _find_recording_artifacts(project_path)
    processed_actions = processed_actions or _load_processed_actions(log_path)
    config_action, raw_trajectory = _split_processed_actions(processed_actions)
    draft = draft_override if draft_override is not None else _load_draft(project_path)

    if draft:
        trajectory = _normalize_draft_trajectory(draft.get("trajectory") or [])
        source = "draft"
        has_draft = True
        draft_saved_at = float(draft.get("saved_at", 0.0) or 0.0)
    else:
        trajectory = [
            _serialize_processed_step(action, index)
            for index, action in enumerate(raw_trajectory, 1)
        ]
        source = "raw"
        has_draft = False
        draft_saved_at = 0.0

    summary = _summarize_trajectory(trajectory)
    version = int(status.get("version", 0))

    return {
        "project": status.get("project") or project_path.name,
        "video_url": f"/api/v1/recorder/video?version={version}",
        "log_url": f"/api/v1/recorder/log?version={version}",
        "frame_url_base": f"/api/v1/recorder/frame?version={version}&ts_ms=",
        "trajectory": trajectory,
        "trajectory_count": len(trajectory),
        "active_count": summary["active_count"],
        "video_path": str(video_path),
        "log_path": str(log_path),
        "summary": summary,
        "has_draft": has_draft,
        "source": source,
        "draft_saved_at": draft_saved_at,
        "draft_path": str(_draft_path(project_path)) if has_draft else "",
        "config_action": config_action,
    }


def _read_video_frame_bytes(video_path: Path, timestamp_ms: int) -> bytes:
    import cv2

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise FileNotFoundError(f"Unable to open video: {video_path}")

    try:
        cap.set(cv2.CAP_PROP_POS_MSEC, float(max(timestamp_ms, 0)))
        ok, frame = cap.read()
        if not ok or frame is None:
            raise ValueError(f"No frame available at {timestamp_ms} ms")

        encoded_ok, encoded = cv2.imencode(
            ".jpg",
            frame,
            [int(cv2.IMWRITE_JPEG_QUALITY), 92],
        )
        if not encoded_ok:
            raise ValueError("Failed to encode frame as JPEG")
        return encoded.tobytes()
    finally:
        cap.release()


@router.get("/status")
async def get_recorder_status(request: Request):
    """Return the latest recorder state snapshot."""
    return _get_manager(request).get_status()


@router.post("/start")
async def start_recorder(body: StartRecorderRequest, request: Request):
    """Start a new recording session."""
    manager = _get_manager(request)
    try:
        return manager.start(
            body.project,
            body.output_dir,
            body.fps,
            body.monitor,
        )
    except RecorderConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc


@router.post("/stop")
async def stop_recorder(request: Request):
    """Stop the active recording session."""
    manager = _get_manager(request)
    try:
        return manager.stop()
    except RecorderConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc


@router.post("/open-folder")
async def open_recorder_folder(request: Request):
    """Open the current or most recent recorder output folder."""
    manager = _get_manager(request)
    try:
        status = manager.open_folder()
    except RecorderConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc
    return {"status": "opened", "output_dir": status.get("output_dir", "")}


@router.post("/import")
async def import_recorder_output(body: ImportRecorderRequest, request: Request):
    """Import the most recently completed recording as a recorded skill."""
    manager = _get_manager(request)
    try:
        project_path = manager.get_project_path()
    except RecorderConflict as exc:
        raise HTTPException(409, str(exc)) from exc

    draft = _load_draft(project_path)
    processed_actions_override = None
    if draft:
        try:
            processed_actions_override = _draft_to_processed_actions(draft)
        except Exception as exc:
            raise HTTPException(400, f"Invalid recorder draft: {exc}") from exc

    try:
        skill = await import_recorded_workflow(
            store=request.app.state.recorded_skill_store,
            config=getattr(request.app.state, "config", None),
            project_path=str(project_path),
            skill_name=body.name,
            description=body.description or body.name,
            auto_trace=body.auto_trace,
            processed_actions_override=processed_actions_override,
        )
    except Exception as exc:
        raise HTTPException(500, f"Import failed: {exc}") from exc

    return {
        "name": body.name,
        "status": "imported",
        "steps": len(skill.steps),
        "trace_generated": skill.meta.trace_generated,
        "draft_used": bool(processed_actions_override),
    }


@router.get("/preview")
async def get_recorder_preview(request: Request):
    """Return preview metadata and the effective edited trajectory."""
    manager = _get_manager(request)
    try:
        project_path = _latest_project_path(manager)
    except RecorderConflict as exc:
        raise HTTPException(409, str(exc)) from exc

    try:
        return _build_preview_payload(manager, project_path)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, f"Failed to build preview: {exc}") from exc


@router.get("/draft")
async def get_recorder_draft(request: Request):
    """Return the saved capture draft, if one exists."""
    manager = _get_manager(request)
    try:
        project_path = _latest_project_path(manager)
    except RecorderConflict as exc:
        raise HTTPException(409, str(exc)) from exc

    draft = _load_draft(project_path)
    if not draft:
        return {"exists": False, "project": project_path.name}

    return {
        "exists": True,
        "project": draft.get("project") or project_path.name,
        "saved_at": float(draft.get("saved_at", 0.0) or 0.0),
        "trajectory": _normalize_draft_trajectory(draft.get("trajectory") or []),
    }


@router.put("/draft")
async def update_recorder_draft(body: RecorderDraftRequest, request: Request):
    """Persist the edited capture draft without touching raw assets."""
    manager = _get_manager(request)
    try:
        project_path = _latest_project_path(manager)
    except RecorderConflict as exc:
        raise HTTPException(409, str(exc)) from exc

    try:
        artifacts = _find_recording_artifacts(project_path)
        processed_actions = _load_processed_actions(artifacts[1])
        config_action, _ = _split_processed_actions(processed_actions)
        normalized = _normalize_draft_trajectory(
            [step.model_dump() for step in body.trajectory]
        )
        draft = {
            "schema_version": 1,
            "project": manager.get_status().get("project") or project_path.name,
            "saved_at": time.time(),
            "config_action": config_action,
            "trajectory": normalized,
        }
        _write_draft(project_path, draft)
        return _build_preview_payload(
            manager,
            project_path,
            artifacts=artifacts,
            processed_actions=processed_actions,
            draft_override=draft,
        )
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, f"Failed to save recorder draft: {exc}") from exc


@router.post("/draft/reset")
async def reset_recorder_draft(request: Request):
    """Discard the saved capture draft and fall back to the raw trajectory."""
    manager = _get_manager(request)
    try:
        project_path = _latest_project_path(manager)
    except RecorderConflict as exc:
        raise HTTPException(409, str(exc)) from exc

    try:
        _delete_draft(project_path)
        return _build_preview_payload(manager, project_path, draft_override=None)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, f"Failed to reset recorder draft: {exc}") from exc


@router.get("/video")
async def get_recorder_video(request: Request):
    """Serve the latest recorded video file in a browser-safe format."""
    manager = _get_manager(request)
    try:
        project_path = _latest_project_path(manager)
        video_path, _ = _find_recording_artifacts(project_path)
        safe_video_path = _ensure_browser_video(video_path)
    except RecorderConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc

    return FileResponse(safe_video_path, media_type="video/mp4", filename=safe_video_path.name)


@router.get("/frame")
async def get_recorder_frame(request: Request, ts_ms: int = 0, version: int | None = None):
    """Serve a single frame from the latest recorded video for keyframe preview."""
    del version
    manager = _get_manager(request)
    try:
        project_path = _latest_project_path(manager)
        video_path, _ = _find_recording_artifacts(project_path)
        frame_bytes = _read_video_frame_bytes(video_path, ts_ms)
    except RecorderConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    return Response(content=frame_bytes, media_type="image/jpeg")


@router.get("/log")
async def get_recorder_log(request: Request):
    """Serve the latest raw recorder log for inspection."""
    manager = _get_manager(request)
    try:
        project_path = _latest_project_path(manager)
        _, log_path = _find_recording_artifacts(project_path)
    except RecorderConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc

    return PlainTextResponse(log_path.read_text(encoding="utf-8"))


@router.get("/events")
async def stream_recorder_events(request: Request):
    """Stream recorder state changes and live ticks to the frontend."""
    manager = _get_manager(request)

    async def event_stream():
        initial = manager.get_status()
        last_version = initial["version"]
        last_tick = int(initial.get("duration_s", 0)) if initial["status"] == "recording" else -1
        yield _sse_message("status", initial)

        while True:
            if await request.is_disconnected():
                break

            status = manager.get_status()
            if status["version"] != last_version:
                event = "status"
                if status["status"] == "stopped":
                    event = "stopped"
                elif status["status"] == "error":
                    event = "failed"
                yield _sse_message(event, status)
                last_version = status["version"]
                if status["status"] != "recording":
                    last_tick = -1

            if status["status"] == "recording":
                tick = int(status.get("duration_s", 0))
                if tick != last_tick:
                    yield _sse_message("tick", status)
                    last_tick = tick

            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
