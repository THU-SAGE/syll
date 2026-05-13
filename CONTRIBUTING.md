# Contributing to Syll

Thanks for wanting to help. Syll is intentionally small and should stay easy to
review. The best pull requests are focused, testable, and clear about which
extension point they touch.

## Quick setup

```bash
git clone https://github.com/THU-SAGE/syll.git
cd syll
python -m pip install -e ".[dev]"
ruff check syll/ tests/
pytest tests/ -v
```

Python 3.11+ is required.

## Choose your track

| Track | Typical files touched | Tests | ADR? | Review |
|---|---|---:|---:|---:|
| Skill | `syll/skills/{name}/SKILL.md` | Not required unless behavior is wired into code | No | S |
| Tool | tool file + `AgentLoop._register_default_tools()` + tests | Yes | No | M |
| Channel | channel file + channel manager + local config schema + tests | Yes | No | M |
| Web route | route file + one `include_router` line + route tests | Yes | No | M |
| Core contract | agent, bus, session, or existing config semantics | Yes | Usually | L |

ADRs are only needed for changes that alter core behavior contracts: public
semantics in `syll/agent/`, `syll/bus/`, or `syll/session/`; existing config
field meanings, defaults, migrations, or security boundaries; or new top-level
packages. Ordinary Skill, Tool, Channel, and Web route additions do not need an
ADR, even if a Channel adds a small local config schema.

## Commit style

Use Conventional-Commit-like subjects when practical:

```text
type(scope): short imperative subject
```

Recommended types: `feat`, `fix`, `docs`, `refactor`, `test`, `build`, `ci`,
`chore`.

Recommended scopes: `agent`, `bus`, `channels`, `cli`, `config`, `cron`, `mcp`,
`providers`, `session`, `skills`, `templates`, `tests`, `tools`, `web`, `docs`,
`build`, `ci`.

This is encouraged, not enforced by CI.

## Project layout

```text
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

## Writing a skill

Skills are markdown files with YAML frontmatter under
`syll/skills/{name}/SKILL.md` or
`~/.syll/workspace/skills/{name}/SKILL.md`.

```markdown
---
name: my-skill
description: One-line summary used in the system prompt.
metadata: {"syll":{"always":false}}
---

# My Skill

How to do the thing...
```

The `metadata` field is a single-line JSON string. The current frontmatter
loader reads `key: value` lines and then parses the `metadata` value with
`json.loads`, so nested YAML under `metadata:` will not be interpreted as Syll
metadata.

See `syll/skills/skill-creator/SKILL.md` for the full guide.

## Writing a tool

Tools extend `Tool(ABC)` and implement `name`, `description`, `parameters`, and
`execute()`. See `syll/agent/tools/` for examples. Register new built-in tools
in `AgentLoop._register_default_tools()`.

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

## Writing a channel

Channels extend `BaseChannel` in `syll/channels/`. Each channel subscribes to
its own outbound queue and publishes inbound messages to the shared bus. See
`syll/channels/telegram.py` for the simplest example.

New channels usually need:

- a channel implementation file
- registration in the channel manager
- a local config schema entry
- tests for inbound/outbound routing and access control

## Writing a web route

Web routes live under `syll/web/routes/` and are included from `syll/web/app.py`.
Mutating `/api/v1/*` endpoints are protected by the global admin guard; keep
route-level dependencies when a route needs an explicit security signal.

Add route tests for new API behavior, especially auth, validation, and error
responses.

## Tests

All non-doc PRs should run:

```bash
ruff check syll/ tests/
pytest tests/ -v
```

Tests use `pytest-asyncio` in auto mode, so plain `async def test_...`
functions are fine.

## Pull requests

- Keep PRs small and focused.
- Do not mix behavior changes with broad refactors unless the refactor is tiny
  and directly required.
- Include tests for new behavior.
- Update `CHANGELOG.md` under `## [Unreleased]` for runtime changes.
- Add or link an ADR only for core contract changes.
- Describe manual verification for UI, channel, MCP, GUI, or desktop behavior.

## Code style

- Type hints on everything practical.
- Use `async`/`await` for IO.
- Line length 100 (enforced by ruff for Python).
- No emoji in code or docstrings.

## Releasing (maintainers only)

1. Ensure `CHANGELOG.md` has entries under `[Unreleased]`.
2. Run `scripts/release.sh 0.2.1` or `scripts/release.sh 0.2.1rc1`.
3. Review the diff, approve the prompt, then run `git push && git push --tags`.
4. GitHub Actions builds, tests, and publishes to PyPI or TestPyPI.

## Questions

Open an issue at [THU-SAGE/syll/issues](https://github.com/THU-SAGE/syll/issues)
or start a discussion if the repository has Discussions enabled.
