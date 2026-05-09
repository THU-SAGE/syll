"""Tests for CLI banner rendering fallbacks."""

import builtins

from syll.cli.banner import render_ghost_ascii


def test_render_ghost_ascii_degrades_when_pillow_missing(monkeypatch):
    """The cosmetic splash art should not crash syll wake if Pillow is absent."""
    original_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "PIL" or name.startswith("PIL."):
            raise ModuleNotFoundError("No module named 'PIL'")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    lines = render_ghost_ascii(width=16)

    assert lines
    assert "Pillow" in lines[0]
