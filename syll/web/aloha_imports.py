"""Compatibility wrappers for the renamed recorded workflow import helpers."""

from __future__ import annotations

from syll.web.recorded_skill_imports import (
    build_recorded_workflow_importer,
    generate_recorded_trace,
    import_recorded_workflow,
)


def build_aloha_importer(store, config=None):
    return build_recorded_workflow_importer(store, config)


async def import_aloha_recording(**kwargs):
    return await import_recorded_workflow(**kwargs)


async def generate_aloha_trace(**kwargs):
    return await generate_recorded_trace(**kwargs)
