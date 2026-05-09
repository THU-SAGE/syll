"""GUI Skill data model and storage layer.

Stores demonstration-based GUI skills that can be replayed via ICL injection
into UI-TARS conversations.

Disk layout:
    ~/.syll/workspace/skills/{name}/
    ├── SKILL.md          # Standard skill format, metadata.type="gui-skill"
    ├── steps.json        # GUIStep array
    └── keyframes/
        ├── step_01.webp
        └── ...
"""

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger


@dataclass
class GUIAction:
    """A single GUI action (click, type, scroll, etc.)."""

    type: str  # click | type | scroll | hotkey | wait | double_click | right_click | drag
    coordinates: list[int] | None = None  # [x, y]
    end_coordinates: list[int] | None = None  # for drag
    content: str = ""  # typed text / hotkey combo / scroll direction
    description: str = ""  # human-readable description


@dataclass
class GUIStep:
    """One step in a GUI demonstration."""

    index: int
    screenshot_before: str  # filename in keyframes/
    action: GUIAction
    screenshot_after: str | None = None


@dataclass
class GUISkillMeta:
    """Metadata for a GUI skill."""

    name: str
    description: str
    app_context: str = ""  # e.g. "Chrome"
    screen_resolution: str = ""  # e.g. "1440x900"
    created_at: str = ""
    updated_at: str = ""


@dataclass
class GUISkill:
    """A complete GUI demonstration skill."""

    meta: GUISkillMeta
    steps: list[GUIStep] = field(default_factory=list)


class GUISkillStore:
    """Persistence layer for GUI skills, stored under workspace/skills/."""

    def __init__(self, workspace: Path):
        self.skills_dir = workspace / "skills"
        self.skills_dir.mkdir(parents=True, exist_ok=True)

    def save_skill(self, skill: GUISkill) -> None:
        """Write SKILL.md + steps.json + ensure keyframes/ dir."""
        skill_dir = self.skills_dir / skill.meta.name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "keyframes").mkdir(exist_ok=True)

        skill.meta.updated_at = datetime.now(timezone.utc).isoformat()
        if not skill.meta.created_at:
            skill.meta.created_at = skill.meta.updated_at

        # Write steps.json
        steps_data = [asdict(s) for s in skill.steps]
        (skill_dir / "steps.json").write_text(
            json.dumps(steps_data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # Write SKILL.md
        n = len(skill.steps)
        step_lines = "\n".join(
            f"{s.index}. {s.action.description or s.action.type}" for s in skill.steps
        )
        md = f"""---
name: {skill.meta.name}
description: {skill.meta.description}
metadata: {{"syll":{{"type":"gui-skill","app_context":"{skill.meta.app_context}","steps":{n}}}}}
---
# {skill.meta.name}

GUI demonstration skill with {n} annotated step(s).

## Steps

{step_lines or "(no steps yet)"}

## Usage

Executed via `gui_action(instruction="...", skill_name="{skill.meta.name}")` with ICL injection.
"""
        (skill_dir / "SKILL.md").write_text(md, encoding="utf-8")
        logger.debug(f"Saved GUI skill '{skill.meta.name}' ({n} steps)")

    def load_skill(self, name: str) -> GUISkill | None:
        """Load a GUI skill from disk."""
        skill_dir = self.skills_dir / name
        steps_path = skill_dir / "steps.json"
        if not steps_path.exists():
            return None

        try:
            raw = json.loads(steps_path.read_text(encoding="utf-8"))
            steps = [
                GUIStep(
                    index=s["index"],
                    screenshot_before=s["screenshot_before"],
                    action=GUIAction(**s["action"]),
                    screenshot_after=s.get("screenshot_after"),
                )
                for s in raw
            ]
        except Exception as e:
            logger.error(f"Failed to load steps for '{name}': {e}")
            return None

        # Parse minimal meta from SKILL.md frontmatter
        meta = GUISkillMeta(name=name, description="")
        md_path = skill_dir / "SKILL.md"
        if md_path.exists():
            text = md_path.read_text(encoding="utf-8")
            for line in text.split("\n"):
                if line.startswith("description:"):
                    meta.description = line.split(":", 1)[1].strip()
                    break

        return GUISkill(meta=meta, steps=steps)

    def list_gui_skills(self) -> list[dict]:
        """List all GUI skills (identified by presence of steps.json)."""
        results = []
        if not self.skills_dir.exists():
            return results
        for d in sorted(self.skills_dir.iterdir()):
            if d.is_dir() and (d / "steps.json").exists():
                skill = self.load_skill(d.name)
                if skill:
                    results.append({
                        "name": skill.meta.name,
                        "description": skill.meta.description,
                        "steps": len(skill.steps),
                        "app_context": skill.meta.app_context,
                    })
        return results

    def delete_skill(self, name: str) -> bool:
        """Delete an entire GUI skill directory."""
        import shutil

        skill_dir = self.skills_dir / name
        if not skill_dir.exists():
            return False
        shutil.rmtree(skill_dir)
        logger.info(f"Deleted GUI skill '{name}'")
        return True

    def save_keyframe(self, name: str, index: int, image_bytes: bytes, ext: str = ".webp") -> str:
        """Save a screenshot to keyframes/. Returns the filename."""
        skill_dir = self.skills_dir / name
        kf_dir = skill_dir / "keyframes"
        kf_dir.mkdir(parents=True, exist_ok=True)
        filename = f"step_{index:02d}{ext}"
        (kf_dir / filename).write_bytes(image_bytes)
        return filename

    def get_keyframe_path(self, name: str, filename: str) -> Path | None:
        """Get absolute path to a keyframe image."""
        p = self.skills_dir / name / "keyframes" / filename
        return p if p.exists() else None

    def add_step(self, name: str, step: GUIStep, image_bytes: bytes | None = None) -> GUISkill | None:
        """Add a step to an existing skill."""
        skill = self.load_skill(name)
        if not skill:
            return None
        if image_bytes:
            filename = self.save_keyframe(name, step.index, image_bytes)
            step.screenshot_before = filename
        skill.steps.append(step)
        self.save_skill(skill)
        return skill

    def delete_step(self, name: str, index: int) -> GUISkill | None:
        """Delete a step and re-index remaining steps."""
        skill = self.load_skill(name)
        if not skill:
            return None
        skill.steps = [s for s in skill.steps if s.index != index]
        for i, s in enumerate(skill.steps):
            s.index = i + 1
        self.save_skill(skill)
        return skill
