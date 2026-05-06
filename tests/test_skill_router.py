from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import syll.agent.recorded_skill as recorded_skill_module
from syll.web.skill_router import inject_skill_hint, match_recorded_skill


class _DummyTools:
    def has(self, name: str) -> bool:
        return name == "gui_action_planned"


class _DummyStore:
    def __init__(self, workspace: Path):
        self.workspace = workspace

    def list_skills(self) -> list[dict]:
        return [
            {
                "name": "internal-approval",
                "description": "Run the internal approval workflow",
                "aliases": ["internal approval", "审批流程"],
            }
        ]


def test_match_recorded_skill_normalizes_punctuation(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(recorded_skill_module, "RecordedSkillStore", _DummyStore)
    agent_loop = SimpleNamespace(workspace=tmp_path, tools=_DummyTools())

    match = match_recorded_skill(agent_loop, 'Please run "internal_approval" for me')
    assert match is not None
    assert match["name"] == "internal-approval"


def test_inject_skill_hint_localizes_to_chinese(monkeypatch):
    monkeypatch.setattr(
        "syll.web.skill_router.match_recorded_skill",
        lambda agent_loop, content: {
            "name": "internal-approval",
            "description": "内部审批流程",
        },
    )

    hinted = inject_skill_hint(SimpleNamespace(), "帮我跑一下内部审批")
    assert "系统提示" in hinted
    assert "请使用 gui_action_planned" in hinted
    assert "内部审批流程" in hinted
