"""Audio metric tests for the Adobe Audition conversational integration.

CI-safe and cross-platform: synthesizes PCM WAVs with numpy + wave and exercises
the deterministic metrics engine directly. No Adobe app and no GUI are involved.
The LUFS-dependent assertions are skipped when pyloudnorm is unavailable so the
core honesty contract still runs everywhere.

Author: zhangbo <226653803@qq.com>
"""

import math
import wave
from pathlib import Path

import numpy as np
import pytest

from syll.audio.metrics import (
    compare_audio,
    finalize_loudness,
    inspect_audio,
    render_report_markdown,
)


def _write_wav(path: Path, samples: np.ndarray, sr: int = 48000) -> None:
    """Write mono float samples in [-1, 1] as 16-bit PCM WAV."""
    samples = np.clip(samples, -1, 1)
    pcm = (samples * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def _noisy_then_repaired(sr: int = 48000):
    """Return (before, after) where `after` is a real noise repair, not a gain shift.

    The first 0.2s is a quiet-section noise burst that the repair knocks down,
    while the steady 440 Hz dialogue tone is left untouched.
    """
    t = np.arange(sr) / sr
    tone = np.zeros(sr, dtype=np.float32)
    tone[int(0.2 * sr):] = 0.08 * np.sin(2 * math.pi * 440 * t[int(0.2 * sr):])
    before = tone.copy()
    after = tone.copy()
    before[: int(0.2 * sr)] = 0.03 * np.sin(2 * math.pi * 1000 * t[: int(0.2 * sr)])
    after[: int(0.2 * sr)] = 0.001 * np.sin(2 * math.pi * 1000 * t[: int(0.2 * sr)])
    return before, after


def test_inspect_audio_reports_basic_and_sibilance_metrics(tmp_path):
    sr = 48000
    t = np.arange(sr) / sr
    samples = 0.1 * np.sin(2 * math.pi * 6000 * t)
    wav = tmp_path / "sib.wav"
    _write_wav(wav, samples, sr)

    metrics = inspect_audio(wav)

    assert metrics.exists is True
    assert metrics.duration_seconds == 1.0
    assert metrics.sample_rate == sr
    assert metrics.channels == 1
    assert metrics.peak_dbfs is not None
    assert metrics.sibilance_4_8khz_dbfs is not None


def test_inspect_audio_missing_file(tmp_path):
    metrics = inspect_audio(tmp_path / "nope.wav")
    assert metrics.exists is False
    assert "file missing" in metrics.warnings


def test_compare_audio_flags_real_repair_as_success(tmp_path):
    """A genuine noise repair should pass the success verdict (no LUFS needed)."""
    sr = 48000
    before, after = _noisy_then_repaired(sr)

    before_path = tmp_path / "before.wav"
    after_path = tmp_path / "after.wav"
    _write_wav(before_path, before, sr)
    _write_wav(after_path, after, sr)

    report = compare_audio(before_path, after_path)

    assert report.export_exists is True
    assert report.duration_ratio == 1.0
    assert report.noise_floor_delta_db is not None
    assert report.noise_floor_delta_db <= -6
    assert report.gain_only_transform is False
    assert report.success is True
    assert report.quality_label in {"excellent", "pass"}


def test_compare_audio_pure_gain_is_gain_only_honesty_contract(tmp_path):
    """after = before * constant must be reported as gain_only, never a repair."""
    sr = 48000
    t = np.arange(sr) / sr
    voice = 0.03 * np.sin(2 * math.pi * 440 * t)
    hiss = 0.01 * np.sin(2 * math.pi * 6000 * t)
    before_samples = voice + hiss
    after_samples = before_samples * 1.42  # pure-gain transform

    before_path = tmp_path / "before.wav"
    after_path = tmp_path / "after.wav"
    _write_wav(before_path, before_samples, sr)
    _write_wav(after_path, after_samples, sr)

    report = compare_audio(before_path, after_path)

    assert report.gain_only_transform is True
    assert report.quality_label == "gain_only"
    assert report.success is False


def test_relative_repair_metrics_survive_final_gain(tmp_path):
    """Scale-invariant repair proof survives a loudness finalizer (still not gain-only)."""
    sr = 48000
    t = np.arange(sr) / sr
    voice = np.zeros(sr, dtype=np.float32)
    voice[int(0.25 * sr):] = 0.08 * np.sin(2 * math.pi * 440 * t[int(0.25 * sr):])
    noise = np.zeros(sr, dtype=np.float32)
    noise[: int(0.2 * sr)] = 0.03 * np.sin(2 * math.pi * 1000 * t[: int(0.2 * sr)])
    sib = np.zeros(sr, dtype=np.float32)
    sib[int(0.25 * sr):] = 0.015 * np.sin(2 * math.pi * 6000 * t[int(0.25 * sr):])
    before = voice + noise + sib
    repaired_then_louder = voice + (noise * 0.2) + (sib * 0.55)
    repaired_then_louder *= 1.8

    before_path = tmp_path / "before.wav"
    after_path = tmp_path / "after.wav"
    _write_wav(before_path, before, sr)
    _write_wav(after_path, repaired_then_louder, sr)

    report = compare_audio(before_path, after_path)

    assert report.noise_to_rms_delta_db is not None
    assert report.noise_to_rms_delta_db <= -6
    assert report.sibilance_to_rms_delta_db is not None
    assert report.sibilance_to_rms_delta_db <= -1.5
    assert report.gain_only_transform is False


def test_finalize_loudness_moves_lufs_toward_target(tmp_path):
    pytest.importorskip("pyloudnorm")
    sr = 48000
    t = np.arange(sr) / sr
    samples = 0.03 * np.sin(2 * math.pi * 440 * t)
    src = tmp_path / "quiet.wav"
    _write_wav(src, samples, sr)

    info = finalize_loudness(src, src)
    metrics = inspect_audio(src)

    assert info["applied"] is True
    assert metrics.integrated_lufs is not None
    assert abs(metrics.integrated_lufs - -18.0) <= 1.5
    assert metrics.peak_dbfs <= -1.0


def test_report_markdown_includes_targets_and_checks(tmp_path):
    sr = 48000
    before, after = _noisy_then_repaired(sr)

    before_path = tmp_path / "before.wav"
    after_path = tmp_path / "after.wav"
    _write_wav(before_path, before, sr)
    _write_wav(after_path, after, sr)

    md = render_report_markdown(compare_audio(before_path, after_path))

    assert "target" in md
    assert "-18.0" in md
    assert "## Checks" in md
