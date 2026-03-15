#!/usr/bin/env python3
"""Render a Notein export bundle directly to a PDF.

This script takes either:
  - a Notein export zip, or
  - an already-unpacked Notein export directory

It will:
    1) unpack the source if needed,
    2) read the Notein SQLite database,
    3) render each page from strokes, shapes, text boxes, and images,
    4) save all pages into one PDF.

Example:
    python notein_extract.py /path/to/note_export.zip -o out.pdf

Tested against a bundle that contained files such as:
    note_database_note_<uuid>_db
    note_meta.json
    note_image_<uuid>.png
    note_image_<uuid>.svg
"""
from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import sqlite3
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception as exc:  # pragma: no cover
    raise SystemExit(f"Pillow is required: {exc}")

try:
    import cairosvg  # type: ignore
except Exception:
    cairosvg = None

try:
    from bs4 import BeautifulSoup  # type: ignore
except Exception:
    BeautifulSoup = None


@dataclass
class PageInfo:
    page_id: str
    index: int
    width: int
    height: int
    background_rgba: tuple[int, int, int, int]
    background_style: str
    page_row: sqlite3.Row


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render a Notein export bundle to a single PDF."
    )
    parser.add_argument("input_path", help="Path to a Notein zip bundle or extracted directory")
    parser.add_argument(
        "-o",
        "--output",
        help="Output PDF path (default: <input>.pdf)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    input_path = Path(args.input_path).expanduser().resolve()
    if not input_path.exists():
        raise SystemExit(f"Input path does not exist: {input_path}")

    if args.output:
        output_pdf = Path(args.output).expanduser().resolve()
    else:
        base_name = input_path.name if input_path.is_dir() else input_path.stem
        output_pdf = input_path.parent / f"{base_name}.pdf"

    output_pdf.parent.mkdir(parents=True, exist_ok=True)

    with prepare_source_context(input_path) as source_dir:
        db_path = find_database(source_dir)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            page_images = extract_note(
                conn=conn,
                source_dir=source_dir,
            )
        finally:
            conn.close()

    write_pdf(output_pdf, page_images)

    print(f"Wrote PDF: {output_pdf}")
    print(f"Pages: {len(page_images)}")
    return 0


@dataclass
class SourceContext:
    source_dir: Path
    temp_dir: Optional[tempfile.TemporaryDirectory[str]] = None

    def __enter__(self) -> Path:
        return self.source_dir

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.temp_dir is not None:
            self.temp_dir.cleanup()


def prepare_source_context(input_path: Path) -> SourceContext:
    if input_path.is_dir():
        return SourceContext(source_dir=input_path)

    if not zipfile.is_zipfile(input_path):
        raise SystemExit(
            "Input is neither a directory nor a valid zip file. "
            f"Got: {input_path}"
        )

    temp_dir = tempfile.TemporaryDirectory(prefix="notein_extract_")
    work_dir = Path(temp_dir.name)
    with zipfile.ZipFile(input_path, "r") as zf:
        zf.extractall(work_dir)
    return SourceContext(source_dir=work_dir, temp_dir=temp_dir)


def find_database(source_dir: Path) -> Path:
    candidates = sorted(
        p
        for p in source_dir.glob("note_database*_db*")
        if p.is_file() and not p.name.endswith(("-wal", "-shm"))
    )
    if not candidates:
        raise SystemExit(f"Could not find Notein database in: {source_dir}")
    return candidates[0]


def extract_note(
    conn: sqlite3.Connection,
    source_dir: Path,
) -> list[Image.Image]:
    cur = conn.cursor()
    note_row = cur.execute("SELECT * FROM NoteContentEntity LIMIT 1").fetchone()
    if note_row is None:
        raise SystemExit("The Notein database does not contain NoteContentEntity rows.")

    page_order = parse_json_list(note_row["page_list"])
    layer_order = parse_json_list(note_row["page_layer_list"])
    layer_index = {layer_id: i for i, layer_id in enumerate(layer_order)}

    pages_by_id = {
        row["id"]: row
        for row in cur.execute("SELECT * FROM PageEntity").fetchall()
    }

    pages: list[PageInfo] = []
    for idx, page_id in enumerate(page_order, start=1):
        row = pages_by_id.get(page_id)
        if row is None:
            continue
        pages.append(build_page_info(row, idx))

    if not pages:
        raise SystemExit("No pages were found in the Notein page list.")

    page_entities = {
        page.page_id: gather_page_entities(cur, page.page_id) for page in pages
    }

    page_images: list[Image.Image] = []

    for page in pages:
        entities = page_entities[page.page_id]
        image = render_page(
            page=page,
            entities=entities,
            source_dir=source_dir,
            layer_index=layer_index,
        )
        page_images.append(image)

    return page_images


def parse_json_list(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
        if isinstance(value, list):
            return [str(x) for x in value]
    except Exception:
        pass
    return []


def build_page_info(row: sqlite3.Row, index: int) -> PageInfo:
    paper_spec = parse_json_object(row["paper_spec"])
    paper_theme = parse_json_object(row["paper_theme"])
    background_rgba = (255, 255, 255, 255)
    background_style = "blank"

    try:
        base_theme = paper_theme.get("baseTheme", {})
        color = base_theme.get("color")
        if color is not None:
            background_rgba = android_color_to_rgba(int(color))
    except Exception:
        pass

    try:
        paper_style = paper_theme.get("paperStyle", {})
        style_type = str(paper_style.get("type", ""))
        if style_type:
            background_style = style_type
    except Exception:
        pass

    width = int(round(float(paper_spec.get("width", 1240))))
    height = int(round(float(paper_spec.get("height", 1754))))
    return PageInfo(
        page_id=row["id"],
        index=index,
        width=width,
        height=height,
        background_rgba=background_rgba,
        background_style=background_style,
        page_row=row,
    )


def gather_page_entities(cur: sqlite3.Cursor, page_id: str) -> dict[str, list[sqlite3.Row]]:
    return {
        "strokes": cur.execute(
            "SELECT * FROM StrokeEntity WHERE page_id=? ORDER BY creation_time, id", (page_id,)
        ).fetchall(),
        "shapes": cur.execute(
            "SELECT * FROM ShapeEntity WHERE page_id=? ORDER BY creation_time, id", (page_id,)
        ).fetchall(),
        "textboxes": cur.execute(
            "SELECT * FROM TextBoxEntity WHERE page_id=? ORDER BY creation_time, id", (page_id,)
        ).fetchall(),
        "images": cur.execute(
            "SELECT * FROM ImageEntity WHERE page_id=? ORDER BY creation_time, id", (page_id,)
        ).fetchall(),
    }


def parse_json_object(raw: Optional[str]) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        if isinstance(value, dict):
            return value
    except Exception:
        pass
    return {}


def android_color_to_rgba(value: int, alpha_override: Optional[int] = None) -> tuple[int, int, int, int]:
    value &= 0xFFFFFFFF
    a = (value >> 24) & 0xFF
    r = (value >> 16) & 0xFF
    g = (value >> 8) & 0xFF
    b = value & 0xFF
    if alpha_override is not None:
        a = max(0, min(255, alpha_override))
    return (r, g, b, a)


def notein_long_color_to_rgba(value: int) -> tuple[int, int, int, int]:
    value &= 0xFFFFFFFFFFFFFFFF
    high = (value >> 32) & 0xFFFFFFFF
    low = value & 0xFFFFFFFF
    if high:
        return android_color_to_rgba(high)
    return android_color_to_rgba(low)


def html_to_plain_text(raw: Optional[str]) -> str:
    if not raw:
        return ""
    text = raw
    if BeautifulSoup is not None:
        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup.find_all("br"):
            tag.replace_with("\n")
        for tag_name in ("style", "script"):
            for tag in soup.find_all(tag_name):
                tag.decompose()
        text = soup.get_text("\n")
    else:
        text = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
        text = re.sub(r"<style.*?</style>", "", text, flags=re.I | re.S)
        text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = text.replace("\ufeff", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def render_page(
    page: PageInfo,
    entities: dict[str, list[sqlite3.Row]],
    source_dir: Path,
    layer_index: dict[str, int],
) -> Image.Image:
    image = Image.new("RGBA", (page.width, page.height), page.background_rgba)
    draw = ImageDraw.Draw(image, "RGBA")

    draw_page_background_pattern(draw, page)

    items: list[tuple[int, int, str, sqlite3.Row]] = []
    for kind in ("shapes", "images", "textboxes", "strokes"):
        for row in entities[kind]:
            row_layer = row["layer_id"] if "layer_id" in row.keys() else None
            items.append((layer_index.get(row_layer, 999), int(row["creation_time"]), kind, row))
    items.sort(key=lambda item: (item[0], item[1], item[2]))

    for _, _, kind, row in items:
        if kind == "shapes":
            draw_shape(draw, row)
        elif kind == "images":
            draw_image_entity(image, row, source_dir)
        elif kind == "textboxes":
            draw_textbox(draw, row)
        elif kind == "strokes":
            draw_stroke(draw, row)

    return image


def draw_page_background_pattern(draw: ImageDraw.ImageDraw, page: PageInfo) -> None:
    theme = parse_json_object(page.page_row["paper_theme"])
    paper_style = theme.get("paperStyle", {}) if isinstance(theme, dict) else {}
    style_type = str(paper_style.get("type", ""))
    spacing = float(paper_style.get("requiredItemSpace", 0) or 0)
    left_pad = float(paper_style.get("leftPadding", 0) or 0)
    top_pad = float(paper_style.get("topPadding", 0) or 0)

    if "Square" in style_type and spacing >= 10:
        line = (215, 215, 215, 70)
        x = left_pad
        while x < page.width:
            draw.line([(x, 0), (x, page.height)], fill=line, width=1)
            x += spacing
        y = top_pad
        while y < page.height:
            draw.line([(0, y), (page.width, y)], fill=line, width=1)
            y += spacing
    elif any(token in style_type for token in ("Line", "Ruled", "Horizontal")) and spacing >= 10:
        line = (215, 215, 215, 70)
        y = top_pad
        while y < page.height:
            draw.line([(0, y), (page.width, y)], fill=line, width=1)
            y += spacing


def draw_shape(draw: ImageDraw.ImageDraw, row: sqlite3.Row) -> None:
    points_raw = row["points"]
    try:
        pts = [(float(p["x"]), float(p["y"])) for p in json.loads(points_raw)]
    except Exception:
        return
    if not pts:
        return

    color = android_color_to_rgba(int(row["color"]))
    width = max(1, int(round(float(row["width"] or 1))))
    shape_type = int(row["type"])

    if shape_type in {2, 8, 17} and len(pts) >= 3:
        draw.line(pts + [pts[0]], fill=color, width=width)
        return

    if shape_type == 21 and len(pts) >= 2:
        (x1, y1), (x2, y2) = pts[0], pts[1]
        draw.ellipse([min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)], outline=color, width=width)
        return

    if shape_type == 19 and len(pts) >= 2:
        start, end = pts[0], pts[-1]
        draw_arrow(draw, start, end, color, width)
        return

    if len(pts) == 1:
        r = max(1.0, width / 2.0)
        x, y = pts[0]
        draw.ellipse((x - r, y - r, x + r, y + r), fill=color)
        return

    draw.line(pts, fill=color, width=width)


def draw_arrow(
    draw: ImageDraw.ImageDraw,
    start: tuple[float, float],
    end: tuple[float, float],
    color: tuple[int, int, int, int],
    width: int,
) -> None:
    draw.line([start, end], fill=color, width=width)
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length = math.hypot(dx, dy)
    if length == 0:
        return
    ux, uy = dx / length, dy / length
    head_len = max(8.0, width * 4.0)
    head_w = max(5.0, width * 2.0)
    left = (
        end[0] - head_len * ux + head_w * uy,
        end[1] - head_len * uy - head_w * ux,
    )
    right = (
        end[0] - head_len * ux - head_w * uy,
        end[1] - head_len * uy + head_w * ux,
    )
    draw.polygon([end, left, right], fill=color)


def draw_stroke(draw: ImageDraw.ImageDraw, row: sqlite3.Row) -> None:
    if row["record_json"]:
        draw_legacy_stroke(draw, row)
    elif row["ink_stroke_json"]:
        draw_ink_stroke(draw, row)


def draw_legacy_stroke(draw: ImageDraw.ImageDraw, row: sqlite3.Row) -> None:
    try:
        payload = json.loads(row["record_json"])
    except Exception:
        return

    points = []
    for p in payload.get("points", []):
        try:
            points.append((float(p["x"]), float(p["y"])))
        except Exception:
            continue
    points = dedupe_points(points)
    if not points:
        return

    stroke_type = int(payload.get("type", 1) or 1)
    width = max(1.0, float(payload.get("width", 1.0) or 1.0))
    color = android_color_to_rgba(int(payload.get("color", -16777216)))

    if stroke_type in {2, 11} or width >= 10:
        color = (color[0], color[1], color[2], min(color[3], 170))

    draw_polyline(draw, points, color, width)


def draw_ink_stroke(draw: ImageDraw.ImageDraw, row: sqlite3.Row) -> None:
    try:
        payload = json.loads(row["ink_stroke_json"])
        stroke = payload["stroke"]
        inputs = stroke["inputs"]["inputs"]
        brush = stroke.get("brush", {})
        stw = parse_matrix(payload.get("strokeToWorldTransform"))
        wtv = parse_matrix(payload.get("worldToViewTransform"))
    except Exception:
        return

    points = []
    for p in inputs:
        try:
            x1, y1 = apply_matrix(stw, float(p["x"]), float(p["y"]))
            x2, y2 = apply_matrix(wtv, x1, y1)
            points.append((x2, y2))
        except Exception:
            continue
    points = dedupe_points(points)
    if not points:
        return

    color_value = int(brush.get("color", -16777216))
    color = notein_long_color_to_rgba(color_value)
    width = float(brush.get("size", 1.0) or 1.0)
    scale = 1.0
    if stw and wtv:
        scale = abs(float(stw[0])) * abs(float(wtv[0]))
        if not math.isfinite(scale) or scale <= 0:
            scale = 1.0
    width = max(1.0, width * scale)
    draw_polyline(draw, points, color, width)


def dedupe_points(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not points:
        return []
    out = [points[0]]
    for p in points[1:]:
        if abs(p[0] - out[-1][0]) > 1e-6 or abs(p[1] - out[-1][1]) > 1e-6:
            out.append(p)
    return out


def draw_polyline(
    draw: ImageDraw.ImageDraw,
    points: list[tuple[float, float]],
    color: tuple[int, int, int, int],
    width: float,
) -> None:
    width_px = max(1, int(round(width)))
    if len(points) == 1:
        r = max(1.0, width / 2.0)
        x, y = points[0]
        draw.ellipse((x - r, y - r, x + r, y + r), fill=color)
        return

    draw.line(points, fill=color, width=width_px, joint="curve")
    r = max(1.0, width / 2.0)
    for x, y in (points[0], points[-1]):
        draw.ellipse((x - r, y - r, x + r, y + r), fill=color)


def parse_matrix(raw: Optional[str]) -> list[float]:
    if not raw:
        return [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
    raw = raw.strip().strip('"')
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) != 9:
        return [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
    return [float(p) for p in parts]


def apply_matrix(matrix: list[float], x: float, y: float) -> tuple[float, float]:
    return (
        matrix[0] * x + matrix[1] * y + matrix[2],
        matrix[3] * x + matrix[4] * y + matrix[5],
    )


def draw_textbox(draw: ImageDraw.ImageDraw, row: sqlite3.Row) -> None:
    text = html_to_plain_text(row["text"])
    if not text:
        return

    x = float(row["left"] or 0)
    y = float(row["top"] or 0)
    text_size = max(10, int(round(float(row["text_size"] or 20))))
    line_height = float(row["line_height"] or text_size)
    line_spacing = max(0, int(round(line_height - text_size)))

    try:
        color = android_color_to_rgba(int(row["default_text_color"]))
    except Exception:
        color = (0, 0, 0, 255)

    font = load_font(text_size)
    draw.multiline_text((x, y), text, fill=color, font=font, spacing=line_spacing)


def load_font(size: int) -> ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "/Library/Fonts/Arial.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            try:
                return ImageFont.truetype(candidate, size=size)
            except Exception:
                continue
    return ImageFont.load_default()


def draw_image_entity(canvas: Image.Image, row: sqlite3.Row, source_dir: Path) -> None:
    uri = row["uri"] or ""
    basename = os.path.basename(uri)
    asset_path = resolve_asset_path(source_dir, basename)
    if asset_path is None:
        return

    left = float(row["left"] or 0)
    top = float(row["top"] or 0)
    right = float(row["right"] or left)
    bottom = float(row["bottom"] or top)
    box_width = max(1, int(round(abs(right - left))))
    box_height = max(1, int(round(abs(bottom - top))))
    rotation = float(row["rotation"] or 0.0)

    # If bounds store the final post-rotation box, estimate the pre-rotation size
    # so rotating does not clip the image into partial halves.
    place_width, place_height = estimate_unrotated_size_for_bounds(
        box_width,
        box_height,
        rotation,
    )

    rendered = load_asset_image(asset_path, target_size=(place_width, place_height))
    if rendered is None:
        return

    placed = rendered.resize((place_width, place_height), Image.LANCZOS)
    if abs(rotation) > 1e-3:
        placed = placed.rotate(-rotation, expand=True, resample=Image.BICUBIC)
        cx = (left + right) / 2.0
        cy = (top + bottom) / 2.0
        paste_x = int(round(cx - placed.width / 2.0))
        paste_y = int(round(cy - placed.height / 2.0))
    else:
        paste_x = int(round(left))
        paste_y = int(round(top))

    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    overlay.alpha_composite(placed, (paste_x, paste_y))
    result = Image.alpha_composite(canvas, overlay)
    canvas.paste(result)


def estimate_unrotated_size_for_bounds(
    bounds_width: int,
    bounds_height: int,
    rotation_degrees: float,
) -> tuple[int, int]:
    if abs(rotation_degrees) <= 1e-3:
        return bounds_width, bounds_height

    theta = math.radians(abs(rotation_degrees))
    c = abs(math.cos(theta))
    s = abs(math.sin(theta))

    det = c * c - s * s
    if abs(det) < 1e-6:
        # Near 45/135 degrees: inversion is unstable; keep conservative fallback.
        return bounds_width, bounds_height

    w = (bounds_width * c - bounds_height * s) / det
    h = (bounds_height * c - bounds_width * s) / det

    if not math.isfinite(w) or not math.isfinite(h) or w <= 1 or h <= 1:
        return bounds_width, bounds_height

    return max(1, int(round(w))), max(1, int(round(h)))


def resolve_asset_path(source_dir: Path, basename: str) -> Optional[Path]:
    if not basename:
        return None
    direct = source_dir / basename
    if direct.exists():
        return direct

    prefixed = source_dir / f"note_image_{basename}"
    if prefixed.exists():
        return prefixed

    stem = Path(basename).stem
    matches = sorted(source_dir.glob(f"note_image_{stem}.*"))
    if matches:
        return matches[0]
    return None


def load_asset_image(path: Path, target_size: Optional[tuple[int, int]] = None) -> Optional[Image.Image]:
    suffix = path.suffix.lower()
    try:
        if suffix == ".svg":
            if cairosvg is None:
                return None
            kwargs: dict[str, Any] = {}
            if target_size is not None:
                width, height = target_size
                kwargs["output_width"] = max(1, int(width))
                kwargs["output_height"] = max(1, int(height))
            png_bytes = cairosvg.svg2png(url=str(path), **kwargs)
            from io import BytesIO

            return Image.open(BytesIO(png_bytes)).convert("RGBA")
        return Image.open(path).convert("RGBA")
    except Exception:
        return None


def write_pdf(output_pdf: Path, pages: list[Image.Image]) -> None:
    if not pages:
        raise SystemExit("No pages were rendered; cannot write PDF.")

    converted = [page.convert("RGB") for page in pages]
    converted[0].save(
        output_pdf,
        format="PDF",
        save_all=True,
        append_images=converted[1:],
        resolution=300.0,
    )


if __name__ == "__main__":
    raise SystemExit(main())
