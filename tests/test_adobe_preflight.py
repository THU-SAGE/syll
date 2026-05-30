"""Preflight readiness tests for the Adobe conversational integration.

CI-safe and cross-platform: forces a non-macOS host by monkeypatching
``platform.system`` (which the preflight module reads at call time) so the
macOS-host blocker is asserted without any Adobe app or GUI.

Author: zhangbo <226653803@qq.com>
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

import syll.agent.adobe.preflight as preflight
from syll.agent.adobe.preflight import (
    AdobePreflight,
    audition_preflight,
    photoshop_preflight,
)


@pytest.fixture
def linux_host(monkeypatch):
    """Force the preflight to see a Linux host."""
    monkeypatch.setattr(preflight.platform, "system", lambda: "Linux")


def _gui_config(enabled=True, selected_screen=0):
    return SimpleNamespace(enabled=enabled, selected_screen=selected_screen)


def _call(fn, tmp_path):
    return fn(
        workspace_path=Path(tmp_path),
        gui_config=_gui_config(),
        tool_registry=None,
        skill_store=None,
        coord_profiles_dir=Path(tmp_path) / "coord_profiles",
    )


def test_photoshop_preflight_not_ready_on_linux(linux_host, tmp_path):
    pf = _call(photoshop_preflight, tmp_path)
    assert pf.ready is False
    assert "This Photoshop demo requires a macOS host" in pf.blockers


def test_audition_preflight_not_ready_on_linux(linux_host, tmp_path):
    pf = _call(audition_preflight, tmp_path)
    assert pf.ready is False
    assert "This Audition demo requires a macOS host" in pf.blockers


def test_preflight_ready_is_negation_of_blockers(linux_host, tmp_path):
    for fn in (photoshop_preflight, audition_preflight):
        pf = _call(fn, tmp_path)
        assert pf.ready == (not pf.blockers)


def test_preflight_returns_adobe_preflight_dataclass(linux_host, tmp_path):
    pf = _call(photoshop_preflight, tmp_path)
    assert isinstance(pf, AdobePreflight)
    # Linux host is not applicable for macOS Accessibility.
    assert pf.accessibility == "not_applicable"
    assert pf.recommended_mode == "none"


def test_adobe_preflight_ready_property_matches_blockers():
    assert AdobePreflight(ready=True, blockers=[]).ready == (not [])
    blocked = AdobePreflight(ready=False, blockers=["x"])
    assert blocked.ready == (not blocked.blockers)
