"""Helpers for running macOS click backend experiments."""

from dataclasses import asdict, dataclass
from types import SimpleNamespace
from typing import Any, Iterable

from syll.agent.gui_click import (
    perform_click_sequence,
    perform_press,
    perform_right_click,
)


@dataclass
class ClickExperimentCase:
    """One click experiment variant."""

    backend: str
    mac_click_style: str = "auto"
    action: str = "click"
    click_count: int = 1
    hold_duration: float = 1.0
    label: str | None = None


async def run_click_experiment_matrix(
    pyautogui: Any,
    x: int,
    y: int,
    *,
    cases: Iterable[ClickExperimentCase],
    intent: str = "",
    preflight_permissions: bool = False,
) -> list[dict[str, Any]]:
    """Run the same point through multiple backends/styles and collect diagnostics."""
    results: list[dict[str, Any]] = []

    for case in cases:
        config = SimpleNamespace(
            click_backend=case.backend,
            mac_click_style=case.mac_click_style,
            preflight_permissions=preflight_permissions,
        )
        action_dict: dict[str, Any] = {
            "intent": intent,
            "experiment_label": case.label or case.backend,
        }
        try:
            if case.action == "click":
                message = await perform_click_sequence(
                    pyautogui,
                    x,
                    y,
                    case.click_count,
                    raw=action_dict,
                    config=config,
                )
            elif case.action == "right_click":
                message = await perform_right_click(
                    pyautogui,
                    x,
                    y,
                    raw=action_dict,
                    config=config,
                )
            elif case.action == "press":
                message = await perform_press(
                    pyautogui,
                    x,
                    y,
                    raw=action_dict,
                    config=config,
                    duration=case.hold_duration,
                )
            else:
                raise ValueError(f"Unsupported experiment action: {case.action}")
            results.append(
                {
                    "success": True,
                    "message": message,
                    **asdict(case),
                    **{
                        key: action_dict.get(key)
                        for key in (
                            "click_backend",
                            "mac_accessibility",
                            "event_style",
                            "frontmost_app",
                            "click_note",
                        )
                    },
                }
            )
        except Exception as exc:
            results.append(
                {
                    "success": False,
                    "message": str(exc),
                    **asdict(case),
                    **{
                        key: action_dict.get(key)
                        for key in (
                            "click_backend",
                            "mac_accessibility",
                            "event_style",
                            "frontmost_app",
                            "click_note",
                        )
                    },
                }
            )

    return results
