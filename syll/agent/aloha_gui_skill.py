"""Aloha GUI Skill data model and storage layer.

Parallel to gui_skill.py — stores ShowUI-Aloha demonstration skills with
trace information for ICL injection and Planner+Actor execution.

Disk layout:
    ~/.syll/workspace/aloha_skills/{name}/
    ├── skill.json         # AlohaSkill serialized
    ├── trajectory.json    # ShowUI-Aloha trace
    └── keyframes/
        ├── step_01_full.jpg
        ├── step_01_crop.jpg
        └── ...
"""

import json
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger


@dataclass
class AlohaTrace:
    """ShowUI-Aloha semantic trace for one step."""

    observation: str = ""
    think: str = ""
    action: str = ""
    expectation: str = ""


@dataclass
class AlohaAction:
    """A GUI action (with coordinates)."""

    type: str  # click | type | scroll | hotkey | drag | double_click | right_click | wait | press
    coordinates: list[int] | None = None  # [x, y]
    end_coordinates: list[int] | None = None  # for drag
    content: str = ""  # typed text / hotkey combo / scroll direction
    description: str = ""  # human-readable description


@dataclass
class AlohaStep:
    """One step in an Aloha GUI demonstration."""

    index: int
    screenshot: str  # filename in keyframes/ (full image)
    screenshot_crop: str = ""  # cropped image filename
    action: AlohaAction = field(default_factory=lambda: AlohaAction(type="click"))
    trace: AlohaTrace | None = None


@dataclass
class AlohaSkillMeta:
    """Aloha GUI Skill metadata."""

    name: str
    description: str
    app_context: str = ""
    screen_resolution: str = ""
    source: str = "syll-recorder"  # "syll-recorder" | "manual"
    trace_generated: bool = False
    actor_mode: str = "ui-tars"  # "ui-tars" | "claude-cua"
    aliases: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


@dataclass
class AlohaSkill:
    """Complete Aloha GUI demonstration skill."""

    meta: AlohaSkillMeta
    steps: list[AlohaStep] = field(default_factory=list)
    trajectory: list[dict] = field(default_factory=list)  # ShowUI-Aloha raw trace


class AlohaSkillStore:
    """Persistence layer for Aloha GUI skills, stored under workspace/aloha_skills/."""

    def __init__(self, workspace: Path):
        self.skills_dir = workspace / "aloha_skills"
        self.skills_dir.mkdir(parents=True, exist_ok=True)

    def _skill_dir(self, name: str) -> Path:
        return self.skills_dir / name

    def save_skill(self, skill: AlohaSkill) -> None:
        """Save an AlohaSkill to disk."""
        skill_dir = self._skill_dir(skill.meta.name)
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "keyframes").mkdir(exist_ok=True)

        skill.meta.updated_at = datetime.now(timezone.utc).isoformat()
        if not skill.meta.created_at:
            skill.meta.created_at = skill.meta.updated_at

        # Save skill.json
        data = {
            "meta": asdict(skill.meta),
            "steps": [asdict(s) for s in skill.steps],
        }
        (skill_dir / "skill.json").write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # Save trajectory.json if present
        if skill.trajectory:
            (skill_dir / "trajectory.json").write_text(
                json.dumps({"trajectory": skill.trajectory}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

        logger.debug(f"Saved Aloha skill '{skill.meta.name}' ({len(skill.steps)} steps)")

    def load_skill(self, name: str) -> AlohaSkill | None:
        """Load an AlohaSkill from disk."""
        skill_dir = self._skill_dir(name)
        skill_path = skill_dir / "skill.json"
        if not skill_path.exists():
            return None

        try:
            raw = json.loads(skill_path.read_text(encoding="utf-8"))
            meta_data = raw.get("meta", {})
            meta = AlohaSkillMeta(
                name=meta_data.get("name", name),
                description=meta_data.get("description", ""),
                app_context=meta_data.get("app_context", ""),
                screen_resolution=meta_data.get("screen_resolution", ""),
                source=meta_data.get("source", "syll-recorder"),
                trace_generated=meta_data.get("trace_generated", False),
                actor_mode=meta_data.get("actor_mode", "ui-tars"),
                aliases=meta_data.get("aliases", []),
                created_at=meta_data.get("created_at", ""),
                updated_at=meta_data.get("updated_at", ""),
            )

            steps = []
            for s in raw.get("steps", []):
                action_data = s.get("action", {})
                trace_data = s.get("trace")
                trace = AlohaTrace(**trace_data) if trace_data else None
                steps.append(AlohaStep(
                    index=s["index"],
                    screenshot=s.get("screenshot", ""),
                    screenshot_crop=s.get("screenshot_crop", ""),
                    action=AlohaAction(**action_data),
                    trace=trace,
                ))

            # Load trajectory
            trajectory = []
            traj_path = skill_dir / "trajectory.json"
            if traj_path.exists():
                traj_data = json.loads(traj_path.read_text(encoding="utf-8"))
                trajectory = traj_data.get("trajectory", [])

            return AlohaSkill(meta=meta, steps=steps, trajectory=trajectory)

        except Exception as e:
            logger.error(f"Failed to load Aloha skill '{name}': {e}")
            return None

    def list_skills(self) -> list[dict]:
        """List all Aloha skills."""
        results = []
        if not self.skills_dir.exists():
            return results
        for d in sorted(self.skills_dir.iterdir()):
            if d.is_dir() and (d / "skill.json").exists():
                skill = self.load_skill(d.name)
                if skill:
                    results.append({
                        "name": skill.meta.name,
                        "description": skill.meta.description,
                        "steps": len(skill.steps),
                        "source": skill.meta.source,
                        "trace_generated": skill.meta.trace_generated,
                        "actor_mode": skill.meta.actor_mode,
                        "aliases": skill.meta.aliases,
                        "app_context": skill.meta.app_context,
                        "created_at": skill.meta.created_at,
                    })
        return results

    def delete_skill(self, name: str) -> bool:
        """Delete an entire Aloha skill directory."""
        skill_dir = self._skill_dir(name)
        if not skill_dir.exists():
            return False
        shutil.rmtree(skill_dir)
        logger.info(f"Deleted Aloha skill '{name}'")
        return True

    def save_keyframe(
        self, name: str, index: int, image_bytes: bytes, suffix: str = "_full.jpg"
    ) -> str:
        """Save a screenshot to keyframes/. Returns the filename."""
        skill_dir = self._skill_dir(name)
        kf_dir = skill_dir / "keyframes"
        kf_dir.mkdir(parents=True, exist_ok=True)
        filename = f"step_{index:02d}{suffix}"
        (kf_dir / filename).write_bytes(image_bytes)
        return filename

    def get_keyframe_path(self, name: str, filename: str) -> Path | None:
        """Get absolute path to a keyframe image."""
        p = self._skill_dir(name) / "keyframes" / filename
        return p if p.exists() else None
