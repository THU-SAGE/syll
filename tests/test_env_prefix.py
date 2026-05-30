"""SYLL__ environment-variable prefix tests.

With ``env_prefix="SYLL__"`` and ``env_nested_delimiter="__"``, the documented
``SYLL__SECTION__FIELD`` form must resolve to the matching ``Config`` field.
"""

from syll.config.schema import Config


def test_syll_env_prefix_overrides_config(monkeypatch):
    """``SYLL__SECTION__FIELD`` reaches the right field."""
    monkeypatch.setenv("SYLL__GATEWAY__PORT", "23456")
    monkeypatch.setenv("SYLL__TOOLS__GUI__ENABLED", "true")

    cfg = Config()

    assert cfg.gateway.port == 23456
    assert cfg.tools.gui.enabled is True
