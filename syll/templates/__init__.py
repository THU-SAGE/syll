"""Shipped templates for first-run workspace bootstrap.

Files under `templates/workspace/` are copied into the user's workspace
directory on first run (and on startup migration, missing-only). Never
overwrites existing files — user customizations are preserved.
"""

from pathlib import Path

TEMPLATES_ROOT: Path = Path(__file__).parent
WORKSPACE_TEMPLATE_ROOT: Path = TEMPLATES_ROOT / "workspace"
WORKSPACE_TEMPLATE_VERSION_SML = "2026-05-05"
