"""One-cycle NANOBOT__ → SYLL__ environment variable backcompat tests.

These tests cover the shim in ``syll.config.loader._migrate_legacy_env_vars``
which copies any ``NANOBOT__*`` environment variables to their ``SYLL__*``
equivalents and emits a DeprecationWarning. The shim exists to let users
upgrading from the nanobot-era install keep their exported env vars working
through the 0.2.x series; it is scheduled for removal in 0.3.0.
"""

import os
import warnings

from syll.config.loader import _migrate_legacy_env_vars


def test_legacy_env_var_migrated(monkeypatch):
    """A NANOBOT__ var with no matching SYLL__ var gets copied, with a warning."""
    monkeypatch.setenv("NANOBOT__TOOLS__GUI__ENABLED", "true")
    monkeypatch.delenv("SYLL__TOOLS__GUI__ENABLED", raising=False)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _migrate_legacy_env_vars()

    assert os.environ.get("SYLL__TOOLS__GUI__ENABLED") == "true"
    # The DeprecationWarning fires and mentions the legacy prefix
    dep_warnings = [
        w for w in caught
        if issubclass(w.category, DeprecationWarning)
        and "NANOBOT__" in str(w.message)
    ]
    assert dep_warnings, "expected DeprecationWarning mentioning NANOBOT__"


def test_new_env_var_not_overwritten_by_legacy(monkeypatch):
    """If the user already set SYLL__, the legacy NANOBOT__ must not clobber it."""
    monkeypatch.setenv("NANOBOT__TOOLS__GUI__ENABLED", "true")
    monkeypatch.setenv("SYLL__TOOLS__GUI__ENABLED", "false")

    _migrate_legacy_env_vars()

    # User-set SYLL__ wins
    assert os.environ["SYLL__TOOLS__GUI__ENABLED"] == "false"


def test_no_legacy_vars_is_silent_noop(monkeypatch):
    """When there are no NANOBOT__ vars, the shim emits no warning."""
    # Clean slate for both prefixes
    for key in list(os.environ.keys()):
        if key.startswith(("NANOBOT__", "SYLL__")):
            monkeypatch.delenv(key, raising=False)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _migrate_legacy_env_vars()

    dep_warnings = [
        w for w in caught
        if issubclass(w.category, DeprecationWarning)
        and "NANOBOT__" in str(w.message)
    ]
    assert not dep_warnings, "no legacy vars → no warning"
