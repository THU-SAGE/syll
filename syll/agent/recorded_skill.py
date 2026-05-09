"""Neutral aliases for recorded workflow skill models and storage."""

from __future__ import annotations

from syll.agent.aloha_gui_skill import (
    AlohaAction as RecordedAction,
)
from syll.agent.aloha_gui_skill import (
    AlohaSkill as RecordedSkill,
)
from syll.agent.aloha_gui_skill import (
    AlohaSkillMeta as RecordedSkillMeta,
)
from syll.agent.aloha_gui_skill import (
    AlohaSkillStore as RecordedSkillStore,
)
from syll.agent.aloha_gui_skill import (
    AlohaStep as RecordedStep,
)
from syll.agent.aloha_gui_skill import (
    AlohaTrace as RecordedTrace,
)

__all__ = [
    "RecordedAction",
    "RecordedSkill",
    "RecordedSkillMeta",
    "RecordedSkillStore",
    "RecordedStep",
    "RecordedTrace",
]
