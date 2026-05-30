# Acknowledgments

Syll has three main lineages. This file makes the public-facing credits explicit.

## nanobot

Syll began as a fork of the third-party `HKUDS/nanobot` agent framework (MIT).
The channel → bus → agent-loop shape, markdown skill loading, and the
editable-workspace bootstrap originate there; they were extended and reworked
as the project grew into Syll.

## ShowUI-Aloha

The `syll/agent/aloha/` planner / actor / learn stack is based on
`ShowUI-Aloha`, with direct file-level ancestry called out in source comments
for the derived modules, including:

- `syll/agent/aloha/act/planner.py`
- `syll/agent/aloha/act/executor.py`
- `syll/agent/aloha/act/trajectory_manager.py`
- `syll/agent/aloha/act/claude_cua_agent.py`
- `syll/agent/aloha/learn/log_processor.py`
- `syll/agent/aloha/learn/screenshot_processor.py`
- `syll/agent/aloha/learn/trace_generator.py`

## UI-TARS

The GUI automation path references `UI-TARS` for screenshot → action loops,
prompting shape, and desktop-control patterns, especially in:

- `syll/agent/tools/ui_tars.py`
- `syll/skills/gui-agent/SKILL.md`
- `syll/agent/tools/aloha_planner_tool.py`
- `docs/references/resources.md`

We keep direct `Adapted from ...` notes in source files where code was ported
or tightly derived, so future contributors can trace lineage precisely.
