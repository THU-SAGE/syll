"""Screenshot processor: extracts and processes screenshots from video recordings.

Adapted from ShowUI-Aloha/Aloha_Learn/screenshot_processor.py.
Requires: opencv-python (cv2), numpy.
"""

import json
from pathlib import Path

import cv2
import numpy as np
from loguru import logger


class VideoScreenshotExtractor:
    """Extract full + crop screenshots per action and scale coordinates to a target resolution."""

    def __init__(
        self,
        target_width: int = 1920,
        target_height: int = 1080,
        jpeg_quality: int = 95,
        crop_size: int = 256,
        x_size: int = 30,
        x_thick: int = 6,
    ):
        self.target_width = target_width
        self.target_height = target_height
        self.jpeg_quality = jpeg_quality
        self.crop_size = crop_size
        self.x_size = x_size
        self.x_thick = x_thick

    def scale_path(self, path: list[dict], scale_x: float, scale_y: float) -> list[dict]:
        """Scale a list of {x,y} points for drag path."""
        if not path:
            return path
        return [{"x": p["x"] * scale_x, "y": p["y"] * scale_y} for p in path]

    def _bbox_with_padding(
        self, pts: list[dict], w: int, h: int, pad: int = 50
    ) -> tuple[int, int, int, int]:
        """Compute bbox of points with padding, clamped to frame bounds."""
        xs = [int(p["x"]) for p in pts]
        ys = [int(p["y"]) for p in pts]
        x1 = max(0, min(xs) - pad)
        y1 = max(0, min(ys) - pad)
        x2 = min(w, max(xs) + pad)
        y2 = min(h, max(ys) + pad)
        if x2 <= x1:
            x2 = min(w, x1 + 1)
        if y2 <= y1:
            y2 = min(h, y1 + 1)
        return x1, y1, x2, y2

    def _get_frame_at(self, video_path: str | Path, timestamp_seconds: float):
        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            return None
        try:
            cap.set(cv2.CAP_PROP_POS_MSEC, timestamp_seconds * 1000.0)
            ok, frame = cap.read()
            if not ok or frame is None:
                return None
            frame = cv2.resize(
                frame, (self.target_width, self.target_height), interpolation=cv2.INTER_LANCZOS4
            )
            return frame
        finally:
            cap.release()

    def _save_jpg(self, path: str, img) -> bool:
        import os

        os.makedirs(os.path.dirname(path), exist_ok=True)
        return cv2.imwrite(path, img, [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality])

    def _safe_crop(self, frame, x: int, y: int, crop_size: int = 256):
        if x is None or y is None:
            return frame
        h, w = frame.shape[:2]
        half = crop_size // 2
        x1, y1 = max(0, x - half), max(0, y - half)
        x2, y2 = min(w, x + half), min(h, y + half)
        return frame[y1:y2, x1:x2]

    def _primary_point_from_coords(self, coords) -> tuple[int, int] | None:
        if not coords:
            return None
        if isinstance(coords, list):
            c = coords[0]
        elif isinstance(coords, dict):
            c = next(iter(coords.values()))
        else:
            return None
        return int(c.get("x", 0)), int(c.get("y", 0))

    def _parse_config_resolution(self, actions: list[dict]) -> tuple[int | None, int | None]:
        for a in actions:
            if (a.get("action") or "").startswith("CONFIG"):
                monitors = a.get("coords", {})
                primary = monitors.get("0") if isinstance(monitors, dict) else None
                if primary is None and isinstance(monitors, dict) and monitors:
                    primary = next(iter(monitors.values()))
                if not primary:
                    return None, None
                width = primary.get("width")
                height = primary.get("height")
                sf = primary.get("scale_factor", 1.0) or 1.0
                logical_w = int(round(width / sf))
                logical_h = int(round(height / sf))
                return logical_w, logical_h
        return None, None

    def scale_coordinates(
        self, coords, scale_x: float, scale_y: float
    ):
        if not coords:
            return coords
        if isinstance(coords, list):
            out = []
            for c in coords:
                if isinstance(c, dict) and ("x" in c) and ("y" in c):
                    out.append({"x": c["x"] * scale_x, "y": c["y"] * scale_y})
            return out if out else coords
        return coords

    def _crop_with_black_padding(self, frame, x, y, crop_size: int = 256):
        if x is None or y is None:
            return frame
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

        x1c = max(0, x1)
        y1c = max(0, y1)
        x2c = min(w, x2)
        y2c = min(h, y2)

        crop = frame[y1c:y2c, x1c:x2c]

        if pad_left or pad_top or pad_right or pad_bottom:
            crop = cv2.copyMakeBorder(
                crop, pad_top, pad_bottom, pad_left, pad_right,
                borderType=cv2.BORDER_CONSTANT, value=(0, 0, 0),
            )

        if crop.shape[:2] != (crop_size, crop_size):
            crop = cv2.resize(crop, (crop_size, crop_size), interpolation=cv2.INTER_AREA)

        return crop

    def process_actions(
        self,
        actions: list[dict],
        video_path: str | Path,
        screenshots_path: Path,
        need_scaling: bool,
        scale_x: float,
        scale_y: float,
    ) -> list[dict]:
        updated = []
        for a in actions:
            act_str = (a.get("action") or "").strip()
            if act_str == "CONFIG" or act_str.startswith("Active Window"):
                continue
            ua = a.copy()
            raw_coords = a.get('coords')
            ua['coords'] = (
                self.scale_coordinates(raw_coords, scale_x, scale_y) if need_scaling else raw_coords
            )
            if act_str == "DragStart at" and "path" in a and isinstance(a["path"], list):
                ua["path"] = self.scale_path(a["path"], scale_x, scale_y) if need_scaling else a["path"]
            else:
                ua["path"] = None

            frame_source_timestamp = float(a.get("keyframe_timestamp", a["timestamp"]) or 0.0)
            timestamp = abs(frame_source_timestamp - 0.1)
            base = f"{timestamp:.3f}s"
            full_fn = f"{base}.jpg"
            crop_fn = f"{base}.crop.jpg"

            full_path = screenshots_path / full_fn
            crop_path = screenshots_path / crop_fn

            frame = self._get_frame_at(video_path, timestamp)
            if frame is None:
                ua['screenshot_full'] = None
                ua['screenshot_crop'] = None
                updated.append(ua)
                continue

            H, W = frame.shape[:2]
            pt = self._primary_point_from_coords(ua.get('coords'))
            actt = act_str.lower()
            no_coor = (
                ("scroll" in actt) or ("wheel" in actt) or ("hotkey" in actt)
                or ("type" in actt) or ("press" in actt)
            )

            if act_str == "DragStart at" and ua.get('path') and len(ua['path']) >= 2:
                full_ok = self._save_jpg(str(full_path), frame)
                x1, y1, x2, y2 = self._bbox_with_padding(ua['path'], W, H, pad=25)
                crop_region = frame[y1:y2, x1:x2].copy()

                pts = []
                for p in ua['path']:
                    px = int(round(p["x"])) - x1
                    py = int(round(p["y"])) - y1
                    pts.append([px, py])
                pts_arr = cv2.approxPolyDP(
                    np.array(pts, dtype=np.int32), epsilon=2.0, closed=False
                )
                if pts_arr.ndim == 3:
                    pts_arr = pts_arr.reshape(-1, 2)
                for idx in range(1, len(pts_arr)):
                    cv2.line(
                        crop_region, tuple(pts_arr[idx - 1]), tuple(pts_arr[idx]),
                        (0, 0, 255), max(2, self.x_thick),
                    )
                crop_ok = self._save_jpg(str(crop_path), crop_region)
            elif no_coor:
                full_ok = self._save_jpg(str(full_path), frame)
                crop_ok = self._save_jpg(str(crop_path), frame)
            else:
                draw_frame = frame.copy()
                cx_raw, cy_raw = (pt if pt else (None, None))
                crop_img = self._crop_with_black_padding(draw_frame, cx_raw, cy_raw, crop_size=self.crop_size)

                if pt:
                    cx = cy = self.crop_size // 2
                    outline = self.x_thick + 2
                    max_half = (self.crop_size // 2) - 1 - outline
                    half_x = max(1, min(self.x_size // 2, max_half))

                    overlay = crop_img.copy()
                    cv2.line(overlay, (cx - half_x, cy - half_x), (cx + half_x, cy + half_x), (255, 255, 255), outline)
                    cv2.line(overlay, (cx - half_x, cy + half_x), (cx + half_x, cy - half_x), (255, 255, 255), outline)
                    cv2.line(overlay, (cx - half_x, cy - half_x), (cx + half_x, cy + half_x), (0, 0, 255), self.x_thick)
                    cv2.line(overlay, (cx - half_x, cy + half_x), (cx + half_x, cy - half_x), (0, 0, 255), self.x_thick)
                    crop_img = cv2.addWeighted(overlay, 0.5, crop_img, 0.5, 0)

                full_ok = self._save_jpg(str(full_path), frame)
                crop_ok = self._save_jpg(str(crop_path), crop_img)

            ua['screenshot_full'] = f"screenshots/{full_fn}" if full_ok else None
            ua['screenshot_crop'] = f"screenshots/{crop_fn}" if crop_ok else None
            updated.append(ua)

        return updated

    def process_project(
        self,
        project_path: str | Path,
        processed_log: list[dict] | None = None,
    ) -> tuple[list[dict], Path, dict]:
        """Process a recording project directory.

        Args:
            project_path: Path to the project directory containing inputs/ and processed log.
            processed_log: Pre-processed action list. If None, loads from
                           {project_dir}/{project_name}_processed_log.json.

        Returns:
            Tuple of (updated_actions, screenshots_dir, metadata).
        """
        project_dir = Path(project_path).resolve()
        inputs_dir = project_dir / "inputs"

        if not inputs_dir.exists():
            # Fallback: files may be directly in project root (e.g. ShowUI-Aloha examples)
            inputs_dir = project_dir

        # Locate video
        candidate_videos = sorted(inputs_dir.glob("*.mp4"))
        if not candidate_videos:
            raise FileNotFoundError(f"No .mp4 files found in {inputs_dir}")
        video_path = candidate_videos[0]
        if len(candidate_videos) > 1:
            candidate_videos.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            video_path = candidate_videos[0]
            logger.warning(f"Multiple videos found, using most recent: {video_path.name}")

        # Load processed log
        if processed_log is None:
            log_path = project_dir / f"{project_dir.name}_processed_log.json"
            if not log_path.exists():
                raise FileNotFoundError(f"Processed log not found: {log_path}")
            actions = json.loads(log_path.read_text(encoding="utf-8"))
        else:
            actions = processed_log

        screenshots_dir = project_dir / "screenshots"
        ow, oh = self._parse_config_resolution(actions)
        if ow is None or oh is None:
            raise ValueError("Could not find screen resolution in the CONFIG action")

        frame_w, frame_h = self.target_width, self.target_height
        need_scaling = not (ow == frame_w and oh == frame_h)
        scale_x = frame_w / ow if ow else 1.0
        scale_y = frame_h / oh if oh else 1.0

        updated_actions = self.process_actions(
            actions, video_path, screenshots_dir, need_scaling, scale_x, scale_y
        )

        meta = {
            "video_file": str(video_path),
            "original_resolution": f"{ow}x{oh}" if ow and oh else "unknown",
            "target_resolution": f"{frame_w}x{frame_h}",
        }

        return updated_actions, screenshots_dir, meta
