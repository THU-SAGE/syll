"""Proactive ritual installation endpoint.

Rituals are small cron-scheduled prompts that let the ghost reach out to
the user on its own (morning greeting, evening wind-down, etc.). This
module parses `workspace/lore/rituals.md` and installs each ritual as a
cron job with `metadata.action_type='ritual'`, reusing the existing cron
delivery validation so failures are visible to the user.

Install is an explicit user action (POST /rituals/install). There is no
startup auto-install — deletes made in the Schedule tab stay deleted.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request
from loguru import logger
from pydantic import BaseModel

from syll.config.loader import load_config
from syll.cron.types import CronSchedule
from syll.web.routes.cron import validate_deliver_settings

router = APIRouter(tags=["rituals"])


class InstallFailure(BaseModel):
    name: str
    reason: str


class InstallResult(BaseModel):
    installed: list[str]
    skipped: list[str]
    failed: list[InstallFailure]


def _parse_rituals_md(text: str) -> list[dict]:
    """Parse the rituals.md markdown into a list of {name, schedule, prompt}
    dicts. Uses a small state machine over lines — no YAML dependency.

    Recognized format for each ritual:
        ## ritual: <name>
        - **schedule**: `<cron-expr>`
        - **prompt**: |
            <multi-line prompt text>
            <continues until next ## ritual: or EOF>

    Malformed rituals are skipped with a warning log line.
    """
    rituals: list[dict] = []
    current: dict | None = None
    collecting_prompt = False
    prompt_lines: list[str] = []

    header_re = re.compile(r"^##\s+ritual:\s*(\S.*?)\s*$")
    schedule_re = re.compile(r"^-\s*\*\*schedule\*\*:\s*`([^`]+)`\s*$")
    prompt_start_re = re.compile(r"^-\s*\*\*prompt\*\*:\s*\|\s*$")

    def _flush() -> None:
        nonlocal current, collecting_prompt, prompt_lines
        if current and current.get("name") and current.get("schedule"):
            # Strip leading common indentation from the prompt block
            # (supports the markdown "|" folded style with any indent).
            prompt_text = "\n".join(prompt_lines).rstrip()
            if prompt_text:
                # Remove common leading whitespace (dedent)
                lines = prompt_text.split("\n")
                indents = [
                    len(line) - len(line.lstrip())
                    for line in lines
                    if line.strip()
                ]
                if indents:
                    common = min(indents)
                    prompt_text = "\n".join(
                        line[common:] if len(line) >= common else line
                        for line in lines
                    )
                current["prompt"] = prompt_text.strip()
                rituals.append(current)
            else:
                logger.warning(f"Ritual '{current.get('name')}' has empty prompt, skipping")
        current = None
        collecting_prompt = False
        prompt_lines = []

    for line in text.splitlines():
        m = header_re.match(line)
        if m:
            _flush()
            current = {"name": m.group(1).strip()}
            continue

        if current is None:
            continue

        if collecting_prompt:
            # Stop collecting if we hit a new ritual header or another top-level bullet
            if line.startswith("## ") or line.startswith("---"):
                _flush()
                # Re-process the line that ended the prompt block
                m2 = header_re.match(line)
                if m2:
                    current = {"name": m2.group(1).strip()}
                continue
            prompt_lines.append(line)
            continue

        m_sched = schedule_re.match(line)
        if m_sched:
            current["schedule"] = m_sched.group(1).strip()
            continue

        if prompt_start_re.match(line):
            collecting_prompt = True
            prompt_lines = []
            continue

    _flush()
    return rituals


@router.post("/rituals/install", response_model=InstallResult)
async def install_rituals(request: Request) -> InstallResult:
    """Install default rituals from workspace/lore/rituals.md.

    Preconditions:
    - identity.primary_chat_id must be set (raises 400)
    - identity.primary_channel must be an enabled channel (raises 400)
    - workspace/lore/rituals.md must exist (raises 404)
    """
    config = load_config()
    if not config.identity.primary_chat_id:
        raise HTTPException(
            400,
            "primary_chat_id must be set in the Profile/Pet tab before installing rituals",
        )

    channel_manager = getattr(request.app.state, "channel_manager", None)
    # Raises HTTPException on invalid settings (shared validator)
    validate_deliver_settings(
        channel_manager,
        config.identity.primary_channel,
        config.identity.primary_chat_id,
    )

    cron = getattr(request.app.state, "cron_service", None)
    if cron is None:
        raise HTTPException(503, "Cron service not available")

    rituals_path = config.workspace_path / "lore" / "rituals.md"
    if not rituals_path.exists():
        raise HTTPException(
            404,
            f"lore/rituals.md not found at {rituals_path} — onboarding may not have run",
        )

    try:
        rituals = _parse_rituals_md(rituals_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(500, f"Failed to parse rituals.md: {e}") from e

    if not rituals:
        raise HTTPException(
            500,
            "rituals.md parsed but yielded zero rituals — check the format",
        )

    existing_names = {j.name for j in cron.list_jobs(include_disabled=True)}

    installed: list[str] = []
    skipped: list[str] = []
    failed: list[InstallFailure] = []

    for r in rituals:
        job_name = f"ritual:{r['name']}"
        if job_name in existing_names:
            skipped.append(job_name)
            continue
        try:
            cron.add_job(
                name=job_name,
                schedule=CronSchedule(kind="cron", expr=r["schedule"]),
                message=r["prompt"],  # raw; substituted at fire time in on_cron_job
                deliver=True,
                channel=config.identity.primary_channel,
                to=config.identity.primary_chat_id,
                metadata={"action_type": "ritual", "ritual_name": r["name"]},
            )
            installed.append(job_name)
            logger.info(f"Installed ritual: {job_name}")
        except Exception as e:
            failed.append(InstallFailure(name=job_name, reason=str(e)))
            logger.error(f"Failed to install ritual {job_name}: {e}")

    return InstallResult(installed=installed, skipped=skipped, failed=failed)


@router.get("/rituals", response_model=list[dict])
async def list_rituals(request: Request) -> list[dict]:
    """List all installed rituals (cron jobs whose name starts with 'ritual:')."""
    cron = getattr(request.app.state, "cron_service", None)
    if cron is None:
        raise HTTPException(503, "Cron service not available")

    all_jobs = cron.list_jobs(include_disabled=True)
    rituals = []
    for j in all_jobs:
        if not j.name.startswith("ritual:"):
            continue
        rituals.append({
            "id": j.id,
            "name": j.name,
            "enabled": j.enabled,
            "schedule_expr": j.schedule.expr,
            "ritual_name": (j.payload.metadata or {}).get("ritual_name"),
            "last_status": j.state.last_status,
            "last_run_at_ms": j.state.last_run_at_ms,
            "next_run_at_ms": j.state.next_run_at_ms,
        })
    return rituals
