"""Consent + gating tests for the Adobe conversational tools.

CI-safe and cross-platform: never drives a GUI and never opens an Adobe app.
The GUI leg (``registry.execute("gui_action"...)``) is asserted to be NEVER
reached on the find/blocker/consent paths, so no real screen takeover can
happen. Readiness is controlled by monkeypatching the preflight functions that
the tool modules call, so the tests behave identically on macOS and Linux CI.

Author: zhangbo <226653803@qq.com>
"""

from types import SimpleNamespace

import syll.agent.tools.clean_audio_in_audition as audition_mod
import syll.agent.tools.photoshop_cutout as photoshop_mod
from syll.agent.adobe.preflight import AdobePreflight
from syll.agent.adobe.register import (
    ADOBE_TOOL_NAMES,
    register_adobe_tools,
    unregister_adobe_tools,
)
from syll.agent.tools.base import ToolResult
from syll.agent.tools.clean_audio_in_audition import CleanAudioInAuditionTool
from syll.agent.tools.photoshop_cutout import PhotoshopCutoutTool
from syll.agent.tools.registry import ToolRegistry


class SpyRegistry:
    """Tool registry stand-in that records whether the GUI leg was invoked."""

    def __init__(self):
        self.executed: list[tuple[str, dict]] = []

    def has(self, _name: str) -> bool:
        return False

    async def execute(self, name, params):
        self.executed.append((name, params))
        return ToolResult(text="should-not-be-reached")


def _gui_config(enabled=True, selected_screen=0):
    return SimpleNamespace(enabled=enabled, selected_screen=selected_screen)


def _not_ready_pf() -> AdobePreflight:
    return AdobePreflight(
        ready=False,
        blockers=["This Photoshop demo requires a macOS host"],
        warnings=[],
    )


def _ready_pf() -> AdobePreflight:
    return AdobePreflight(
        ready=True,
        blockers=[],
        warnings=[],
        recommended_mode="zero_shot",
        zero_shot_mode_available=True,
        app_name="Adobe Photoshop",
    )


def _make_photoshop(tmp_path, registry):
    return PhotoshopCutoutTool(
        registry=registry,
        gui_config=_gui_config(),
        workspace=tmp_path,
        skill_store=None,
    )


def _make_audition(tmp_path, registry):
    return CleanAudioInAuditionTool(
        registry=registry,
        gui_config=_gui_config(),
        workspace=tmp_path,
        skill_store=None,
    )


# --- tool name strings -----------------------------------------------------


def test_tool_name_strings(tmp_path):
    reg = SpyRegistry()
    assert _make_photoshop(tmp_path, reg).name == "photoshop_cutout"
    assert _make_audition(tmp_path, reg).name == "clean_audio_in_audition"


# --- nonexistent input short-circuits before any GUI/preflight -------------


async def test_photoshop_missing_image_returns_cant_find(tmp_path):
    reg = SpyRegistry()
    tool = _make_photoshop(tmp_path, reg)
    result = await tool.execute(image_path=str(tmp_path / "nope.png"))
    assert isinstance(result, ToolResult)
    assert "can't find" in result.text
    assert reg.executed == []


async def test_audition_missing_audio_returns_cant_find(tmp_path):
    reg = SpyRegistry()
    tool = _make_audition(tmp_path, reg)
    result = await tool.execute(audio_path=str(tmp_path / "nope.wav"))
    assert isinstance(result, ToolResult)
    assert "can't find" in result.text
    assert reg.executed == []


# --- not-ready host returns a blocker and never calls the registry ---------


async def test_photoshop_not_ready_returns_blocker_no_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(photoshop_mod, "photoshop_preflight", lambda **_: _not_ready_pf())
    img = tmp_path / "in.png"
    img.write_bytes(b"x")  # presence is enough; preflight is what blocks
    reg = SpyRegistry()
    tool = _make_photoshop(tmp_path, reg)

    result = await tool.execute(image_path=str(img), confirmed=True)

    assert isinstance(result, ToolResult)
    assert "can't run the Photoshop cutout" in result.text
    assert "macOS host" in result.text
    assert reg.executed == []


async def test_audition_not_ready_returns_blocker_no_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(
        audition_mod,
        "audition_preflight",
        lambda **_: AdobePreflight(ready=False, blockers=["This Audition demo requires a macOS host"]),
    )
    aud = tmp_path / "in.wav"
    aud.write_bytes(b"x")
    reg = SpyRegistry()
    tool = _make_audition(tmp_path, reg)

    result = await tool.execute(audio_path=str(aud), confirmed=True)

    assert isinstance(result, ToolResult)
    assert "can't run the Audition cleanup" in result.text
    assert "macOS host" in result.text
    assert reg.executed == []


# --- ready host but unconfirmed returns consent and never calls registry ---


async def test_photoshop_ready_but_unconfirmed_returns_consent_no_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(photoshop_mod, "photoshop_preflight", lambda **_: _ready_pf())
    img = tmp_path / "in.png"
    img.write_bytes(b"x")
    reg = SpyRegistry()
    tool = _make_photoshop(tmp_path, reg)

    result = await tool.execute(image_path=str(img), confirmed=False)

    assert isinstance(result, ToolResult)
    assert "takes over your mouse and" in result.text
    assert "Adobe Photoshop" in result.text
    assert reg.executed == []


async def test_audition_ready_but_unconfirmed_returns_consent_no_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(
        audition_mod,
        "audition_preflight",
        lambda **_: AdobePreflight(
            ready=True,
            blockers=[],
            warnings=[],
            zero_shot_mode_available=True,
            app_name="Adobe Audition",
        ),
    )
    aud = tmp_path / "in.wav"
    aud.write_bytes(b"x")
    reg = SpyRegistry()
    tool = _make_audition(tmp_path, reg)

    result = await tool.execute(audio_path=str(aud), confirmed=False)

    assert isinstance(result, ToolResult)
    assert "takes over your mouse and" in result.text
    assert "Adobe Audition" in result.text
    assert reg.executed == []


# --- registration / unregistration -----------------------------------------


def test_register_and_unregister_adobe_tools(tmp_path):
    registry = ToolRegistry()
    register_adobe_tools(
        registry,
        agent_loop=None,
        gui_config=_gui_config(enabled=True),
        syll_config=None,
        workspace=tmp_path,
        skill_store=None,
        event_store=None,
    )

    assert registry.has("photoshop_cutout")
    assert registry.has("clean_audio_in_audition")
    for name in ADOBE_TOOL_NAMES:
        assert name in registry.tool_names

    unregister_adobe_tools(registry)

    assert not registry.has("photoshop_cutout")
    assert not registry.has("clean_audio_in_audition")
    for name in ADOBE_TOOL_NAMES:
        assert name not in registry.tool_names


def test_register_adobe_tools_is_idempotent(tmp_path):
    registry = ToolRegistry()
    kwargs = dict(
        agent_loop=None,
        gui_config=_gui_config(enabled=True),
        syll_config=None,
        workspace=tmp_path,
        skill_store=None,
        event_store=None,
    )
    register_adobe_tools(registry, **kwargs)
    register_adobe_tools(registry, **kwargs)
    # Two registrations must not produce duplicates.
    assert registry.tool_names.count("photoshop_cutout") == 1
    assert registry.tool_names.count("clean_audio_in_audition") == 1
