"""Trajectory manager: loads and formats trajectory data for planner context.

Adapted from ShowUI-Aloha/Aloha_Act/ui_aloha/act/gui_agent/planner/trajectory_manager.py.
"""

import json
from pathlib import Path

from loguru import logger


class TrajectoryManager:
    """Manages trajectory data for task guidance."""

    def get_full_trace(self, trajectory: list[dict] | dict | None) -> dict | None:
        """Load trace data from a dict, list, or file path.

        Args:
            trajectory: Can be:
                - A dict with "trajectory" key
                - A list of step dicts
                - None

        Returns:
            Dict with "trajectory" key, or None.
        """
        if trajectory is None:
            return None

        if isinstance(trajectory, dict):
            return trajectory

        if isinstance(trajectory, list):
            return {"trajectory": trajectory}

        return None

    def get_trajectory_in_context(
        self, trajectory: list[dict] | dict | None, formatting_string: bool = True
    ) -> str | list[str] | None:
        """Format trajectory as in-context guidance for the planner.

        Args:
            trajectory: Trajectory data (dict with "trajectory" key or list of steps).
            formatting_string: If True, return as formatted string; otherwise list.

        Returns:
            Formatted trajectory string/list or None.
        """
        trace_data = self.get_full_trace(trajectory)
        if not trace_data:
            return None

        steps = trace_data.get("trajectory", [])
        context_steps = []

        for action in steps:
            if "milestone" in action:
                continue

            step_idx = action.get('step_idx', 0)
            step_caption = action.get('caption', {})
            step_action = step_caption.get('action', '')
            context_steps.append(f"Step [{step_idx}]: {step_action}")

        if formatting_string:
            return "\n".join(context_steps)
        return context_steps

    def load_trajectory_file(self, path: str | Path) -> dict | None:
        """Load trajectory from a JSON file."""
        path = Path(path)
        if not path.exists():
            logger.warning(f"Trajectory file not found: {path}")
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, Exception) as e:
            logger.warning(f"Failed to load trajectory from {path}: {e}")
            return None
