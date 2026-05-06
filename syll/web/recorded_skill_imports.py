"""Shared helpers for importing and tracing recorded workflow projects."""

from __future__ import annotations

from typing import Any

from syll.agent.aloha_import import AlohaImporter


def build_recorded_workflow_importer(store, config: Any | None = None) -> AlohaImporter:
    """Create the workflow importer using the configured trace endpoint."""
    trace_model = "gpt-4o"
    api_key = None
    api_base = None

    if config:
        ep = config.resolve_endpoint("trace")
        trace_model = ep.litellm_model or "gpt-4o"
        api_key = ep.api_key or None
        api_base = ep.api_base

    return AlohaImporter(
        store=store,
        trace_model=trace_model,
        api_key=api_key,
        api_base=api_base,
    )


async def import_recorded_workflow(
    *,
    store,
    config,
    project_path: str,
    skill_name: str,
    description: str = "",
    auto_trace: bool = False,
    processed_actions_override: list[dict] | None = None,
):
    """Import a recorded workflow project into the recorded skill store."""
    importer = build_recorded_workflow_importer(store, config)
    return await importer.import_recording(
        project_path=project_path,
        skill_name=skill_name,
        description=description,
        auto_trace=auto_trace,
        processed_actions_override=processed_actions_override,
    )


async def generate_recorded_trace(*, store, config, skill_name: str):
    """Generate or regenerate semantic traces for an existing recorded skill."""
    importer = build_recorded_workflow_importer(store, config)
    return await importer.generate_trace(skill_name)
