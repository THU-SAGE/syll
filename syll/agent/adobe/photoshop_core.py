"""Photoshop cutout agent-tool core.

Photoshop stays responsible for the creative cutout step while opening,
exporting, and verification remain deterministic in Python. These functions
are framework-free building blocks: they take plain arguments (paths, strings)
and return values, with no web/request coupling.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageOps

PHOTOSHOP_SKILL_NAME = "photoshop-cutout-syll"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


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


def detect_photoshop() -> list[str]:
    apps = []
    root = Path("/Applications")
    if root.exists():
        apps.extend(root.glob("Adobe Photoshop*.app"))
        apps.extend(root.glob("Adobe Photoshop*/Adobe Photoshop*.app"))
    return sorted({str(p) for p in apps})


def photoshop_app_name(app_path: str | None) -> str:
    return Path(app_path).stem if app_path else "Adobe Photoshop"


def _jsx_string(path: Path | str) -> str:
    return json.dumps(str(path))


def write_prepare_jsx(input_png: Path, work_psd: Path, script_path: Path) -> None:
    script = f"""
#target photoshop
app.displayDialogs = DialogModes.NO;
var inputFile = new File({_jsx_string(input_png)});
if (!inputFile.exists) throw new Error("Input file missing: " + inputFile.fsName);
var doc = app.open(inputFile);
app.activeDocument = doc;
try {{ doc.changeMode(ChangeMode.RGB); }} catch (e) {{}}
try {{ doc.bitsPerChannel = BitsPerChannelType.EIGHT; }} catch (e) {{}}
var original = doc.activeLayer;
try {{ original.isBackgroundLayer = false; }} catch (e) {{}}
original.name = "Original - hidden reference";
var work = original.duplicate();
work.name = "Syll cutout work - remove background here";
doc.activeLayer = work;
try {{ original.visible = false; }} catch (e) {{}}
var psdFile = new File({_jsx_string(work_psd)});
var psdOptions = new PhotoshopSaveOptions();
psdOptions.layers = true;
psdOptions.alphaChannels = true;
doc.saveAs(psdFile, psdOptions, true, Extension.LOWERCASE);
""".strip()
    script_path.write_text(script, encoding="utf-8")


def write_export_jsx(work_psd: Path, output_psd: Path, cutout_png: Path, script_path: Path) -> None:
    script = f"""
#target photoshop
app.displayDialogs = DialogModes.NO;
if (app.documents.length === 0) {{
  var workFile = new File({_jsx_string(work_psd)});
  if (!workFile.exists) throw new Error("Work PSD missing: " + workFile.fsName);
  app.open(workFile);
}}
var doc = app.activeDocument;
var psdFile = new File({_jsx_string(output_psd)});
var psdOptions = new PhotoshopSaveOptions();
psdOptions.layers = true;
psdOptions.alphaChannels = true;
doc.saveAs(psdFile, psdOptions, true, Extension.LOWERCASE);
var pngFile = new File({_jsx_string(cutout_png)});
var exportOptions = new ExportOptionsSaveForWeb();
exportOptions.format = SaveDocumentType.PNG;
exportOptions.PNG8 = false;
exportOptions.transparency = true;
exportOptions.interlaced = false;
exportOptions.quality = 100;
doc.exportDocument(pngFile, ExportType.SAVEFORWEB, exportOptions);
""".strip()
    script_path.write_text(script, encoding="utf-8")


async def run_photoshop_js(script_path: Path, app_path: str | None, timeout: float) -> str:
    app_name = photoshop_app_name(app_path)
    script_posix = str(script_path)
    lines = [
        f"set jsxPath to {json.dumps(script_posix)}",
        "set jsxFile to POSIX file jsxPath as alias",
        "set jsxSource to read jsxFile",
        f'tell application "{app_name}"',
        "activate",
        "do javascript jsxSource",
        "end tell",
    ]
    cmd = ["osascript"]
    for line in lines:
        cmd.extend(["-e", line])
    code, output = await run_quiet(*cmd, timeout=timeout)
    if code != 0:
        tail = " | ".join(output.splitlines()[-8:])
        raise RuntimeError(f"Photoshop bridge failed running {script_path.name}: {tail}")
    return output or f"ran {script_path.name}"


async def prepare_photoshop_document(
    *,
    input_png: Path,
    work_psd: Path,
    script_path: Path,
    app_path: str | None,
    timeout: float,
) -> str:
    write_prepare_jsx(input_png, work_psd, script_path)
    return await run_photoshop_js(script_path, app_path, timeout)


async def export_photoshop_outputs(
    *,
    work_psd: Path,
    output_psd: Path,
    cutout_png: Path,
    script_path: Path,
    app_path: str | None,
    timeout: float,
) -> str:
    write_export_jsx(work_psd, output_psd, cutout_png, script_path)
    return await run_photoshop_js(script_path, app_path, timeout)


def guess_image_suffix(filename: str | None, content_type: str | None = None) -> str:
    suffix = Path(filename or "input.png").suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return suffix
    ctype = (content_type or "").lower()
    if "jpeg" in ctype or "jpg" in ctype:
        return ".jpg"
    if "webp" in ctype:
        return ".webp"
    if "png" in ctype:
        return ".png"
    return suffix or ".bin"


def normalize_image_to_png(src: Path, dest: Path) -> dict[str, Any]:
    try:
        with Image.open(src) as img:
            img = ImageOps.exif_transpose(img)
            has_alpha = "A" in img.getbands() or "transparency" in img.info
            rgba = img.convert("RGBA")
            rgba.save(dest, "PNG")
            return {
                "width": rgba.width,
                "height": rgba.height,
                "mode": img.mode,
                "has_alpha": has_alpha,
                "pixels": rgba.width * rgba.height,
            }
    except Exception as exc:
        raise ValueError(f"unsupported or corrupt image: {exc}") from exc


def image_summary(path: Path) -> dict[str, Any]:
    try:
        with Image.open(path) as img:
            return {
                "exists": True,
                "width": img.width,
                "height": img.height,
                "mode": img.mode,
                "has_alpha": "A" in img.getbands() or "transparency" in img.info,
            }
    except Exception as exc:
        return {"exists": path.exists(), "error": str(exc)}


def create_previews(cutout_png: Path, checker_png: Path, white_png: Path) -> None:
    with Image.open(cutout_png) as img:
        rgba = img.convert("RGBA")
    white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    Image.alpha_composite(white, rgba).save(white_png, "PNG")

    checker = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    draw = ImageDraw.Draw(checker)
    size = max(12, min(rgba.size) // 24)
    for y in range(0, rgba.height, size):
        for x in range(0, rgba.width, size):
            if ((x // size) + (y // size)) % 2 == 0:
                draw.rectangle([x, y, x + size - 1, y + size - 1], fill=(220, 224, 230, 255))
    Image.alpha_composite(checker, rgba).save(checker_png, "PNG")


def verify_cutout(cutout_png: Path, checker_png: Path, white_png: Path) -> dict[str, Any]:
    if not cutout_png.exists():
        return {
            "success": False,
            "quality_label": "failed",
            "verdict": "Export failed: cutout.png was not created",
            "checks": [{"name": "cutout exists", "ok": False, "target": "output/cutout.png"}],
        }

    with Image.open(cutout_png) as opened:
        has_alpha = "A" in opened.getbands() or "transparency" in opened.info
        rgba = opened.convert("RGBA")
        alpha = rgba.getchannel("A")
        bbox = alpha.getbbox()
        hist = alpha.histogram()

    total = rgba.width * rgba.height
    transparent = sum(hist[:16])
    opaqueish = sum(hist[192:])
    subject = total - sum(hist[:32])
    transparent_ratio = transparent / total if total else 0.0
    opaque_ratio = opaqueish / total if total else 0.0
    subject_ratio = subject / total if total else 0.0
    bbox_ratio = 0.0
    center_offset_ratio = 1.0
    bbox_dict = None
    if bbox:
        left, top, right, bottom = bbox
        bbox_area = max(0, right - left) * max(0, bottom - top)
        bbox_ratio = bbox_area / total if total else 0.0
        cx = (left + right) / 2
        cy = (top + bottom) / 2
        center_offset_ratio = max(
            abs(cx - rgba.width / 2) / max(1, rgba.width),
            abs(cy - rgba.height / 2) / max(1, rgba.height),
        )
        bbox_dict = {"left": left, "top": top, "right": right, "bottom": bottom}

    checks = [
        {"name": "cutout exists", "ok": True, "target": "output/cutout.png"},
        {"name": "alpha channel", "ok": has_alpha, "target": "PNG with transparency"},
        {"name": "background removed", "ok": transparent_ratio >= 0.10, "target": ">= 10% transparent pixels"},
        {"name": "subject remains", "ok": 0.02 <= subject_ratio <= 0.90, "target": "2%-90% non-transparent subject"},
        {"name": "subject centered", "ok": center_offset_ratio <= 0.35, "target": "bbox center within 35% of canvas center"},
    ]
    success = all(c["ok"] for c in checks)
    create_previews(cutout_png, checker_png, white_png)
    verdict = "Syll cutout completed with transparent background" if success else "Cutout exported; review transparency checks"
    quality_label = "pass" if success else "review"
    return {
        "success": success,
        "quality_label": quality_label,
        "verdict": verdict,
        "width": rgba.width,
        "height": rgba.height,
        "has_alpha": has_alpha,
        "transparent_ratio": round(transparent_ratio, 4),
        "opaque_ratio": round(opaque_ratio, 4),
        "subject_ratio": round(subject_ratio, 4),
        "bbox_ratio": round(bbox_ratio, 4),
        "center_offset_ratio": round(center_offset_ratio, 4),
        "bbox": bbox_dict,
        "checks": checks,
    }


def render_cutout_report(metrics: dict[str, Any], files: dict[str, str], mode: str) -> str:
    status_icon = "PASS" if metrics.get("success") else "REVIEW"
    rows = [
        ("canvas", f"{metrics.get('width', 'n/a')}x{metrics.get('height', 'n/a')}"),
        ("alpha channel", "yes" if metrics.get("has_alpha") else "no"),
        ("transparent pixels", f"{metrics.get('transparent_ratio', 0) * 100:.1f}%"),
        ("subject pixels", f"{metrics.get('subject_ratio', 0) * 100:.1f}%"),
        ("bbox area", f"{metrics.get('bbox_ratio', 0) * 100:.1f}%"),
    ]
    table = "\n".join(f"| {k} | {v} |" for k, v in rows)
    checks = "\n".join(
        f"| {'OK' if c.get('ok') else 'FAIL'} {c.get('name')} | {c.get('target', '')} |"
        for c in metrics.get("checks", [])
    )
    outputs = "\n".join(f"- `{name}`: `{path}`" for name, path in files.items() if path)
    return f"""# Photoshop Syll Cutout

**Verdict**: {status_icon} {metrics.get('verdict', 'review needed')}

**Mode**: `{mode}`

## Metrics

| metric | value |
|---|---|
{table}

## Checks

| check | target |
|---|---|
{checks}

## Outputs

{outputs}
"""


def build_photoshop_instruction(*, run_id: str, input_png: Path, work_psd: Path, output_dir: Path) -> str:
    return (
        f"Photoshop Syll cutout run {run_id}. "
        f"The backend has opened/prepared this image in Photoshop: {input_png}. "
        f"The editable work PSD is {work_psd}. "
        "Goal: isolate the Syll ghost/toy mascot from the noisy dark background and leave the background transparent. "
        "Use Photoshop creative tools such as Select Subject, Object Selection, Remove Background, layer mask refinement, "
        "Select and Mask, or manual cleanup as needed. Keep the Syll character and tablet/surface intact; remove the dark/noisy background. "
        "Do not use Terminal. Do not Save As. Do not export files. Do not change output folders. "
        "When the visible document shows the isolated subject on transparency (checkerboard or masked layer), stop and finish with FINISHED_FOR_EXPORT. "
        f"The backend will export fixed outputs into {output_dir} and verify alpha transparency."
    )
