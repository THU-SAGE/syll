"""Preflight and blocker computation for the Adobe agent tools.

Pure, deterministic readiness checks for the Photoshop and Audition GUI
workflows. These functions inspect the host platform, installed Adobe apps,
GUI tool registration, recorded Aloha skills, macOS Accessibility status, and
the selected monitor geometry, then return a single :class:`AdobePreflight`
describing whether a run can proceed.

The module takes plain arguments and returns values; it has no knowledge of the
web layer, request state, or any persistence.
"""

from __future__ import annotations

import platform
import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

PHOTOSHOP_SKILL_NAME = "photoshop-cutout-syll"
AUDIO_SKILL_NAME = "audition-clean-voice"


@dataclass
class AdobePreflight:
    """Deterministic readiness report for an Adobe GUI workflow."""

    ready: bool
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    recommended_mode: str = "none"
    skill_mode_available: bool = False
    zero_shot_mode_available: bool = False
    app_path: str | None = None
    app_name: str | None = None
    accessibility: str = "unknown"
    selected_screen: int = 0
    monitor: dict[str, Any] | None = None


def _detect_apps(*globs: str) -> list[str]:
    apps: list[Path] = []
    root = Path("/Applications")
    if root.exists():
        for pattern in globs:
            apps.extend(root.glob(pattern))
    return sorted({str(p) for p in apps})


def _detect_photoshop() -> list[str]:
    return _detect_apps(
        "Adobe Photoshop*.app",
        "Adobe Photoshop*/Adobe Photoshop*.app",
    )


def _detect_audition() -> list[str]:
    return _detect_apps(
        "Adobe Audition*.app",
        "Adobe Audition*/Adobe Audition*.app",
    )


def _lufs_available() -> bool:
    try:
        import pyloudnorm  # noqa: F401

        return True
    except Exception:
        return False


def _get_monitor_status(
    coord_profiles_dir: Path | str, selected: int
) -> tuple[bool, dict[str, Any] | None, str]:
    try:
        from syll.agent.tools.coordinate_transform import CoordinateTransformService

        service = CoordinateTransformService(coord_profiles_dir)
        contexts = service.get_all_frame_contexts()
        if selected < 0 or selected >= len(contexts):
            return False, None, f"selected_screen={selected} does not exist"
        ctx = contexts[selected]
        return True, asdict(ctx), ""
    except Exception as exc:
        return False, None, f"monitor probe failed: {exc}"


def _skill_resolution_check(skill: Any, monitor: dict[str, Any] | None) -> tuple[bool, str]:
    if not skill or not monitor:
        return True, ""
    raw = getattr(getattr(skill, "meta", None), "screen_resolution", "") or ""
    m = re.match(r"^(\d+)x(\d+)$", raw)
    if not m:
        return True, ""
    sw, sh = int(m.group(1)), int(m.group(2))
    mw, mh = int(monitor.get("width", 0)), int(monitor.get("height", 0))
    if not sw or not sh or not mw or not mh:
        return True, ""
    if abs((sw / sh) - (mw / mh)) > 0.03:
        return False, f"skill resolution {raw} has a different aspect ratio than selected monitor {mw}x{mh}"
    if raw != f"{mw}x{mh}":
        return False, f"skill resolution {raw} differs from selected monitor {mw}x{mh}; use Coord Lab or matching display"
    return True, ""


def _detect_accessibility(supported_platform: bool) -> str:
    if not supported_platform:
        return "not_applicable"
    try:
        from syll.agent.gui_click import detect_mac_accessibility_status

        return detect_mac_accessibility_status()
    except Exception:
        return "unknown"


def photoshop_preflight(
    *,
    workspace_path: Path,
    gui_config: Any,
    tool_registry: Any,
    skill_store: Any,
    coord_profiles_dir: Path | str,
) -> AdobePreflight:
    """Compute readiness for the Photoshop cutout GUI workflow.

    ``workspace_path`` is accepted for interface symmetry; monitor geometry is
    probed from ``coord_profiles_dir``.
    """
    system = platform.system()
    supported_platform = system == "Darwin"
    photoshop_paths = _detect_photoshop() if supported_platform else []
    osascript_found = shutil.which("osascript") is not None
    gui_enabled = bool(getattr(gui_config, "enabled", False))
    selected_screen = int(getattr(gui_config, "selected_screen", 0) or 0)

    has_tool = getattr(tool_registry, "has", lambda _name: False) if tool_registry else (lambda _name: False)
    gui_tool_registered = bool(has_tool("gui_action_planned"))
    gui_zero_shot_registered = bool(has_tool("gui_action"))

    skill = skill_store.load_skill(PHOTOSHOP_SKILL_NAME) if skill_store else None
    aloha_skill_exists = bool(skill)

    macos_accessibility = _detect_accessibility(supported_platform)

    selected_screen_exists, monitor, monitor_warning = _get_monitor_status(coord_profiles_dir, selected_screen)
    skill_resolution_match, resolution_warning = _skill_resolution_check(skill, monitor)
    screen_warning = monitor_warning or resolution_warning

    skill_mode_available = bool(gui_tool_registered and aloha_skill_exists and skill_resolution_match)
    zero_shot_mode_available = bool(gui_zero_shot_registered)
    recommended_mode = "skill" if skill_mode_available else ("zero_shot" if zero_shot_mode_available else "none")

    blockers: list[str] = []
    warnings: list[str] = []
    if not supported_platform:
        blockers.append("This Photoshop demo requires a macOS host")
    if not photoshop_paths:
        blockers.append("Adobe Photoshop was not found in /Applications")
    if not osascript_found:
        blockers.append("osascript is required for the Photoshop bridge")
    if not gui_enabled:
        blockers.append("tools.gui.enabled is false")
    if not gui_tool_registered and not gui_zero_shot_registered:
        blockers.append("GUI tools are not registered")
    if not aloha_skill_exists:
        warnings.append(f"Aloha skill '{PHOTOSHOP_SKILL_NAME}' is missing; zero-shot mode will be used if available")
    if not skill_mode_available and not zero_shot_mode_available:
        blockers.append("No Photoshop GUI run mode is available: record the Aloha skill or enable gui_action")
    if supported_platform and macos_accessibility != "authorized":
        blockers.append("macOS Accessibility permission is not authorized")
    if not selected_screen_exists:
        blockers.append("selected GUI monitor is unavailable")
    if not skill_resolution_match and not zero_shot_mode_available:
        blockers.append(screen_warning)
    elif not skill_resolution_match and screen_warning:
        warnings.append(f"Skill mode disabled: {screen_warning}")
    if screen_warning and skill_resolution_match:
        warnings.append(screen_warning)

    app_path = photoshop_paths[0] if photoshop_paths else None
    app_name = Path(app_path).stem if app_path else "Adobe Photoshop"

    return AdobePreflight(
        ready=not blockers,
        blockers=blockers,
        warnings=warnings,
        recommended_mode=recommended_mode,
        skill_mode_available=skill_mode_available,
        zero_shot_mode_available=zero_shot_mode_available,
        app_path=app_path,
        app_name=app_name,
        accessibility=macos_accessibility,
        selected_screen=selected_screen,
        monitor=monitor,
    )


def audition_preflight(
    *,
    workspace_path: Path,
    gui_config: Any,
    tool_registry: Any,
    skill_store: Any,
    coord_profiles_dir: Path | str,
) -> AdobePreflight:
    """Compute readiness for the Audition voice-cleanup GUI workflow.

    ``workspace_path`` is accepted for interface symmetry; monitor geometry is
    probed from ``coord_profiles_dir``.
    """
    system = platform.system()
    supported_platform = system == "Darwin"
    audition_paths = _detect_audition() if supported_platform else []
    ffmpeg_found = shutil.which("ffmpeg") is not None
    gui_enabled = bool(getattr(gui_config, "enabled", False))
    selected_screen = int(getattr(gui_config, "selected_screen", 0) or 0)

    has_tool = getattr(tool_registry, "has", lambda _name: False) if tool_registry else (lambda _name: False)
    gui_tool_registered = bool(has_tool("gui_action_planned"))
    gui_zero_shot_registered = bool(has_tool("gui_action"))

    skill = skill_store.load_skill(AUDIO_SKILL_NAME) if skill_store else None
    aloha_skill_exists = bool(skill)

    macos_accessibility = _detect_accessibility(supported_platform)

    selected_screen_exists, monitor, monitor_warning = _get_monitor_status(coord_profiles_dir, selected_screen)
    skill_resolution_match, resolution_warning = _skill_resolution_check(skill, monitor)
    screen_warning = monitor_warning or resolution_warning

    skill_mode_available = bool(gui_tool_registered and aloha_skill_exists and skill_resolution_match)
    zero_shot_mode_available = bool(gui_zero_shot_registered)
    recommended_mode = "skill" if skill_mode_available else ("zero_shot" if zero_shot_mode_available else "none")

    blockers: list[str] = []
    warnings: list[str] = []
    if not supported_platform:
        blockers.append("This Audition demo requires a macOS host")
    if not audition_paths:
        blockers.append("Adobe Audition was not found in /Applications")
    if not gui_enabled:
        blockers.append("tools.gui.enabled is false")
    if not gui_tool_registered and not gui_zero_shot_registered:
        blockers.append("GUI tools are not registered")
    if not aloha_skill_exists:
        warnings.append(f"Aloha skill '{AUDIO_SKILL_NAME}' is missing; zero-shot mode will be used if available")
    if not skill_mode_available and not zero_shot_mode_available:
        blockers.append("No audio run mode is available: record the Aloha skill or enable gui_action")
    if supported_platform and macos_accessibility != "authorized":
        blockers.append("macOS Accessibility permission is not authorized")
    if not selected_screen_exists:
        blockers.append("selected GUI monitor is unavailable")
    if not skill_resolution_match and not zero_shot_mode_available:
        blockers.append(screen_warning)
    elif not skill_resolution_match and screen_warning:
        warnings.append(f"Skill mode disabled: {screen_warning}")
    if not ffmpeg_found:
        warnings.append("ffmpeg not found; non-WAV uploads and normalization are limited")
    if screen_warning and skill_resolution_match:
        warnings.append(screen_warning)
    if not _lufs_available():
        warnings.append("pyloudnorm not installed; LUFS will be unavailable")

    app_path = audition_paths[0] if audition_paths else None
    app_name = Path(app_path).stem if app_path else "Adobe Audition"

    return AdobePreflight(
        ready=not blockers,
        blockers=blockers,
        warnings=warnings,
        recommended_mode=recommended_mode,
        skill_mode_available=skill_mode_available,
        zero_shot_mode_available=zero_shot_mode_available,
        app_path=app_path,
        app_name=app_name,
        accessibility=macos_accessibility,
        selected_screen=selected_screen,
        monitor=monitor,
    )
