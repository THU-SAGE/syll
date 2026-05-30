"""Photoshop cutout verification tests for the Adobe conversational integration.

CI-safe and cross-platform: builds synthetic RGBA PNGs with Pillow and runs the
deterministic verify_cutout / create_previews helpers directly. No Photoshop app
and no GUI are involved.

Author: zhangbo <226653803@qq.com>
"""

from pathlib import Path

import pytest
from PIL import Image

from syll.agent.adobe.photoshop_core import (
    create_previews,
    normalize_image_to_png,
    verify_cutout,
)


def _centered_subject_png(path: Path, size: int = 240, subject: int = 96) -> None:
    """Opaque centered subject on a transparent background (a passing cutout)."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    left = (size - subject) // 2
    top = (size - subject) // 2
    block = Image.new("RGBA", (subject, subject), (200, 120, 60, 255))
    img.paste(block, (left, top))
    img.save(path, "PNG")


def _fully_opaque_png(path: Path, size: int = 240) -> None:
    """A solid opaque image with no transparency (a failing cutout)."""
    Image.new("RGBA", (size, size), (90, 90, 90, 255)).save(path, "PNG")


def test_verify_cutout_passes_for_centered_transparent_subject(tmp_path):
    cutout = tmp_path / "cutout.png"
    checker = tmp_path / "preview_checker.png"
    white = tmp_path / "preview_on_white.png"
    _centered_subject_png(cutout)

    metrics = verify_cutout(cutout, checker, white)

    assert metrics["success"] is True
    assert metrics["quality_label"] == "pass"
    assert metrics["has_alpha"] is True
    assert metrics["transparent_ratio"] >= 0.10
    assert all(c["ok"] for c in metrics["checks"])
    # The two before/after preview files must be written.
    assert checker.is_file()
    assert white.is_file()


def test_verify_cutout_fails_for_fully_opaque_image(tmp_path):
    cutout = tmp_path / "cutout.png"
    checker = tmp_path / "preview_checker.png"
    white = tmp_path / "preview_on_white.png"
    _fully_opaque_png(cutout)

    metrics = verify_cutout(cutout, checker, white)

    assert metrics["success"] is False
    assert metrics["quality_label"] == "review"
    assert metrics["transparent_ratio"] < 0.10
    # The "background removed" check must be the one that fails.
    bg = next(c for c in metrics["checks"] if c["name"] == "background removed")
    assert bg["ok"] is False


def test_verify_cutout_missing_file_fails_without_previews(tmp_path):
    cutout = tmp_path / "missing.png"
    checker = tmp_path / "preview_checker.png"
    white = tmp_path / "preview_on_white.png"

    metrics = verify_cutout(cutout, checker, white)

    assert metrics["success"] is False
    assert metrics["quality_label"] == "failed"
    assert not checker.exists()
    assert not white.exists()


def test_create_previews_writes_both_composites(tmp_path):
    cutout = tmp_path / "cutout.png"
    checker = tmp_path / "preview_checker.png"
    white = tmp_path / "preview_on_white.png"
    _centered_subject_png(cutout)

    create_previews(cutout, checker, white)

    assert checker.is_file()
    assert white.is_file()
    with Image.open(white) as img:
        # The white composite has no transparency in the background.
        assert img.convert("RGBA").getchannel("A").getextrema()[0] == 255


def test_normalize_image_to_png_round_trips_rgba(tmp_path):
    src = tmp_path / "src.png"
    dest = tmp_path / "dest.png"
    _centered_subject_png(src, size=120, subject=48)

    info = normalize_image_to_png(src, dest)

    assert dest.is_file()
    assert info["width"] == 120
    assert info["height"] == 120
    assert info["has_alpha"] is True


def test_normalize_image_to_png_rejects_corrupt_input(tmp_path):
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not really an image")
    with pytest.raises(ValueError):
        normalize_image_to_png(bad, tmp_path / "dest.png")
