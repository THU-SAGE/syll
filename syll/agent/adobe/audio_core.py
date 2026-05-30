"""Framework-free Adobe Audition control and export helpers.

These helpers keep Audition responsible for the creative repair step and keep
deterministic verification in Python. They contain no web framework coupling:
every function takes plain arguments (paths, strings) and returns values or
raises ordinary exceptions.
"""

from __future__ import annotations

import asyncio
import platform
import shutil
from pathlib import Path

from loguru import logger

AUDIO_SKILL_NAME = "audition-clean-voice"
PENDING_DIR_NAME = "_pending"
AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac"}
SHORT_EXPORT_DIR = Path("/tmp/syll_audio_export")
TARGET_LUFS = -18.0
MAX_UPLOAD_BYTES = 200 * 1024 * 1024


async def run_quiet(*args: str, timeout: float = 15.0, input_text: str | None = None) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if input_text is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input_text.encode() if input_text is not None else None),
            timeout=timeout,
        )
        output = (stdout + stderr).decode(errors="replace").strip()
        return proc.returncode or 0, output
    except Exception as exc:
        return 1, str(exc)


def detect_audition() -> list[str]:
    apps = []
    root = Path("/Applications")
    if root.exists():
        apps.extend(root.glob("Adobe Audition*.app"))
        apps.extend(root.glob("Adobe Audition*/Adobe Audition*.app"))
    return sorted({str(p) for p in apps})


def lufs_available() -> bool:
    try:
        import pyloudnorm  # noqa: F401
        return True
    except Exception:
        return False


async def normalize_to_wav(src: Path, dest: Path) -> None:
    """Normalize audio to 48k mono WAV for deterministic metrics and Audition input."""
    if src.suffix.lower() == ".wav" and shutil.which("ffmpeg") is None:
        shutil.copyfile(src, dest)
        return
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not installed; only WAV uploads can be used")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner", "-loglevel", "error",
        "-y",
        "-i", str(src),
        "-ac", "1",
        "-ar", "48000",
        "-sample_fmt", "s16",
        str(dest),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        tail = stderr.decode(errors="replace").strip().splitlines()[-6:]
        raise RuntimeError("ffmpeg normalize failed: " + " | ".join(tail))


async def set_clipboard_text(text: str) -> None:
    if platform.system() != "Darwin":
        return
    try:
        import pyperclip

        pyperclip.copy(text)
        return
    except Exception:
        pass
    code, output = await run_quiet("pbcopy", timeout=3.0, input_text=text)
    if code != 0:
        logger.warning(f"failed to seed clipboard for audio export: {output}")


async def preopen_audition(input_wav: Path) -> str:
    """Open the file in Audition before zero-shot control so no Terminal is needed."""
    if platform.system() != "Darwin":
        return "skipped: not macOS"

    app_paths = detect_audition()
    app_arg = app_paths[0] if app_paths else "Adobe Audition"
    code, output = await run_quiet("open", "-a", app_arg, str(input_wav), timeout=20.0)
    if code != 0 and app_arg != "Adobe Audition":
        code, output = await run_quiet("open", "-a", "Adobe Audition", str(input_wav), timeout=20.0)
    if code != 0:
        return f"open failed: {output}"

    await asyncio.sleep(2.0)
    app_name = Path(app_arg).stem if app_arg.endswith(".app") else "Adobe Audition"
    await run_quiet("osascript", "-e", f'tell application "{app_name}" to activate', timeout=5.0)
    return f"opened with {app_name}"


def export_candidates(run_dir: Path, pending_file: Path, short_file: Path, final_file: Path) -> list[Path]:
    roots = [run_dir, short_file.parent, pending_file.parent]
    exact = [
        final_file,
        run_dir / "cleaned.wav",
        run_dir / "cleaned.wav.wav",
        short_file,
        short_file.with_name("cleaned.wav.wav"),
        pending_file,
        pending_file.with_name("cleaned.wav.wav"),
    ]
    globbed: list[Path] = []
    for root in roots:
        if root.exists():
            globbed.extend(sorted(root.glob("cleaned*.wav"), key=lambda p: p.stat().st_mtime, reverse=True))

    candidates: list[Path] = []
    seen: set[Path] = set()
    for candidate in exact + globbed:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        candidates.append(candidate)
    return candidates


def should_finalize_loudness(report) -> bool:
    after_lufs = getattr(getattr(report, "after", None), "integrated_lufs", None)
    if after_lufs is None:
        return False
    if abs(after_lufs - TARGET_LUFS) <= 1.5:
        return False
    if getattr(report, "gain_only_transform", False):
        return False
    noise_delta = getattr(report, "noise_floor_delta_db", None)
    noise_rel_delta = getattr(report, "noise_to_rms_delta_db", None)
    sib_delta = getattr(report, "sibilance_delta_db", None)
    sib_rel_delta = getattr(report, "sibilance_to_rms_delta_db", None)
    return any(
        value is not None and value <= threshold
        for value, threshold in (
            (noise_delta, -3.0),
            (noise_rel_delta, -6.0),
            (sib_delta, 0.0),
            (sib_rel_delta, -1.5),
        )
    )


def build_audio_instruction(
    *,
    run_id: str,
    input_wav: Path,
    run_dir: Path,
    pending: Path,
    short_export_dir: Path,
    final_wav: Path,
    mode: str,
) -> str:
    preferred_save_folder = short_export_dir if mode == "zero_shot" else pending
    return (
        f"Audition audio run {run_id}. The input WAV is {input_wav}. "
        "Goal: produce a noticeably repaired spoken-voice WAV, not a gain-only or normalize-only transform. "
        "Use Adobe Audition's voice repair workflow: "
        "1) make sure the waveform/editor for the input clip is active; "
        "2) prefer Essential Sound > Dialogue > Repair, then apply Reduce Noise, Reduce Rumble when there is low-frequency rumble, DeHum when there is 50/60 Hz hum, and DeEss for harsh sibilance; "
        "3) if Essential Sound is unavailable, use Effects > Noise Reduction/Restoration > DeNoise or Adaptive Noise Reduction, then a DeEsser; "
        f"4) try Match Loudness around {TARGET_LUFS:g} LUFS and keep peaks below -1 dBFS, but do not spend more than two attempts on this panel; the backend can safely finalize loudness after export. "
        "Avoid merely increasing or decreasing volume; the result must reduce noise floor and 4-8 kHz sibilance when present. "
        "Do not use Terminal. The app/file should already be open, so continue in Audition. "
        f"Save/export the cleaned result as WAV base name cleaned (Audition may append .wav automatically). Preferred save folder: {preferred_save_folder}. "
        f"Fallback save folder: {short_export_dir}. Final server artifact will be {final_wav}. "
        f"The run folder is {run_dir}. For Save As: if the dialog already shows the correct folder or this run/audio_demos folder, keep that folder, set the filename field to cleaned, and click Save immediately. "
        f"If you must change folders, use Cmd+Shift+G, enter exactly {short_export_dir}, press Enter, then Save. "
        "The clipboard has the fallback folder path, but if paste inserts the wrong text, type the path manually once. "
        "Do not loop in the Save dialog; after two save attempts, finish with a short summary so the backend can inspect watched folders. "
    )


async def wait_for_export(candidates: list[Path], final_file: Path, timeout_seconds: float) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        for candidate in candidates:
            if candidate.is_file():
                if candidate.resolve() == final_file.resolve():
                    return True
                final_file.parent.mkdir(parents=True, exist_ok=True)
                if final_file.exists():
                    final_file.unlink()
                shutil.move(str(candidate), str(final_file))
                return True
        await asyncio.sleep(1.0)
    return False
