"""Phase 4 review-pass-7: end-to-end install pipeline against a LOCAL fake
released bridge repo.

Exercises the real `install_bridge()` path with a fresh git repo on the
local filesystem (no network), simulating what `THU-SAGE/syll-bridges`
will look like once published. The fake bridge has the same shape as the
planned Stagehand bridge:

  - Subdirectory layout: `<repo>/stagehand-mcp/`
  - TypeScript-style devDependencies that the build needs
  - A pre-build script that emits `dist/index.js`
  - A pinned tag matching `BRIDGE_ALLOWED_TAGS["stagehand"][0]`

After install, the test asserts:

  H1+H2 — `bridge_entry_point("stagehand")` exists, is absolute, no `~`,
          and lives next to `node_modules/` for Node module resolution.
  H3   — `npm ci` ran with dev deps available (build succeeded).
  M1   — A second install without `force=True` raises "already installed";
          a half-clone (manifest missing) lets retry succeed without force.
  M2   — Manifest records {tag, sha} and the SHA matches `git rev-parse HEAD`.

  Template launch closure — the template's resolved `stdio.args[0]` matches
  `bridge_entry_point("stagehand")` exactly. Catches the H1 path mismatch.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from syll.agent.mcp_bridges import (
    BRIDGE_ALLOWED_TAGS,
    BRIDGE_ALLOWLIST,
    BridgeInstallError,
    bridge_entry_point,
    bridge_package_root,
    bridge_path,
    install_bridge,
    is_installed,
    read_manifest,
)
from syll.web.routes._mcp_templates import get_template

pytestmark = pytest.mark.skipif(
    not (shutil.which("git") and shutil.which("node") and shutil.which("npm")),
    reason="git + node + npm required for bridge install e2e",
)


def _build_fake_bridge_repo(repo_root: Path, tag: str) -> str:
    """Initialize a local git repo with the same layout the planned external
    bridge will use. Returns the commit SHA at HEAD."""
    sub = repo_root / "stagehand-mcp"
    sub.mkdir(parents=True, exist_ok=True)

    # package.json with TS-style devDependencies (none actually executed —
    # the build script is shell-only — but the structure exercises the
    # `npm ci` (full) → build → prune pipeline).
    pkg = {
        "name": "stagehand-mcp-fake",
        "version": "0.1.0",
        "private": True,
        "scripts": {
            # No real tsc — pretend a build by emitting a stub dist file.
            "build": "mkdir -p dist && echo 'console.log(\"stagehand-fake-ok\")' > dist/index.js",
        },
        "dependencies": {},
        "devDependencies": {},
    }
    (sub / "package.json").write_text(json.dumps(pkg, indent=2), encoding="utf-8")
    # `npm ci` requires a lockfile. Generate one inline (empty).
    (sub / "package-lock.json").write_text(
        json.dumps({
            "name": "stagehand-mcp-fake",
            "version": "0.1.0",
            "lockfileVersion": 3,
            "requires": True,
            "packages": {"": {"name": "stagehand-mcp-fake", "version": "0.1.0"}},
        }, indent=2),
        encoding="utf-8",
    )

    # Init repo + commit + tag.
    def _git(*args, cwd=repo_root):
        subprocess.run(
            ["git", *args], cwd=cwd, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    _git("init", "-q")
    _git("config", "user.email", "test@example.com")
    _git("config", "user.name", "syll-test")
    _git("add", ".")
    _git("commit", "-q", "-m", "init")
    _git("tag", tag)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_root,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return sha


@pytest.fixture
def fake_released_stagehand(monkeypatch, tmp_path):
    """Create a local "released" bridge repo and rewire BRIDGE_ALLOWLIST to
    point at it. Bridges install destination is also redirected into tmpdir
    so we don't touch ~/.syll."""
    repo = tmp_path / "syll-bridges-repo"
    bridges = tmp_path / "bridges"

    tag = BRIDGE_ALLOWED_TAGS["stagehand"][0]
    sha = _build_fake_bridge_repo(repo, tag)

    # Point the allowlist at the local repo + flip released=True.
    spec = dict(BRIDGE_ALLOWLIST["stagehand"])
    spec["url"] = str(repo)
    spec["released"] = True
    monkeypatch.setitem(BRIDGE_ALLOWLIST, "stagehand", spec)

    # Redirect install destination.
    monkeypatch.setattr("syll.agent.mcp_bridges.bridges_root", lambda: bridges)
    monkeypatch.setattr(
        "syll.agent.mcp_bridges.bridge_path",
        lambda name: bridges / name,
    )
    monkeypatch.setattr(
        "syll.agent.mcp_bridges._manifest_path",
        lambda name: bridges / name / ".syll-install.json",
    )
    # bridge_package_root and bridge_entry_point read BRIDGE_ALLOWLIST and
    # bridge_path() — both are now monkeypatched to the right values.

    yield {"repo": repo, "tag": tag, "sha": sha, "bridges": bridges}


# ── Real install pipeline ──────────────────────────────────────────────


@pytest.mark.timeout(60)
async def test_real_install_pipeline_produces_runnable_layout(fake_released_stagehand):
    """The end-to-end happy path. After install:
       - manifest exists with the right SHA + tag + entry_point
       - dist/index.js lives at bridge_entry_point()
       - node_modules sits next to dist (Node module resolution)
       - is_installed() returns True
    """
    info = fake_released_stagehand
    manifest = await install_bridge("stagehand")
    assert manifest["name"] == "stagehand"
    assert manifest["tag"] == info["tag"]
    assert manifest["sha"] == info["sha"], (
        f"manifest sha {manifest['sha']!r} doesn't match repo HEAD {info['sha']!r}"
    )

    entry = bridge_entry_point("stagehand")
    assert entry.exists(), f"entry-point JS missing at {entry}"
    assert entry.is_absolute()
    assert "~" not in str(entry), "entry path must be expanded"

    pkg_root = bridge_package_root("stagehand")
    # H2 invariant: dist lives INSIDE the package root (next to package.json
    # and node_modules), NOT moved up to <bridge_path>/dist. The fake bridge
    # has no deps so node_modules is empty/absent — that's fine. The
    # critical thing is layout: when a real bridge with deps installs, its
    # node_modules will sit next to dist and Node module resolution works.
    assert (pkg_root / "package.json").exists()
    assert (pkg_root / "dist" / "index.js").exists()
    # And `<bridge_path>/dist/` must NOT exist (the previous code moved
    # dist up to the clone root, breaking module resolution).
    if pkg_root != bridge_path("stagehand"):
        assert not (bridge_path("stagehand") / "dist").exists(), (
            "dist must NOT be moved out of the package root — review-pass-7 H2"
        )

    assert is_installed("stagehand") is True


@pytest.mark.timeout(60)
async def test_template_arg_matches_installer_entry_point(fake_released_stagehand):
    """Review-pass-7 H1 closure: the Stagehand template's args[0] must
    match `bridge_entry_point("stagehand")` exactly so a "Use template"
    save followed by Save+Confirm can actually launch the installed bridge."""
    await install_bridge("stagehand")
    template = get_template("stagehand")
    assert template is not None
    args = template["config"]["stdio"]["args"]
    assert len(args) == 1
    assert args[0] == str(bridge_entry_point("stagehand"))
    # Belt-and-braces: the JS file at args[0] is real.
    assert Path(args[0]).exists()


# ── Manifest SHA + commit auditing (M2) ────────────────────────────────


@pytest.mark.timeout(60)
async def test_manifest_records_sha_matching_git_head(fake_released_stagehand):
    info = fake_released_stagehand
    await install_bridge("stagehand")
    m = read_manifest("stagehand")
    assert m is not None
    assert "sha" in m, "manifest must record commit SHA"
    assert m["sha"] == info["sha"]


# ── Partial-install retry (M1) ─────────────────────────────────────────


@pytest.mark.timeout(60)
async def test_partial_install_does_not_block_retry(fake_released_stagehand, tmp_path):
    """If the build step fails, the cleanup-on-failure path removes dest so
    the next attempt can try again — no `--force` required.

    Simulate by patching `_run_subprocess` to make `npm run build` fail
    on the first attempt only.
    """
    info = fake_released_stagehand
    from syll.agent import mcp_bridges as mod

    real = mod._run_subprocess
    call_count = {"npm_build": 0}

    async def flaky_npm(*cmd, **kwargs):
        if cmd[:3] == ("npm", "run", "build"):
            call_count["npm_build"] += 1
            if call_count["npm_build"] == 1:
                # Pretend build failed by returning non-zero.
                return 1
        return await real(*cmd, **kwargs)

    # First attempt: build fails → cleanup runs.
    import unittest.mock as _mock
    with _mock.patch.object(mod, "_run_subprocess", flaky_npm):
        with pytest.raises(BridgeInstallError, match="npm run build failed"):
            await install_bridge("stagehand")
        # Dest swept clean by the cleanup-on-failure wrapper.
        assert not bridge_path("stagehand").exists(), (
            "partial install must be cleaned up — review-pass-7 M1"
        )
        # Second attempt succeeds (no --force needed).
        await install_bridge("stagehand")
    assert is_installed("stagehand") is True


# ── Already-installed semantics ────────────────────────────────────────


@pytest.mark.timeout(60)
async def test_double_install_without_force_raises(fake_released_stagehand):
    await install_bridge("stagehand")
    with pytest.raises(BridgeInstallError, match="already installed"):
        await install_bridge("stagehand")


@pytest.mark.timeout(60)
async def test_double_install_with_force_replaces(fake_released_stagehand):
    info = fake_released_stagehand
    m1 = await install_bridge("stagehand")
    # Touch a sentinel inside the package — force install must wipe it.
    sentinel = bridge_package_root("stagehand") / "_stale.txt"
    sentinel.write_text("old", encoding="utf-8")
    assert sentinel.exists()
    m2 = await install_bridge("stagehand", force=True)
    assert not sentinel.exists()
    assert m1["sha"] == m2["sha"]  # same tag, same commit


# ── Node preflight + cancellation cleanup ──────────────────────────────


@pytest.mark.timeout(10)
async def test_node_version_preflight_fails_before_clone(fake_released_stagehand, monkeypatch):
    """Low Node versions should fail before clone/npm side effects."""
    from syll.agent import mcp_bridges as mod

    async def _old_node():
        return "v18.19.0"

    monkeypatch.setattr(mod, "_capture_node_version", _old_node)
    with pytest.raises(BridgeInstallError, match="requires Node >= 20.19.0"):
        await install_bridge("stagehand")
    assert not bridge_path("stagehand").exists()


@pytest.mark.timeout(60)
async def test_node_version_preflight_does_not_delete_existing_install(
    fake_released_stagehand,
    monkeypatch,
):
    """A forced reinstall with a bad Node version must not remove a working bridge."""
    from syll.agent import mcp_bridges as mod

    await install_bridge("stagehand")
    entry = bridge_entry_point("stagehand")
    assert entry.exists()

    async def _old_node():
        return "v18.19.0"

    monkeypatch.setattr(mod, "_capture_node_version", _old_node)
    with pytest.raises(BridgeInstallError, match="requires Node >= 20.19.0"):
        await install_bridge("stagehand", force=True)
    assert entry.exists()


@pytest.mark.skipif(os.name == "nt", reason="PID liveness assertion is POSIX-only")
@pytest.mark.timeout(10)
async def test_run_subprocess_cancel_terminates_child(tmp_path):
    """Cancelling an install step must not leave the child process orphaned."""
    from syll.agent import mcp_bridges as mod

    pid_file = tmp_path / "child.pid"
    script = (
        "import os, pathlib, time; "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); "
        "time.sleep(60)"
    )
    task = asyncio.create_task(
        mod._run_subprocess(
            sys.executable,
            "-c",
            script,
            job_id="cancel-test",
            bridge="stagehand",
        )
    )
    for _ in range(50):
        if pid_file.exists():
            break
        await asyncio.sleep(0.05)
    assert pid_file.exists(), "child process did not start"
    pid = int(pid_file.read_text(encoding="utf-8"))

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.sleep(0.1)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
