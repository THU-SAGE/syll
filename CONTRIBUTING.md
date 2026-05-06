# Contributing to Syll

Thanks for wanting to help. Syll is intentionally small (~3,400 lines of
core agent code) and should stay that way.

## Quick setup

```bash
git clone https://github.com/THU-SAGE/syll.git
cd syll
pip install -e ".[dev]"
pytest
ruff check syll/ tests/
```

Python 3.11+.

## Project layout

```
syll/
├── agent/          core agent logic (loop, context, memory, skills, tools)
├── channels/       chat platform integrations
├── bus/            message routing between channels and agent
├── cron/           scheduled jobs + proactive rituals
├── config/         pydantic schema + loader
├── providers/      LLM providers (via LiteLLM)
├── session/        conversation sessions
├── skills/         bundled skills (markdown-based)
├── templates/      workspace bootstrap templates
├── web/            FastAPI + Alpine.js web UI
└── cli/            command-line interface
```

## Writing a tool

Tools extend `Tool(ABC)` and implement `name`, `description`, `parameters`,
and `execute()`. See `syll/agent/tools/` for examples. Register new tools
in `syll/agent/loop.py::_register_default_tools()`.

```python
from syll.agent.tools.base import Tool

class MyTool(Tool):
    name = "my_tool"
    description = "Does a thing"
    parameters = {"type": "object", "properties": {...}, "required": [...]}

    async def execute(self, **kwargs) -> str:
        ...
```

Tools can return `str` or a `ToolResult(text, media)` for multimodal replies.

## Writing a skill

Skills are markdown files with YAML frontmatter under `syll/skills/{name}/SKILL.md`
or `~/.syll/workspace/skills/{name}/SKILL.md`. See
`syll/skills/skill-creator/SKILL.md` for the full guide.

```markdown
---
name: my-skill
description: one-line summary used in the system prompt
metadata:
  syll:
    always: false
---

# My Skill

How to do the thing...
```

## Writing a channel

Channels extend `BaseChannel` in `syll/channels/`. Each channel subscribes
to its own outbound queue and publishes inbound messages to the shared bus.
See `syll/channels/telegram.py` for the simplest example.

## Tests

All PRs must pass `pytest` and `ruff check`. Tests use `pytest-asyncio` in
auto mode — you can write plain `async def test_...` functions.

## Pull requests

- Keep PRs small and focused
- Include tests for new behavior
- Update `CHANGELOG.md` under `## [Unreleased]`
- Match the existing code style (ruff handles most of it)

## Code style

- Type hints on everything
- `async`/`await` for IO
- Line length 100 (enforced by ruff)
- No emoji in code or docstrings

## Questions

Open an issue at [THU-SAGE/syll/issues](https://github.com/THU-SAGE/syll/issues).
