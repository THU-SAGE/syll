"""Small audio metrics engine for the Audition demo.

The module intentionally avoids mandatory heavy DSP dependencies. It reads
PCM WAV files produced by the demo normalizer, uses numpy for simple numeric
work, and treats pyloudnorm as an optional enhancement for LUFS.
"""

from __future__ import annotations

import math
import statistics
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

_DB_FLOOR = -120.0
_TARGET_LUFS = -18.0
_TARGET_LUFS_TOLERANCE = 1.5
_TARGET_PEAK_DBFS = -1.0


@dataclass
class AudioMetrics:
    path: str
    exists: bool
    duration_seconds: float = 0.0
    sample_rate: int = 0
    channels: int = 0
    peak_dbfs: float | None = None
    rms_dbfs: float | None = None
    noise_floor_dbfs: float | None = None
    sibilance_4_8khz_dbfs: float | None = None
    integrated_lufs: float | None = None
    clipping_count: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AudioCompareReport:
    before: AudioMetrics
    after: AudioMetrics
    export_exists: bool
    duration_ratio: float | None
    noise_floor_delta_db: float | None
    sibilance_delta_db: float | None
    noise_to_rms_delta_db: float | None
    sibilance_to_rms_delta_db: float | None
    lufs_delta: float | None
    peak_delta_db: float | None
    rms_delta_db: float | None
    target_lufs: float
    target_lufs_tolerance: float
    target_peak_dbfs: float
    quality_label: str
    quality_summary: str
    gain_only_transform: bool
    checks: list[dict[str, Any]]
    verdict: str
    success: bool
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _dbfs(value: float) -> float:
    if value <= 0 or not math.isfinite(value):
        return _DB_FLOOR
    return max(_DB_FLOOR, 20.0 * math.log10(value))


def _round(value: float | None, ndigits: int = 3) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(float(value), ndigits)


def _read_wav_float(path: Path) -> tuple[np.ndarray, int, int]:
    """Read a PCM WAV into mono float32 samples in [-1, 1]."""
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        sample_rate = wav.getframerate()
        sampwidth = wav.getsampwidth()
        frames = wav.getnframes()
        raw = wav.readframes(frames)

    if frames == 0:
        return np.array([], dtype=np.float32), sample_rate, channels

    if sampwidth == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sampwidth == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif sampwidth == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        signed = (
            b[:, 0].astype(np.int32)
            | (b[:, 1].astype(np.int32) << 8)
            | (b[:, 2].astype(np.int32) << 16)
        )
        signed = np.where(signed & 0x800000, signed - 0x1000000, signed)
        data = signed.astype(np.float32) / 8388608.0
    elif sampwidth == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"Unsupported WAV sample width: {sampwidth}")

    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)

    return np.clip(data.astype(np.float32), -1.0, 1.0), sample_rate, channels


def _write_wav_float(path: Path, samples: np.ndarray, sample_rate: int) -> None:
    """Write mono float samples in [-1, 1] as 16-bit PCM WAV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    clipped = np.clip(samples, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm.tobytes())


def _window_noise_floor(samples: np.ndarray, sample_rate: int) -> float | None:
    """Estimate noise floor as the 5th percentile of 100 ms window RMS."""
    if samples.size == 0 or sample_rate <= 0:
        return None
    win = max(1, int(sample_rate * 0.1))
    if samples.size < win:
        return _dbfs(float(np.sqrt(np.mean(samples * samples))))
    usable = samples[: samples.size - (samples.size % win)]
    if usable.size == 0:
        return None
    windows = usable.reshape(-1, win)
    rms = np.sqrt(np.mean(windows * windows, axis=1))
    return _dbfs(float(np.percentile(rms, 5)))


def _sibilance_band_dbfs(samples: np.ndarray, sample_rate: int) -> float | None:
    if samples.size == 0 or sample_rate <= 8000:
        return None
    # Limit cost for long clips while keeping a stable whole-file estimate.
    max_samples = sample_rate * 120
    if samples.size > max_samples:
        samples = samples[:max_samples]
    n = int(samples.size)
    if n <= 1:
        return None
    windowed = samples * np.hanning(n)
    spectrum = np.fft.rfft(windowed)
    freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate)
    mask = (freqs >= 4000.0) & (freqs <= 8000.0)
    if not np.any(mask):
        return None
    filtered = np.zeros_like(spectrum)
    filtered[mask] = spectrum[mask]
    band = np.fft.irfft(filtered, n=n)
    return _dbfs(float(np.sqrt(np.mean(band * band))))


def _integrated_lufs(samples: np.ndarray, sample_rate: int, warnings: list[str]) -> float | None:
    try:
        import pyloudnorm as pyln  # type: ignore
    except Exception:
        warnings.append("pyloudnorm unavailable; LUFS was not computed")
        return None
    try:
        meter = pyln.Meter(sample_rate)
        return float(meter.integrated_loudness(samples.astype(float)))
    except Exception as exc:
        warnings.append(f"LUFS failed: {exc}")
        return None


def inspect_audio(path: str | Path) -> AudioMetrics:
    """Inspect a normalized WAV file and return demo-friendly metrics."""
    p = Path(path)
    if not p.is_file():
        return AudioMetrics(path=str(p), exists=False, warnings=["file missing"])

    warnings: list[str] = []
    try:
        samples, sample_rate, channels = _read_wav_float(p)
    except Exception as exc:
        return AudioMetrics(path=str(p), exists=True, warnings=[f"read failed: {exc}"])

    duration = float(samples.size / sample_rate) if sample_rate else 0.0
    if samples.size == 0:
        warnings.append("audio is empty")
        peak = rms = 0.0
    else:
        peak = float(np.max(np.abs(samples)))
        rms = float(np.sqrt(np.mean(samples * samples)))

    clipping_count = int(np.sum(np.abs(samples) >= 0.999)) if samples.size else 0
    lufs = _integrated_lufs(samples, sample_rate, warnings) if samples.size else None

    return AudioMetrics(
        path=str(p),
        exists=True,
        duration_seconds=_round(duration) or 0.0,
        sample_rate=sample_rate,
        channels=channels,
        peak_dbfs=_round(_dbfs(peak)),
        rms_dbfs=_round(_dbfs(rms)),
        noise_floor_dbfs=_round(_window_noise_floor(samples, sample_rate)),
        sibilance_4_8khz_dbfs=_round(_sibilance_band_dbfs(samples, sample_rate)),
        integrated_lufs=_round(lufs),
        clipping_count=clipping_count,
        warnings=warnings,
    )


def finalize_loudness(
    src_path: str | Path,
    dest_path: str | Path | None = None,
    *,
    target_lufs: float = _TARGET_LUFS,
    peak_ceiling_dbfs: float = _TARGET_PEAK_DBFS,
) -> dict[str, Any]:
    """Apply a deterministic gain pass to hit target LUFS without exceeding peak ceiling.

    This is intentionally a gain-only finalizer for the already repaired Audition
    export. Repair proof should be measured before or with scale-invariant metrics.
    """
    src = Path(src_path)
    dest = Path(dest_path) if dest_path is not None else src
    warnings: list[str] = []
    try:
        samples, sample_rate, _channels = _read_wav_float(src)
    except Exception as exc:
        return {"applied": False, "reason": f"read failed: {exc}", "warnings": warnings}
    if samples.size == 0:
        return {"applied": False, "reason": "audio is empty", "warnings": warnings}

    before_lufs = _integrated_lufs(samples, sample_rate, warnings)
    if before_lufs is None:
        return {"applied": False, "reason": "LUFS unavailable", "warnings": warnings}

    peak = float(np.max(np.abs(samples)))
    peak_dbfs = _dbfs(peak)
    desired_gain_db = target_lufs - before_lufs
    max_gain_db = peak_ceiling_dbfs - peak_dbfs
    applied_gain_db = min(desired_gain_db, max_gain_db)
    limited_by_peak = applied_gain_db < desired_gain_db - 0.05

    if abs(applied_gain_db) < 0.05:
        return {
            "applied": False,
            "reason": "already within gain tolerance",
            "before_lufs": _round(before_lufs),
            "gain_db": _round(applied_gain_db),
            "warnings": warnings,
        }

    scaled = samples * (10.0 ** (applied_gain_db / 20.0))
    tmp = dest.with_name(dest.name + ".tmp")
    _write_wav_float(tmp, scaled, sample_rate)
    tmp.replace(dest)
    after = inspect_audio(dest)
    return {
        "applied": True,
        "before_lufs": _round(before_lufs),
        "after_lufs": after.integrated_lufs,
        "gain_db": _round(applied_gain_db),
        "desired_gain_db": _round(desired_gain_db),
        "limited_by_peak": limited_by_peak,
        "target_lufs": target_lufs,
        "peak_ceiling_dbfs": peak_ceiling_dbfs,
        "warnings": warnings + after.warnings,
    }


def _delta(after: float | None, before: float | None) -> float | None:
    if after is None or before is None:
        return None
    return _round(after - before)


def _relative_delta(
    after_value: float | None,
    after_anchor: float | None,
    before_value: float | None,
    before_anchor: float | None,
) -> float | None:
    if None in (after_value, after_anchor, before_value, before_anchor):
        return None
    return _round((after_value - after_anchor) - (before_value - before_anchor))  # type: ignore[operator]


def _check(name: str, ok: bool, detail: str, target: str) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": detail, "target": target}


def _gain_only_transform(
    *,
    duration_ratio: float | None,
    noise_delta: float | None,
    sibilance_delta: float | None,
    peak_delta: float | None,
    rms_delta: float | None,
    lufs_delta: float | None,
) -> bool:
    if duration_ratio is None or not 0.98 <= duration_ratio <= 1.02:
        return False
    deltas = [
        d
        for d in (noise_delta, sibilance_delta, peak_delta, rms_delta, lufs_delta)
        if d is not None and math.isfinite(d)
    ]
    if len(deltas) < 3:
        return False
    median = statistics.median(deltas)
    if abs(median) < 1.0:
        return False
    same_direction = all((d >= 0) == (median >= 0) for d in deltas)
    tightly_clustered = max(abs(d - median) for d in deltas) <= 1.0
    return same_direction and tightly_clustered


def _quality_label(
    *,
    export_exists: bool,
    duration_ok: bool,
    noise_ok: bool,
    sibilance_ok: bool,
    lufs_ok: bool | None,
    peak_ok: bool,
    gain_only: bool,
) -> tuple[str, str]:
    if not export_exists:
        return "failed", "Export did not land in a watched folder"
    if gain_only:
        return "gain_only", "Gain-only transform; repair not proven"
    if not duration_ok:
        return "failed", "Duration changed too much; review the export"

    core_ok = noise_ok and sibilance_ok and peak_ok
    loudness_ok = True if lufs_ok is None else lufs_ok
    if core_ok and loudness_ok:
        return "excellent", "Noise, sibilance, loudness, and peak checks passed"
    if noise_ok and peak_ok and loudness_ok:
        return "pass", "Noise repair passed; sibilance needs a quick listen"
    if noise_ok or sibilance_ok:
        return "partial", "Some repair happened, but the demo is not fully convincing"
    if loudness_ok and peak_ok:
        return "gain_only", "Loudness changed, but repair was not measured"
    return "failed", "Repair was not measurable"


def compare_audio(before_path: str | Path, after_path: str | Path) -> AudioCompareReport:
    before = inspect_audio(before_path)
    after = inspect_audio(after_path)
    warnings = list(before.warnings) + list(after.warnings)
    export_exists = after.exists

    duration_ratio: float | None = None
    if before.duration_seconds > 0 and after.duration_seconds > 0:
        duration_ratio = _round(after.duration_seconds / before.duration_seconds)

    noise_delta = _delta(after.noise_floor_dbfs, before.noise_floor_dbfs)
    sibilance_delta = _delta(after.sibilance_4_8khz_dbfs, before.sibilance_4_8khz_dbfs)
    noise_rel_delta = _relative_delta(
        after.noise_floor_dbfs,
        after.rms_dbfs,
        before.noise_floor_dbfs,
        before.rms_dbfs,
    )
    sibilance_rel_delta = _relative_delta(
        after.sibilance_4_8khz_dbfs,
        after.rms_dbfs,
        before.sibilance_4_8khz_dbfs,
        before.rms_dbfs,
    )
    lufs_delta = _delta(after.integrated_lufs, before.integrated_lufs)
    peak_delta = _delta(after.peak_dbfs, before.peak_dbfs)
    rms_delta = _delta(after.rms_dbfs, before.rms_dbfs)

    duration_ok = duration_ratio is not None and 0.95 <= duration_ratio <= 1.05
    noise_ok = (
        (noise_delta is not None and noise_delta <= -3.0)
        or (noise_rel_delta is not None and noise_rel_delta <= -6.0)
    )
    # Scale-invariant high-band reduction survives the loudness finalizer.
    sibilance_ok = (
        (sibilance_delta is not None and sibilance_delta <= 0.0)
        or (sibilance_rel_delta is not None and sibilance_rel_delta <= -1.5)
        or (after.sibilance_4_8khz_dbfs is not None and after.sibilance_4_8khz_dbfs <= -60.0)
    )
    peak_ok = after.peak_dbfs is not None and after.peak_dbfs <= _TARGET_PEAK_DBFS and after.clipping_count == 0
    lufs_ok: bool | None
    if after.integrated_lufs is None:
        lufs_ok = None
        warnings.append("LUFS success check skipped")
    else:
        lufs_ok = abs(after.integrated_lufs - _TARGET_LUFS) <= _TARGET_LUFS_TOLERANCE

    gain_only = _gain_only_transform(
        duration_ratio=duration_ratio,
        noise_delta=noise_delta,
        sibilance_delta=sibilance_delta,
        peak_delta=peak_delta,
        rms_delta=rms_delta,
        lufs_delta=lufs_delta,
    )
    quality_label, quality_summary = _quality_label(
        export_exists=export_exists,
        duration_ok=duration_ok,
        noise_ok=noise_ok,
        sibilance_ok=sibilance_ok,
        lufs_ok=lufs_ok,
        peak_ok=peak_ok,
        gain_only=gain_only,
    )

    checks = [
        _check("Export", export_exists, "cleaned.wav was found", "file exists"),
        _check("Duration", duration_ok, f"ratio={duration_ratio}", "0.95-1.05"),
        _check("Noise floor", noise_ok, f"delta={noise_delta} dB, relative={noise_rel_delta} dB", "<= -3 dB absolute or <= -6 dB relative-to-RMS"),
        _check("De-ess 4-8 kHz", sibilance_ok, f"delta={sibilance_delta} dB, relative={sibilance_rel_delta} dB", "<= 0 dB absolute, <= -1.5 dB relative-to-RMS, or after <= -60 dBFS"),
        _check("Peak safety", peak_ok, f"peak={after.peak_dbfs} dBFS, clips={after.clipping_count}", "<= -1 dBFS and 0 clips"),
    ]
    if lufs_ok is None:
        checks.append(_check("Podcast loudness", True, "LUFS skipped", f"{_TARGET_LUFS} +/- {_TARGET_LUFS_TOLERANCE} LUFS"))
    else:
        checks.append(_check("Podcast loudness", lufs_ok, f"after={after.integrated_lufs} LUFS", f"{_TARGET_LUFS} +/- {_TARGET_LUFS_TOLERANCE} LUFS"))
    checks.append(_check("Not gain-only", not gain_only, "same-gain deltas detected" if gain_only else "deltas are content-specific", "repair changes differ from pure volume"))

    success = quality_label in {"excellent", "pass"}
    if success:
        verdict = "Cleaned successfully"
    elif quality_label == "gain_only":
        verdict = "Gain-only transform; repair not proven"
    elif export_exists:
        verdict = "Partial cleanup; review metrics"
    else:
        verdict = "Export failed"

    return AudioCompareReport(
        before=before,
        after=after,
        export_exists=export_exists,
        duration_ratio=duration_ratio,
        noise_floor_delta_db=noise_delta,
        sibilance_delta_db=sibilance_delta,
        noise_to_rms_delta_db=noise_rel_delta,
        sibilance_to_rms_delta_db=sibilance_rel_delta,
        lufs_delta=lufs_delta,
        peak_delta_db=peak_delta,
        rms_delta_db=rms_delta,
        target_lufs=_TARGET_LUFS,
        target_lufs_tolerance=_TARGET_LUFS_TOLERANCE,
        target_peak_dbfs=_TARGET_PEAK_DBFS,
        quality_label=quality_label,
        quality_summary=quality_summary,
        gain_only_transform=gain_only,
        checks=checks,
        verdict=verdict,
        success=success,
        warnings=warnings,
    )


def render_report_markdown(report: AudioCompareReport) -> str:
    """Render a fixed report that is stable for demos and tests."""
    before = report.before
    after = report.after

    def fmt(v: Any, suffix: str = "") -> str:
        if v is None:
            return "n/a"
        return f"{v}{suffix}"

    status = "PASS" if report.success else "REVIEW"
    rows = [
        ("LUFS", before.integrated_lufs, after.integrated_lufs, report.lufs_delta, f"{report.target_lufs} +/- {report.target_lufs_tolerance}"),
        ("noise floor dBFS", before.noise_floor_dbfs, after.noise_floor_dbfs, report.noise_floor_delta_db, "<= -6 dB delta"),
        ("noise floor vs RMS", None, None, report.noise_to_rms_delta_db, "<= -6 dB relative delta"),
        ("sibilance 4-8kHz dBFS", before.sibilance_4_8khz_dbfs, after.sibilance_4_8khz_dbfs, report.sibilance_delta_db, "<= -3 dB delta"),
        ("sibilance vs RMS", None, None, report.sibilance_to_rms_delta_db, "<= -1.5 dB relative delta"),
        ("peak dBFS", before.peak_dbfs, after.peak_dbfs, report.peak_delta_db, "<= -1 dBFS"),
        ("RMS dBFS", before.rms_dbfs, after.rms_dbfs, report.rms_delta_db, "content-specific"),
        ("duration ratio", 1.0, report.duration_ratio, None, "0.95-1.05"),
    ]
    table = ["| metric | before | after | delta | target |", "|---|---:|---:|---:|---|"]
    for name, b, a, d, target in rows:
        table.append(f"| {name} | {fmt(b)} | {fmt(a)} | {fmt(d)} | {target} |")

    checks = "\n".join(
        f"- {'OK' if c['ok'] else 'FAIL'} {c['name']}: {c['detail']} (target: {c['target']})"
        for c in report.checks
    )
    warning_text = "\n".join(f"- {w}" for w in report.warnings) or "- none"
    return "\n".join([
        "# Audition Audio Demo Report",
        "",
        f"**Verdict**: {status} - {report.verdict}",
        f"**Quality**: {report.quality_label} - {report.quality_summary}",
        "",
        *table,
        "",
        "## Checks",
        "",
        checks,
        "",
        "## What Syll Tried",
        "",
        "Syll opened Adobe Audition, prioritized dialogue repair (noise, rumble, hum, de-ess), exported `cleaned.wav`, and verified the result with deterministic audio metrics.",
        "",
        "## Warnings",
        "",
        warning_text,
        "",
    ])
