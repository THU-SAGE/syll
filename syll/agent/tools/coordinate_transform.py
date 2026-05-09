"""Unified coordinate transform service for GUI automation.

Handles the 4-step pipeline:
  Model → Screenshot → Desktop → Executor

Different actor types (UI-TARS, Claude CUA, ShowUI) have different
coordinate space mappings in Step 1.
"""

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from loguru import logger


class ActorType(str, Enum):
    """Coordinate space mapping rules per actor."""

    UI_TARS = "ui_tars"  # model output = screenshot pixel coords (identity)
    CLAUDE_CUA = "claude_cua"  # model output = API space (display_w × display_h)
    SHOWUI = "showui"  # model output = XGA/WXGA/FWXGA scaled coords


@dataclass
class FrameContext:
    """Describes the geometry of one monitor capture."""

    monitor_index: int
    left: int
    top: int
    width: int  # logical size (macOS points)
    height: int
    physical_width: int  # mss physical pixels
    physical_height: int
    screenshot_width: int  # after DPR resize
    screenshot_height: int
    dpr: float = 1.0


@dataclass
class ActorSpace:
    """Per-actor coordinate space description."""

    actor_type: ActorType
    api_width: int = 1024  # Claude CUA: display_width
    api_height: int = 768  # Claude CUA: display_height


@dataclass
class CalibrationProfile:
    """Per-monitor calibration offsets and scaling."""

    monitor_index: int = 0
    offset_dx: float = 0.0
    offset_dy: float = 0.0
    scale_sx: float = 1.0
    scale_sy: float = 1.0
    enabled: bool = True
    updated_at: str = ""


@dataclass
class TransformResult:
    """Full transform pipeline output with intermediate values."""

    model_x: int
    model_y: int
    screenshot_x: int
    screenshot_y: int
    desktop_x: int
    desktop_y: int
    executor_x: int
    executor_y: int
    actor_type: str = ""
    clamped: bool = False


# ShowUI aspect-ratio matching targets
SCALING_TARGETS: dict[str, tuple[int, int]] = {
    "XGA": (1024, 768),  # 4:3
    "WXGA": (1280, 800),  # 16:10
    "FWXGA": (1366, 768),  # ~16:9
}


class CoordinateTransformService:
    """Unified coordinate transform for all GUI actor types."""

    def __init__(self, profiles_dir: Path | str):
        self._profiles_dir = Path(profiles_dir)
        self._profiles_dir.mkdir(parents=True, exist_ok=True)

    # ---- Frame context detection ----

    def get_all_frame_contexts(self) -> list[FrameContext]:
        """Probe all monitors and return FrameContext for each."""
        import mss

        contexts: list[FrameContext] = []
        with mss.mss() as sct:
            # mss monitors: 0=virtual-all, 1=primary, 2=secondary, ...
            for idx in range(len(sct.monitors) - 1):
                contexts.append(self._build_frame_context(sct, idx))
        return contexts

    def get_frame_context(self, monitor_index: int = 0) -> FrameContext:
        """Get FrameContext for a single monitor."""
        import mss

        with mss.mss() as sct:
            if monitor_index + 1 >= len(sct.monitors):
                monitor_index = 0
            return self._build_frame_context(sct, monitor_index)

    def _build_frame_context(self, sct: Any, monitor_index: int) -> FrameContext:
        """Build FrameContext from mss for a given monitor index."""
        mss_idx = monitor_index + 1  # mss: 0=all, 1=primary
        monitor = sct.monitors[mss_idx]

        logical_w = monitor["width"]
        logical_h = monitor["height"]

        # DPR detection: grab a small sample and compare physical vs logical
        dpr = self._detect_dpr(sct, mss_idx, logical_w, logical_h)

        physical_w = round(logical_w * dpr)
        physical_h = round(logical_h * dpr)

        # Screenshot size = logical (after DPR resize)
        screenshot_w = logical_w
        screenshot_h = logical_h

        return FrameContext(
            monitor_index=monitor_index,
            left=monitor["left"],
            top=monitor["top"],
            width=logical_w,
            height=logical_h,
            physical_width=physical_w,
            physical_height=physical_h,
            screenshot_width=screenshot_w,
            screenshot_height=screenshot_h,
            dpr=dpr,
        )

    @staticmethod
    def _detect_dpr(sct: Any, mss_idx: int, logical_w: int, logical_h: int) -> float:
        """Detect DPR by sampling a small region and comparing sizes."""
        try:
            monitor = sct.monitors[mss_idx]
            sample = {
                "left": monitor["left"],
                "top": monitor["top"],
                "width": min(100, logical_w),
                "height": min(100, logical_h),
            }
            shot = sct.grab(sample)
            dpr = shot.width / sample["width"]
            return max(1.0, round(dpr, 2))
        except Exception:
            return 1.0

    # ---- Transform pipeline ----

    def model_to_screenshot(
        self, x: int, y: int, ctx: FrameContext, actor: ActorSpace
    ) -> tuple[int, int]:
        """Step 1: Model coords → Screenshot coords (actor-specific)."""
        if actor.actor_type == ActorType.UI_TARS:
            if actor.api_width > 0 and actor.api_height > 0:
                # Model saw a WXGA-scaled image; reverse-scale to logical size
                return self._cua_model_to_screenshot(x, y, ctx, actor)
            return x, y  # identity when screenshot was not scaled
        elif actor.actor_type == ActorType.CLAUDE_CUA:
            return self._cua_model_to_screenshot(x, y, ctx, actor)
        elif actor.actor_type == ActorType.SHOWUI:
            return self._showui_model_to_screenshot(x, y, ctx)
        return x, y

    def model_to_executor(
        self,
        x: int,
        y: int,
        ctx: FrameContext,
        actor: ActorSpace,
        profile: CalibrationProfile | None = None,
    ) -> TransformResult:
        """Full 4-step pipeline: model → screenshot → desktop → executor."""
        # Step 1: Model → Screenshot
        sx, sy = self.model_to_screenshot(x, y, ctx, actor)

        # Step 2: Screenshot → Desktop (+monitor offset)
        dx = sx + ctx.left
        dy = sy + ctx.top

        # Step 3: Desktop → Executor (calibration)
        if profile and profile.enabled:
            ex = round(dx * profile.scale_sx + profile.offset_dx)
            ey = round(dy * profile.scale_sy + profile.offset_dy)
        else:
            ex, ey = dx, dy

        # Step 4: Clamp to monitor bounds
        clamped = False
        min_x, max_x = ctx.left, ctx.left + ctx.width - 1
        min_y, max_y = ctx.top, ctx.top + ctx.height - 1
        if ex < min_x or ex > max_x or ey < min_y or ey > max_y:
            clamped = True
        ex = max(min_x, min(ex, max_x))
        ey = max(min_y, min(ey, max_y))

        return TransformResult(
            model_x=x,
            model_y=y,
            screenshot_x=sx,
            screenshot_y=sy,
            desktop_x=dx,
            desktop_y=dy,
            executor_x=ex,
            executor_y=ey,
            actor_type=actor.actor_type.value,
            clamped=clamped,
        )

    @staticmethod
    def _cua_model_to_screenshot(
        x: int, y: int, ctx: FrameContext, actor: ActorSpace
    ) -> tuple[int, int]:
        """Claude CUA: scale from API space to screenshot space."""
        if actor.api_width <= 0 or actor.api_height <= 0:
            return x, y
        sx = round(x / actor.api_width * ctx.screenshot_width)
        sy = round(y / actor.api_height * ctx.screenshot_height)
        return sx, sy

    @staticmethod
    def _showui_model_to_screenshot(
        x: int, y: int, ctx: FrameContext
    ) -> tuple[int, int]:
        """ShowUI: reverse the aspect-ratio matching scale."""
        ratio = ctx.screenshot_width / ctx.screenshot_height
        target = None
        for tw, th in SCALING_TARGETS.values():
            if abs(tw / th - ratio) < 0.02 and tw < ctx.screenshot_width:
                target = (tw, th)
                break
        if target is None:
            target = (1280, 800)  # fallback WXGA
        sx = target[0] / ctx.screenshot_width
        sy = target[1] / ctx.screenshot_height
        return round(x / sx), round(y / sy)

    # ---- Profile persistence ----

    def _profile_path(self, monitor_index: int) -> Path:
        return self._profiles_dir / f"monitor_{monitor_index}.json"

    def load_profile(self, monitor_index: int = 0) -> CalibrationProfile:
        """Load a calibration profile, returning defaults if not found."""
        path = self._profile_path(monitor_index)
        if path.exists():
            try:
                data = json.loads(path.read_text())
                return CalibrationProfile(**data)
            except Exception as e:
                logger.warning(f"Failed to load profile {path}: {e}")
        return CalibrationProfile(monitor_index=monitor_index)

    def save_profile(self, profile: CalibrationProfile) -> None:
        """Persist a calibration profile to disk."""
        profile.updated_at = datetime.now(timezone.utc).isoformat()
        path = self._profile_path(profile.monitor_index)
        path.write_text(json.dumps(asdict(profile), indent=2))
        logger.info(f"Saved calibration profile: {path}")

    def list_profiles(self) -> list[CalibrationProfile]:
        """List all saved calibration profiles."""
        profiles: list[CalibrationProfile] = []
        for path in sorted(self._profiles_dir.glob("monitor_*.json")):
            try:
                data = json.loads(path.read_text())
                profiles.append(CalibrationProfile(**data))
            except Exception:
                continue
        return profiles
