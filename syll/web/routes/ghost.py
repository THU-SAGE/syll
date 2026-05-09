"""Syll pet configuration API with legacy `/ghost` compatibility."""

import json
from pathlib import Path

from fastapi import APIRouter, UploadFile
from pydantic import BaseModel

router = APIRouter(prefix="/syll", tags=["syll"])
legacy_router = APIRouter(prefix="/ghost", tags=["ghost"])

_PREFS_PATH = Path.home() / ".syll" / "ghost_prefs.json"
_LEGACY_PREF_PATHS = (
    Path.home() / ".nanobot" / "syll_prefs.json",
    Path.home() / ".nanobot" / "ghost_prefs.json",
)
_SVG_DIR = Path(__file__).parent.parent / "static" / "ghost"

_DEFAULT_CONFIG = {
    "size": "M",
    "always_on_top": True,
    "auto_sleep_seconds": 60,
    "error_reset_seconds": 5,
    "notifications_enabled": True,
    "state_svg_map": {
        "idle": "ghost-idle-follow.svg",
        "working": "ghost-working-thinking.svg",
        "sleeping": "ghost-sleeping.svg",
        "error": "ghost-gui-help.svg",
        # Voice input: the mascot is actively recording the mic. Falls back
        # to idle-follow until a dedicated animation is supplied.
        "listening": "ghost-idle-follow.svg",
    },
}


class SyllConfigUpdate(BaseModel):
    size: str | None = None
    always_on_top: bool | None = None
    auto_sleep_seconds: int | None = None
    error_reset_seconds: int | None = None
    notifications_enabled: bool | None = None
    state_svg_map: dict[str, str] | None = None


# Legacy alias kept for older imports.
GhostConfigUpdate = SyllConfigUpdate


def _read_config() -> dict:
    data = {}
    for path in (_PREFS_PATH, *_LEGACY_PREF_PATHS):
        try:
            data = json.loads(path.read_text())
            break
        except Exception:
            continue

    for key, value in _DEFAULT_CONFIG.items():
        if key not in data:
            data[key] = value
    if "state_svg_map" in data:
        for state, svg in _DEFAULT_CONFIG["state_svg_map"].items():
            data["state_svg_map"].setdefault(state, svg)
    return data


def _write_config(data: dict):
    _PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _PREFS_PATH.write_text(json.dumps(data, indent=2))


@router.get("/config")
@legacy_router.get("/config")
async def get_syll_config():
    """Return mascot configuration."""
    return _read_config()


@router.put("/config")
@legacy_router.put("/config")
async def update_syll_config(body: SyllConfigUpdate):
    """Update mascot configuration."""
    config = _read_config()
    update = body.model_dump(exclude_none=True)
    if "state_svg_map" in update:
        config.setdefault("state_svg_map", {})
        config["state_svg_map"].update(update.pop("state_svg_map"))
    config.update(update)
    _write_config(config)
    return config


@router.get("/svgs")
@legacy_router.get("/svgs")
async def list_syll_svgs():
    """List available mascot SVG files."""
    svgs = []
    if _SVG_DIR.exists():
        svgs = sorted(f.name for f in _SVG_DIR.glob("*.svg"))
    return {"svgs": svgs}


@router.post("/svgs")
@legacy_router.post("/svgs")
async def upload_syll_svg(file: UploadFile):
    """Upload a new SVG to the mascot assets directory."""
    if not file.filename:
        return {"error": "Only .svg files are accepted"}

    normalized_name = file.filename.replace("\\", "/")
    safe_name = Path(normalized_name).name
    if safe_name != normalized_name or safe_name in {"", ".", ".."}:
        return {"error": "Invalid filename"}

    if Path(safe_name).suffix.lower() != ".svg":
        return {"error": "Only .svg files are accepted"}
    _SVG_DIR.mkdir(parents=True, exist_ok=True)
    dest = _SVG_DIR / safe_name
    content = await file.read()
    dest.write_bytes(content)
    return {"filename": safe_name, "size": len(content)}


# Legacy callables kept so older imports keep working.
get_ghost_config = get_syll_config
update_ghost_config = update_syll_config
list_ghost_svgs = list_syll_svgs
upload_ghost_svg = upload_syll_svg
