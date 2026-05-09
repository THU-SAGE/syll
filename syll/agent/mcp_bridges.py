"""Out-of-tree MCP bridge install logic — Phase 4b scaffolding.

The Stagehand MCP bridge is a Node project that lives in a separate
`THU-SAGE/syll-bridges` repository. This module knows how to clone, build,
and verify a pinned version into `~/.syll/bridges/<name>/`.

**Today (scaffold)**: the source URL constants below are placeholders. The
install command surfaces a clear "bridge repo not yet released" error to
the user, and the test suite exercises the full pipeline against a stub
git+npm. When the external repo is published, only the constants need to
change — the wiring (CLI, HTTP endpoint, WS progress, UI button) is
already in place.

**Why scaffold rather than wait**: doing this now gives users a single
visible upgrade path (`syll bridge install stagehand`) and lets the UI
gracefully degrade until the repo lands. It also surfaces the security
posture (admin-gated, allowlisted bridges, pinned tags, no `@latest`)
before adding the actual external dependency.

Bridge spec, per Phase 4 plan rev. 6:
  - `name` : known string, must be in `BRIDGE_ALLOWLIST`.
  - `version` : a Git tag from `BRIDGE_ALLOWED_TAGS[name]`.
  - install: `git clone --depth 1 --branch <tag> <url> dest`
             then `npm ci`, `npm run build`, and `npm prune --omit=dev`.
  - destination: `~/.syll/bridges/<name>/`
  - manifest: `~/.syll/bridges/<name>/.syll-install.json` records
    {url, tag, sha (commit), installed_at_ms}. Used by `bridge list`.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from loguru import logger

# ── Bridge allowlist ──────────────────────────────────────────────────────
#
# Today these are placeholders. Once `THU-SAGE/syll-bridges` is published,
# update the URL and tag list. The allowlist is the security boundary: only
# names that appear here can ever be passed to the install command.

BRIDGE_ALLOWLIST: dict[str, dict] = {
    "stagehand": {
        "url": "https://github.com/THU-SAGE/syll-bridges.git",
        "subdirectory": "stagehand-mcp",
        "default_tag": "stagehand-mcp/v0.1.0",
        "node_min_version": "20.19.0",
        "released": False,  # Flip to True when the external repo lands.
    },
}

BRIDGE_ALLOWED_TAGS: dict[str, list[str]] = {
    "stagehand": ["stagehand-mcp/v0.1.0"],
}

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")
_SAFE_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR")


def bridges_root() -> Path:
    return (Path.home() / ".syll" / "bridges").expanduser()


def bridge_path(name: str) -> Path:
    """Where the bridge repo gets cloned. Always under bridges_root()."""
    return bridges_root() / name


def bridge_package_root(name: str) -> Path:
    """The package root after install — where `node_modules/` lives.

    If the spec has a `subdirectory`, this is `<clone_root>/<subdirectory>`.
    Otherwise it's `<clone_root>` itself. Critical for Node's module
    resolution: running `<package_root>/dist/index.js` looks up
    `<package_root>/node_modules/`. Earlier code moved `dist` up one level,
    which broke this — review-pass-7 H2.
    """
    spec = BRIDGE_ALLOWLIST.get(name, {})
    sub = spec.get("subdirectory")
    return bridge_path(name) / sub if sub else bridge_path(name)


def bridge_entry_point(name: str) -> Path:
    """Absolute path to the bridge's entry JS — what the MCP template should
    point at. Returned as an EXPANDED, absolute Path (no `~`); MCP stdio
    spawns via `create_subprocess_exec` which doesn't shell-expand `~`."""
    return bridge_package_root(name) / "dist" / "index.js"


# ── Manifest ─────────────────────────────────────────────────────────────


def _manifest_path(name: str) -> Path:
    return bridge_path(name) / ".syll-install.json"


def read_manifest(name: str) -> dict | None:
    p = _manifest_path(name)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"bridge[{name}] manifest unreadable: {e}")
        return None


def is_installed(name: str) -> bool:
    """A bridge is "installed" iff its manifest AND entry-point JS both exist.

    Checking the manifest alone isn't enough (a partial install could have
    written one); checking dist alone would miss a dist that's stale from
    a prior install. Both gates together mean: build succeeded and we
    explicitly recorded success.
    """
    if read_manifest(name) is None:
        return False
    return bridge_entry_point(name).exists()


def list_installed() -> list[dict]:
    out: list[dict] = []
    for name in BRIDGE_ALLOWLIST:
        m = read_manifest(name)
        out.append({
            "name": name,
            "installed": is_installed(name),
            "entry_point": str(bridge_entry_point(name)),
            "manifest": m,
            "released": BRIDGE_ALLOWLIST[name]["released"],
            "default_tag": BRIDGE_ALLOWLIST[name]["default_tag"],
        })
    return out


# ── Errors + result type ─────────────────────────────────────────────────


class BridgeInstallError(Exception):
    pass


class BridgeNotReleasedError(BridgeInstallError):
    """Raised before the external bridge repo is published."""


@dataclass
class InstallProgress:
    job_id: str
    bridge: str
    line: str
    status: str = "running"  # "running" | "ok" | "error"
    extra: dict = field(default_factory=dict)


ProgressFn = Callable[[InstallProgress], Awaitable[None]]


# ── Install pipeline ─────────────────────────────────────────────────────


async def _emit(progress: ProgressFn | None, p: InstallProgress) -> None:
    if progress is None:
        return
    try:
        await progress(p)
    except Exception as e:
        logger.warning(f"bridge[{p.bridge}] progress emit failed: {e}")


def _safe_subprocess_env() -> dict[str, str]:
    return {k: os.environ[k] for k in _SAFE_ENV_KEYS if k in os.environ}


def _parse_version(v: str) -> tuple[int, int, int] | None:
    match = _VERSION_RE.match(v.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


async def _capture_node_version() -> str | None:
    """Return `node --version`, or None when node is unavailable/broken."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "node",
            "--version",
            env=_safe_subprocess_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            return None
        version = out.decode("utf-8", errors="replace").strip()
        return version or None
    except OSError:
        return None


async def _require_node_version(name: str, spec: dict) -> None:
    """Fail fast when the bridge declares a Node minimum that is not met."""
    required = spec.get("node_min_version")
    if not required:
        return

    actual = await _capture_node_version()
    if actual is None:
        raise BridgeInstallError(
            f"bridge {name!r} requires Node >= {required}, but `node --version` "
            "could not be executed"
        )

    parsed_actual = _parse_version(actual)
    parsed_required = _parse_version(str(required))
    if parsed_actual is None or parsed_required is None:
        raise BridgeInstallError(
            f"bridge {name!r} requires Node >= {required}; installed Node "
            f"reported unsupported version {actual!r}"
        )
    if parsed_actual < parsed_required:
        raise BridgeInstallError(
            f"bridge {name!r} requires Node >= {required}; found {actual}. "
            "Install a newer Node runtime before installing this bridge."
        )


async def _terminate_process(proc: asyncio.subprocess.Process) -> None:
    """Best-effort subprocess cleanup used when an install task is cancelled."""
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            return
        await proc.wait()


async def _run_subprocess(
    *cmd: str,
    cwd: Path | None = None,
    progress: ProgressFn | None = None,
    job_id: str = "",
    bridge: str = "",
) -> int:
    """Run a subprocess, streaming stdout+stderr line-by-line into `progress`.

    Inherits `PATH`/`HOME` only; explicit env hygiene matches the MCP stdio
    pattern (no LD_PRELOAD, no inherited cloud creds).
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd) if cwd else None,
        env=_safe_subprocess_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        assert proc.stdout is not None
        while True:
            line_bytes = await proc.stdout.readline()
            if not line_bytes:
                break
            line = line_bytes.decode("utf-8", errors="replace").rstrip()
            if line:
                await _emit(progress, InstallProgress(job_id=job_id, bridge=bridge, line=line))
        rc = await proc.wait()
        return rc
    except asyncio.CancelledError:
        await _terminate_process(proc)
        raise
    except Exception:
        await _terminate_process(proc)
        raise



async def install_bridge(
    name: str,
    version: str | None = None,
    *,
    job_id: str = "",
    progress: ProgressFn | None = None,
    force: bool = False,
) -> dict:
    """Clone, build, and verify a bridge into `~/.syll/bridges/<name>/`.

    Returns the new manifest dict on success. Raises `BridgeInstallError`
    on any failure (including the placeholder-repo state).
    """
    if name not in BRIDGE_ALLOWLIST:
        raise BridgeInstallError(f"unknown bridge {name!r}")
    spec = BRIDGE_ALLOWLIST[name]
    if not spec["released"]:
        # Phase 4b scaffold: external repo not yet published. The CLI / HTTP
        # surface still works — clients see a clean error.
        raise BridgeNotReleasedError(
            f"bridge {name!r} is not yet released; the source repo "
            f"({spec['url']}) is a placeholder. Track the corresponding "
            "issue or watch the syll-bridges repo for a tagged release."
        )

    tag = version or spec["default_tag"]
    if tag not in BRIDGE_ALLOWED_TAGS.get(name, []):
        raise BridgeInstallError(
            f"version {tag!r} not in allowed tags for {name!r}: "
            f"{BRIDGE_ALLOWED_TAGS.get(name, [])}"
        )
    await _require_node_version(name, spec)

    dest = bridge_path(name)
    # Review-pass-7 M1: replace `dest.exists()` with `is_installed(name)`.
    # A partial install (clone succeeded, build failed) leaves a dest dir
    # but no manifest — without this change, the second attempt fails with
    # "already installed" and the user is stuck.
    if is_installed(name) and not force:
        raise BridgeInstallError(
            f"bridge {name!r} already installed at {dest}; pass force=True to reinstall"
        )

    if dest.exists():
        await _emit(
            progress,
            InstallProgress(
                job_id=job_id, bridge=name,
                line=f"removing existing {dest} (force={force} or partial install)",
            ),
        )
        shutil.rmtree(dest, ignore_errors=True)

    bridges_root().mkdir(parents=True, exist_ok=True)

    # Review-pass-7 M1: wrap the entire pipeline; on failure, sweep the
    # half-built dest so the user can retry without --force.
    try:
        return await _install_bridge_pipeline(
            name=name,
            spec=spec,
            tag=tag,
            dest=dest,
            job_id=job_id,
            progress=progress,
        )
    except BaseException:
        # Best-effort cleanup; never mask the original exception.
        try:
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
        except Exception as e:
            logger.warning(f"bridge[{name}] cleanup after failure raised: {e}")
        raise


async def _install_bridge_pipeline(
    *,
    name: str,
    spec: dict,
    tag: str,
    dest: Path,
    job_id: str,
    progress: ProgressFn | None,
) -> dict:
    """The actual clone → install → build sequence. Separated from
    `install_bridge` so the outer function can wrap with cleanup-on-failure
    semantics without nesting try/except.
    """
    await _emit(
        progress,
        InstallProgress(
            job_id=job_id, bridge=name,
            line=f"cloning {spec['url']} @ {tag} into {dest}",
        ),
    )
    rc = await _run_subprocess(
        "git", "clone", "--depth", "1", "--branch", tag, spec["url"], str(dest),
        progress=progress, job_id=job_id, bridge=name,
    )
    if rc != 0:
        raise BridgeInstallError(f"git clone failed: exit {rc}")

    # Review-pass-7 M2: capture commit SHA so the manifest records what
    # was ACTUALLY installed, not just the tag (tags can be moved).
    sha = await _capture_git_sha(dest)
    if sha is None:
        raise BridgeInstallError(
            f"git rev-parse HEAD failed in {dest} after clone"
        )

    sub = spec.get("subdirectory")
    work_dir = dest / sub if sub else dest
    if not (work_dir / "package.json").exists():
        raise BridgeInstallError(f"package.json missing in {work_dir}")

    # Review-pass-7 H3: TS/Node bridges typically keep build tooling
    # (typescript / tsup / vite / esbuild) in devDependencies. Running
    # `npm ci --omit=dev` first would skip them and the subsequent build
    # would fail. Order is now: full ci → build → prune.
    await _emit(
        progress,
        InstallProgress(
            job_id=job_id, bridge=name, line=f"npm ci in {work_dir}",
        ),
    )
    rc = await _run_subprocess(
        "npm", "ci",
        cwd=work_dir, progress=progress, job_id=job_id, bridge=name,
    )
    if rc != 0:
        raise BridgeInstallError(f"npm ci failed: exit {rc}")

    await _emit(
        progress,
        InstallProgress(job_id=job_id, bridge=name, line="npm run build"),
    )
    rc = await _run_subprocess(
        "npm", "run", "build",
        cwd=work_dir, progress=progress, job_id=job_id, bridge=name,
    )
    if rc != 0:
        raise BridgeInstallError(f"npm run build failed: exit {rc}")

    # Slim down: drop devDependencies AFTER build succeeded.
    await _emit(
        progress,
        InstallProgress(job_id=job_id, bridge=name, line="npm prune --omit=dev"),
    )
    rc = await _run_subprocess(
        "npm", "prune", "--omit=dev",
        cwd=work_dir, progress=progress, job_id=job_id, bridge=name,
    )
    if rc != 0:
        # Prune failure is non-fatal — bridge will still run with dev deps
        # present. Log and continue.
        logger.warning(f"bridge[{name}] npm prune failed (rc={rc}); continuing")

    # Review-pass-7 H1+H2: don't move dist out of the package root.
    # `node <pkg>/dist/index.js` resolves modules under `<pkg>/node_modules`,
    # so dist must stay where npm ci put it.
    entry = bridge_entry_point(name)
    if not entry.exists():
        raise BridgeInstallError(
            f"build completed but {entry} missing — expected entry-point JS"
        )

    manifest = {
        "name": name,
        "url": spec["url"],
        "tag": tag,
        "sha": sha,
        "subdirectory": sub or "",
        "entry_point": str(entry),
        "installed_at_ms": int(time.time() * 1000),
    }
    _manifest_path(name).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    await _emit(
        progress,
        InstallProgress(
            job_id=job_id, bridge=name,
            line=f"installed at {entry} (tag={tag} sha={sha[:8]})",
            status="ok",
            extra={"manifest": manifest},
        ),
    )
    return manifest


async def _capture_git_sha(repo: Path) -> str | None:
    """Return the current HEAD commit SHA, or None if `git rev-parse` fails."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "rev-parse", "HEAD",
            cwd=str(repo),
            env=_safe_subprocess_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            return None
        sha = out.decode("utf-8").strip()
        return sha or None
    except Exception as e:
        logger.warning(f"git rev-parse failed: {e}")
        return None


async def uninstall_bridge(name: str) -> bool:
    """Remove `~/.syll/bridges/<name>/`. Returns True if anything was removed."""
    if name not in BRIDGE_ALLOWLIST:
        raise BridgeInstallError(f"unknown bridge {name!r}")
    dest = bridge_path(name)
    if not dest.exists():
        return False
    shutil.rmtree(dest)
    return True
