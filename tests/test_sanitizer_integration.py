"""Real-execution DOMPurify integration test (opt-in).

Runs `tests/sanitizer/run.js` under Node; auto-skips when Node or JSDOM is
unavailable. See `tests/sanitizer/README.md` for one-time setup.

Why opt-in: JSDOM is ~6MB and lives outside Python deps; we don't want to
require it for every clone. But once installed, this test exercises the
exact `marked.parse → DOMPurify.sanitize` pipeline the browser uses, and
proves attacker payloads do not survive — review-pass-3's complaint that
all our prior sanitizer assertions were structural-only.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

RUN_JS = Path(__file__).parent / "sanitizer" / "run.js"


def test_sanitizer_neutralizes_malicious_markdown_payloads():
    if shutil.which("node") is None:
        pytest.skip("node not installed; sanitizer integration test skipped")
    if not RUN_JS.exists():
        pytest.skip(f"runner missing: {RUN_JS}")

    proc = subprocess.run(
        ["node", str(RUN_JS)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    out = proc.stdout.strip()
    if out == "JSDOM_MISSING":
        pytest.skip(
            "jsdom not installed in tests/sanitizer/. "
            "Run: cd tests/sanitizer && npm install jsdom@26"
        )

    # Anything else: real execution result.
    assert proc.returncode == 0, (
        f"sanitizer rejected malicious payload set:\n{out}\n{proc.stderr}"
    )
    payload_log = json.loads(out)
    assert payload_log["ok"] is True
    # Spot-check at least one expected payload was tested.
    payloads = [r["payload"] for r in payload_log["results"]]
    assert any("script" in p for p in payloads), (
        "expected <script> payload in fixture set"
    )
    # Every result must have empty violations.
    for r in payload_log["results"]:
        assert r["violations"] == [], (
            f"DOMPurify let dangerous content through:\n  payload: {r['payload']}\n"
            f"  output:  {r['out']}\n  violations: {r['violations']}"
        )
