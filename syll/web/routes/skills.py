"""Skills API routes."""

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(tags=["skills"])

# ---------------------------------------------------------------------------
# Skill templates — pre-built starters for common skill types
# ---------------------------------------------------------------------------

SKILL_TEMPLATES: dict[str, dict] = {
    "blank": {
        "label": "Blank Skill",
        "description": "Empty skill — start from scratch",
        "content": (
            "---\n"
            "name: {name}\n"
            "description: \"{description}\"\n"
            "---\n\n"
            "# {title}\n\n"
            "Describe what this skill does and when to use it.\n"
        ),
    },
    "cli-tool": {
        "label": "CLI Tool Integration",
        "description": "Integrate an external CLI tool (e.g. curl, jq, ffmpeg)",
        "content": (
            "---\n"
            "name: {name}\n"
            'description: "{description}"\n'
            'metadata: {{"syll":{{"requires":{{"bins":["TOOL_NAME"]}}}}}}\n'
            "---\n\n"
            "# {title}\n\n"
            "## When to use\n\n"
            "Use this skill when the user asks to …\n\n"
            "## Quick start\n\n"
            "```bash\n"
            "TOOL_NAME --help\n"
            "```\n\n"
            "## Examples\n\n"
            "```bash\n"
            "# Example 1\n"
            "TOOL_NAME arg1 arg2\n"
            "```\n"
        ),
    },
    "api-integration": {
        "label": "API Integration",
        "description": "Call an external HTTP API (REST / webhook)",
        "content": (
            "---\n"
            "name: {name}\n"
            'description: "{description}"\n'
            'metadata: {{"syll":{{"requires":{{"env":["API_KEY"]}}}}}}\n'
            "---\n\n"
            "# {title}\n\n"
            "## When to use\n\n"
            "Trigger this skill when the user asks about …\n\n"
            "## API Reference\n\n"
            "Base URL: `https://api.example.com/v1`\n\n"
            "### Endpoints\n\n"
            "```bash\n"
            'curl -s -H "Authorization: Bearer $API_KEY" \\\n'
            '  "https://api.example.com/v1/resource"\n'
            "```\n\n"
            "## Response Format\n\n"
            "JSON with fields: `id`, `name`, `data`.\n"
        ),
    },
    "workflow": {
        "label": "Multi-step Workflow",
        "description": "Guide the agent through a multi-step procedure",
        "content": (
            "---\n"
            "name: {name}\n"
            'description: "{description}"\n'
            "---\n\n"
            "# {title}\n\n"
            "## When to use\n\n"
            "Use this skill when the user asks to …\n\n"
            "## Steps\n\n"
            "1. **Step 1** — Describe what to do first\n"
            "2. **Step 2** — Describe the next action\n"
            "3. **Step 3** — Final action or verification\n\n"
            "## Notes\n\n"
            "- Important consideration 1\n"
            "- Important consideration 2\n"
        ),
    },
    "always-on": {
        "label": "Always-on Skill",
        "description": "Skill loaded into every conversation (system prompt)",
        "content": (
            "---\n"
            "name: {name}\n"
            'description: "{description}"\n'
            'metadata: {{"syll":{{"always":true}}}}\n'
            "---\n\n"
            "# {title}\n\n"
            "This skill is always active. It provides …\n\n"
            "## Rules\n\n"
            "- Rule 1\n"
            "- Rule 2\n"
        ),
    },
}

_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class CreateSkillRequest(BaseModel):
    name: str
    description: str = ""
    template: str = "blank"


class UpdateSkillRequest(BaseModel):
    content: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _workspace_skill_path(request: Request, name: str) -> Path:
    loader = request.app.state.skills_loader
    return loader.workspace_skills / name / "SKILL.md"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/skills/templates")
async def list_templates():
    """List available skill templates."""
    return [
        {"id": tid, "label": t["label"], "description": t["description"]}
        for tid, t in SKILL_TEMPLATES.items()
    ]


@router.get("/skills")
async def list_skills(request: Request):
    """List all skills with availability info."""
    loader = request.app.state.skills_loader
    all_skills = loader.list_skills(filter_unavailable=False)

    result = []
    for s in all_skills:
        meta = loader.get_skill_metadata(s["name"]) or {}
        available = loader._check_requirements(loader._get_skill_meta(s["name"]))
        result.append({
            "name": s["name"],
            "source": s["source"],
            "description": meta.get("description", s["name"]),
            "available": available,
        })
    return result


@router.get("/skills/{name}")
async def get_skill(name: str, request: Request):
    """Get skill details including full content."""
    loader = request.app.state.skills_loader
    content = loader.load_skill(name)
    if content is None:
        raise HTTPException(status_code=404, detail="Skill not found")

    meta = loader.get_skill_metadata(name) or {}

    # Determine source
    skill_path = _workspace_skill_path(request, name)
    source = "workspace" if skill_path.exists() else "builtin"

    return {
        "name": name,
        "content": content,
        "metadata": meta,
        "source": source,
    }


@router.post("/skills")
async def create_skill(body: CreateSkillRequest, request: Request):
    """Create a new workspace skill from a template."""
    name = body.name.strip().lower()

    if not _NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail="Name must be lowercase alphanumeric with hyphens (e.g. my-skill)",
        )

    # Check not already exists in workspace
    skill_path = _workspace_skill_path(request, name)
    if skill_path.exists():
        raise HTTPException(status_code=409, detail=f"Skill '{name}' already exists")

    template = SKILL_TEMPLATES.get(body.template)
    if not template:
        raise HTTPException(status_code=400, detail=f"Unknown template: {body.template}")

    title = name.replace("-", " ").title()
    content = template["content"].format(
        name=name,
        description=body.description or f"Custom skill: {title}",
        title=title,
    )

    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text(content, encoding="utf-8")

    return {"name": name, "content": content, "source": "workspace"}


@router.put("/skills/{name}")
async def update_skill(name: str, body: UpdateSkillRequest, request: Request):
    """Update skill content (workspace skills only)."""
    skill_path = _workspace_skill_path(request, name)

    if not skill_path.exists():
        # Check if it's a builtin — need to "fork" to workspace
        loader = request.app.state.skills_loader
        existing = loader.load_skill(name)
        if existing is None:
            raise HTTPException(status_code=404, detail="Skill not found")
        # Fork builtin to workspace
        skill_path.parent.mkdir(parents=True, exist_ok=True)

    skill_path.write_text(body.content, encoding="utf-8")
    return {"name": name, "source": "workspace", "content": body.content}


@router.delete("/skills/{name}")
async def delete_skill(name: str, request: Request):
    """Delete a workspace skill. Builtin skills cannot be deleted."""
    skill_path = _workspace_skill_path(request, name)
    if not skill_path.exists():
        raise HTTPException(
            status_code=400,
            detail="Only workspace skills can be deleted (builtin skills are read-only)",
        )

    import shutil
    shutil.rmtree(skill_path.parent)
    return {"deleted": name}
