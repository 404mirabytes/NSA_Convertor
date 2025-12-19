#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Noteshelf (Android) .nsa -> PDF (template + annotations) [best-effort]

- .nsa je ZIP
- Document.plist popisuje stránky (uuid, pdfKitPageRect, associatedPDFFileName, associatedPDFKitPageIndex...)
- Templates/*.ns_pdf jsou normální PDF (podklad)
- Annotations/<page_uuid> je SQLite s tabulkou "annotation"
  - ink tahy: annotationType=0, blob ve stroke_segments_v3
  - blob: segmentCount * 28 bytes; každý segment 7x float32 LE:
      x1,y1,x2,y2,?,pressure,?
  - tvary: annotationType=5, JSON ve shape_data (controlPoints, strokeOpacity, properties.strokeThickness, ...)

Cíl: vyrobit "flattened" PDF podobné Noteshelf exportu.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import sqlite3
import struct
import tempfile
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from statistics import median
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import fitz  # PyMuPDF


# -----------------------------
# Helpers
# -----------------------------

def parse_pdfkit_rect(rect_str: str) -> Optional[Tuple[float, float]]:
    """
    Noteshelf mívá: '{{0.0, 0.0}}, {1200.0, 1698.1512}}'
    Vrací (w, h) z posledních dvou čísel.
    """
    nums = re.findall(r"[-+]?\d*\.?\d+", rect_str or "")
    if len(nums) >= 4:
        return float(nums[2]), float(nums[3])
    return None


def extract_points_from_blob(blob: bytes) -> List[Tuple[float, float]]:
    """stroke_segments_v3: 7 float32 per segment (28 bytes). Vrací polyline body."""
    if not blob:
        return []
    if len(blob) % 28 != 0:
        blob = blob[: len(blob) - (len(blob) % 28)]
        if not blob:
            return []
    it = struct.iter_unpack("<7f", blob)
    pts: List[Tuple[float, float]] = []
    try:
        x1, y1, x2, y2, *_ = next(it)
    except StopIteration:
        return []
    pts.append((x1, y1))
    pts.append((x2, y2))
    for x1, y1, x2, y2, *_ in it:
        pts.append((x2, y2))
    return pts


def rdp(points: List[Tuple[float, float]], epsilon: float) -> List[Tuple[float, float]]:
    """Ramer-Douglas-Peucker zjednodušení polyline pro hladší výsledek a menší PDF."""
    if len(points) < 3:
        return points

    (x1, y1) = points[0]
    (x2, y2) = points[-1]
    dx = x2 - x1
    dy = y2 - y1
    norm = dx * dx + dy * dy

    max_dist = -1.0
    index = -1

    for i, (x, y) in enumerate(points[1:-1], start=1):
        if norm == 0:
            dist = (x - x1) ** 2 + (y - y1) ** 2
        else:
            t = ((x - x1) * dx + (y - y1) * dy) / norm
            projx = x1 + t * dx
            projy = y1 + t * dy
            dist = (x - projx) ** 2 + (y - projy) ** 2
        if dist > max_dist:
            max_dist = dist
            index = i

    if max_dist <= epsilon * epsilon:
        return [points[0], points[-1]]

    left = rdp(points[: index + 1], epsilon)
    right = rdp(points[index:], epsilon)
    return left[:-1] + right


def rgb_from_int(color_int: int) -> Tuple[float, float, float]:
    """0xRRGGBB -> (r,g,b) in 0..1"""
    r = (color_int >> 16) & 0xFF
    g = (color_int >> 8) & 0xFF
    b = color_int & 0xFF
    return (r / 255.0, g / 255.0, b / 255.0)


def hsl_metrics_from_rgb_int(color_int: int) -> Tuple[float, float]:
    """Vrátí (saturation, lightness) z RGB (0..1)."""
    r = ((color_int >> 16) & 0xFF) / 255.0
    g = ((color_int >> 8) & 0xFF) / 255.0
    b = (color_int & 0xFF) / 255.0
    mx = max(r, g, b)
    mn = min(r, g, b)
    l = (mx + mn) / 2.0
    if mx == mn:
        s = 0.0
    else:
        d = mx - mn
        s = d / (2 - mx - mn) if l > 0.5 else d / (mx + mn)
    return s, l


def choose_invert_y(sample_pts: List[Tuple[float, float]], page_w: float, page_h: float, sx: float, sy: float) -> bool:
    """
    Zkusí (y) a (page_h - y) a vybere variantu, která má víc bodů uvnitř stránky.
    """
    def score(invert: bool) -> float:
        inside = 0
        total = 0
        for x, y in sample_pts:
            X = x * sx
            Y = y * sy
            if invert:
                Y = page_h - Y
            total += 1
            if 0 <= X <= page_w and 0 <= Y <= page_h:
                inside += 1
        return inside / total if total else 0.0

    s0 = score(False)
    s1 = score(True)
    return s1 > s0 + 0.05


# -----------------------------
# PenType classification
# -----------------------------

@dataclass
class PenStats:
    count: int
    med_width: float
    avg_sat: float
    avg_light: float
    unique_colors: int


def collect_pen_stats(zf: zipfile.ZipFile, ann_members: List[str]) -> Dict[int, PenStats]:
    widths: Dict[int, List[float]] = defaultdict(list)
    colors: Dict[int, Counter] = defaultdict(Counter)

    for m in ann_members:
        db_bytes = zf.read(m)
        with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tf:
            tf.write(db_bytes)
            tf.flush()
            temp_path = tf.name
        
        try:
            con = sqlite3.connect(temp_path)
            cur = con.cursor()
            # Zajímá nás jen ink
            for pt, w, c in cur.execute(
                "SELECT penType, strokeWidth, strokeColor FROM annotation WHERE annotationType=0"
            ):
                pt = int(pt) if pt is not None else -1
                if w is not None:
                    widths[pt].append(float(w))
                if c is not None:
                    colors[pt][int(c)] += 1
            con.close()
        finally:
            os.unlink(temp_path)

    stats: Dict[int, PenStats] = {}
    for pt, ws in widths.items():
        if not ws:
            continue
        medw = median(ws)
        total = 0
        sat_sum = 0.0
        light_sum = 0.0
        for col, cnt in colors[pt].most_common(25):
            s, l = hsl_metrics_from_rgb_int(col)
            sat_sum += s * cnt
            light_sum += l * cnt
            total += cnt
        avg_s = sat_sum / total if total else 0.0
        avg_l = light_sum / total if total else 0.0
        stats[pt] = PenStats(
            count=len(ws),
            med_width=medw,
            avg_sat=avg_s,
            avg_light=avg_l,
            unique_colors=len(colors[pt]),
        )
    return stats


def classify_highlighters(stats: Dict[int, PenStats]) -> List[int]:
    """
    Heuristika: zvýrazňovač = jasnější/sytější barvy + rozumný strokeWidth + dost dat.
    """
    candidates = [pt for pt, s in stats.items() if s.count >= 20]
    if not candidates:
        return []

    overall_med = median([stats[pt].med_width for pt in candidates])

    highlighters: List[int] = []
    for pt in candidates:
        s = stats[pt]
        if s.avg_sat > 0.35 and s.avg_light > 0.18 and s.med_width >= overall_med * 0.9:
            highlighters.append(pt)

    return highlighters


def compute_highlighter_width_multipliers(
    stats: Dict[int, PenStats],
    highlighter_types: List[int],
    desired_ratio_to_pen: float,
    clamp: Tuple[float, float] = (1.0, 12.0),
) -> Dict[int, float]:
    """
    Cíl: aby highlighter vypadal cca desired_ratio_to_pen krát tlustší než normální pero,
    podle mediánů strokeWidth v DB.

    mult = desired_ratio * pen_med / hl_med
    """
    hl_set = set(highlighter_types)
    pen_meds = [s.med_width for pt, s in stats.items() if pt not in hl_set and s.count >= 20]
    pen_med = median(pen_meds) if pen_meds else None

    mults: Dict[int, float] = {}
    for pt in highlighter_types:
        hl_med = stats[pt].med_width
        if pen_med and hl_med > 0:
            mult = desired_ratio_to_pen * (pen_med / hl_med)
        else:
            mult = 4.0
        mult = max(clamp[0], min(clamp[1], mult))
        mults[pt] = mult
    return mults


# -----------------------------
# Core conversion
# -----------------------------

def find_document_plist_member(zf: zipfile.ZipFile) -> str:
    for n in zf.namelist():
        if n.endswith("Document.plist"):
            return n
    raise RuntimeError("Nenalezen Document.plist uvnitř .nsa")


def build_annotation_index(zf: zipfile.ZipFile) -> Dict[str, str]:
    """
    Map uuid -> zip member path
    (soubor je typicky .../Annotations/<uuid>)
    """
    ann = {}
    for n in zf.namelist():
        if "/Annotations/" in n and not n.endswith("/"):
            ann[os.path.basename(n)] = n
    return ann


def build_template_index(zf: zipfile.ZipFile) -> Dict[str, str]:
    """
    Map basename(template) -> zip member path (Templates/.../*.ns_pdf)
    """
    tpl = {}
    for n in zf.namelist():
        if n.endswith(".ns_pdf") or n.lower().endswith(".pdf"):
            base = os.path.basename(n)
            tpl[base] = n
    return tpl


def build_resource_index(zf: zipfile.ZipFile) -> Dict[str, str]:
    """
    Map resource id (basename without extension) -> zip member path (Resources/.../image)
    """
    exts = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
    res = {}
    for n in zf.namelist():
        if "/Resources/" not in n or n.endswith("/"):
            continue
        _, ext = os.path.splitext(n)
        if ext.lower() not in exts:
            continue
        base = os.path.splitext(os.path.basename(n))[0]
        res.setdefault(base, n)
        res.setdefault(base.lower(), n)
    return res


def open_template_cached(cache: Dict[str, fitz.Document], zf: zipfile.ZipFile, member: str) -> fitz.Document:
    if member in cache:
        return cache[member]
    data = zf.read(member)
    if not data.startswith(b"%PDF"):
        raise RuntimeError(f"Template '{member}' nevypadá jako PDF.")
    doc = fitz.open(stream=data, filetype="pdf")
    cache[member] = doc
    return doc


def draw_page_annotations(
    zf: zipfile.ZipFile,
    out_page: fitz.Page,
    ann_member: str,
    sx: float,
    sy: float,
    resource_index: Dict[str, str],
    highlighter_types: set,
    highlighter_opacity: float,
    hl_width_mults: Dict[int, float],
    smooth: bool,
    epsilon: float,
) -> None:
    ann_bytes = zf.read(ann_member)
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tf:
        tf.write(ann_bytes)
        tf.flush()
        temp_path = tf.name
    
    try:
        con = sqlite3.connect(temp_path)
        cur = con.cursor()

        cols = [c[1] for c in cur.execute("PRAGMA table_info(annotation)").fetchall()]
        has_pen_type = "penType" in cols

        # rozhodni invert_y podle vzorku bodů
        sample_pts: List[Tuple[float, float]] = []
        for (blob,) in cur.execute("SELECT stroke_segments_v3 FROM annotation WHERE annotationType=0 LIMIT 20"):
            pts = extract_points_from_blob(blob)
            if pts:
                step = max(1, len(pts) // 30)
                sample_pts.extend(pts[::step])
            if len(sample_pts) > 500:
                break

        invert_y = choose_invert_y(sample_pts, out_page.rect.width, out_page.rect.height, sx, sy)

        # -------- images (annotationType=2) --------
        for img_id, bx, by, bw, bh, _img_tx, _tx in cur.execute(
            "SELECT id, boundingRect_x, boundingRect_y, boundingRect_w, boundingRect_h, imgTxMatrix, txMatrix "
            "FROM annotation WHERE annotationType=2"
        ):
            if not img_id or bw is None or bh is None:
                continue

            img_id_str = str(img_id)
            member = resource_index.get(img_id_str)
            if not member:
                base = os.path.splitext(img_id_str)[0]
                member = resource_index.get(base) or resource_index.get(base.lower())
            if not member:
                continue

            try:
                img_bytes = zf.read(member)
            except Exception:
                continue

            x0 = float(bx or 0.0) * sx
            y0 = float(by or 0.0) * sy
            w = float(bw) * sx
            h = float(bh) * sy
            if w <= 0 or h <= 0:
                continue

            if invert_y:
                y0 = out_page.rect.height - y0 - h

            rect = fitz.Rect(x0, y0, x0 + w, y0 + h)
            try:
                out_page.insert_image(rect, stream=img_bytes)
            except Exception:
                continue

        # -------- ink strokes (annotationType=0) --------
        groups = defaultdict(list)  # (rgb,width,opacity) -> list[polyline]

        if has_pen_type:
            q = "SELECT strokeWidth, strokeColor, penType, stroke_segments_v3 FROM annotation WHERE annotationType=0"
        else:
            q = "SELECT strokeWidth, strokeColor, 0 as penType, stroke_segments_v3 FROM annotation WHERE annotationType=0"

        for strokeWidth, strokeColor, penType, blob in cur.execute(q):
            pts = extract_points_from_blob(blob)
            if len(pts) < 2:
                continue

            # hodně dlouhé tahy zřeď (kvůli velikosti PDF)
            if len(pts) > 5000:
                step = max(1, len(pts) // 1600)
                pts = pts[::step]

            pts_scaled: List[Tuple[float, float]] = []
            for x, y in pts:
                X = x * sx
                Y = y * sy
                if invert_y:
                    Y = out_page.rect.height - Y
                pts_scaled.append((X, Y))

            if smooth and len(pts_scaled) > 3:
                pts_scaled = rdp(pts_scaled, epsilon)

            col = int(strokeColor) if strokeColor is not None else 0
            rgb = rgb_from_int(col)

            w = float(strokeWidth) if strokeWidth is not None else 1.0
            width = w * sx  # základní převod do PDF bodů

            pt = int(penType) if penType is not None else 0
            opacity = 1.0

            if pt in highlighter_types:
                opacity = highlighter_opacity
                width *= hl_width_mults.get(pt, 4.0)

            key = (rgb, round(width, 3), round(opacity, 3))
            groups[key].append(pts_scaled)

        for (rgb, width, opacity), polylines in groups.items():
            sh = out_page.new_shape()
            for poly in polylines:
                sh.draw_polyline(poly)
            sh.finish(width=float(width), color=rgb, stroke_opacity=float(opacity))
            sh.commit()

        # -------- shapes (annotationType=5) --------
        shape_groups = defaultdict(list)  # (rgb,width,opacity,closed) -> list[polyline]
        for strokeColor, strokeWidth, shape_data in cur.execute(
            "SELECT strokeColor, strokeWidth, shape_data FROM annotation WHERE annotationType=5"
        ):
            if not shape_data:
                continue
            try:
                sd = json.loads(shape_data)
            except Exception:
                continue

            pts = sd.get("controlPoints") or []
            if len(pts) < 2:
                continue

            pts_scaled: List[Tuple[float, float]] = []
            for x, y in pts:
                X = float(x) * sx
                Y = float(y) * sy
                if invert_y:
                    Y = out_page.rect.height - Y
                pts_scaled.append((X, Y))

            col = int(strokeColor) if strokeColor is not None else 0
            rgb = rgb_from_int(col)

            opacity = float(sd.get("strokeOpacity", 1.0))
            thickness = float(sd.get("properties", {}).get("strokeThickness", strokeWidth if strokeWidth is not None else 1.0))
            width = thickness * sx

            closed = int(sd.get("numberOfSides", 0)) >= 3
            key = (rgb, round(width, 3), round(opacity, 3), closed)
            shape_groups[key].append(pts_scaled)

        for (rgb, width, opacity, closed), polys in shape_groups.items():
            sh = out_page.new_shape()
            for poly in polys:
                sh.draw_polyline(poly)
                if closed:
                    sh.draw_line(poly[-1], poly[0])
            sh.finish(width=float(width), color=rgb, stroke_opacity=float(opacity))
            sh.commit()

        con.close()
    finally:
        os.unlink(temp_path)


def nsa_to_pdf(
    nsa_path: str,
    out_pdf: str,
    *,
    desired_highlighter_ratio: float = 5.0,
    highlighter_opacity: float = 0.38,
    smooth: bool = True,
    epsilon: float = 0.8,
    verbose: bool = True,
) -> None:
    if verbose:
        print(f"[+] Reading: {nsa_path}")

    with zipfile.ZipFile(nsa_path, "r") as zf:
        doc_member = find_document_plist_member(zf)
        doc = plistlib.loads(zf.read(doc_member))
        pages = doc.get("pages", [])
        if not pages:
            raise RuntimeError("Document.plist neobsahuje žádné stránky.")

        ann_index = build_annotation_index(zf)
        tpl_index = build_template_index(zf)
        res_index = build_resource_index(zf)

        # PenType stats pro auto-detekci zvýrazňovače
        ann_members_all = list(ann_index.values())
        stats = collect_pen_stats(zf, ann_members_all)
        hl_types = classify_highlighters(stats)
        hl_mults = compute_highlighter_width_multipliers(stats, hl_types, desired_highlighter_ratio)

        if verbose:
            print(f"[i] Detected highlighter penTypes: {hl_types} (opacity={highlighter_opacity}, ratio~{desired_highlighter_ratio}x)")
            if hl_types:
                print(f"[i] Highlighter width multipliers: {hl_mults}")

        # Cache pro template PDFs
        tpl_cache: Dict[str, fitz.Document] = {}

        # Output doc (nové PDF)
        out_doc = fitz.open()

        # Pro každou stránku vytvoř stránku v out_doc a dokresli anotace
        for i, p in enumerate(pages, start=1):
            uuid = p.get("uuid")
            tpl_name = p.get("associatedPDFFileName") or next(iter(doc.get("documents", {}).keys()), None)
            pdf_idx_1based = p.get("associatedPDFKitPageIndex") or p.get("associatedPageIndex") or 1
            tpl_basename = os.path.basename(tpl_name) if tpl_name else None

            # 1) vlož template stránku (nebo vytvoř blank)
            out_page: fitz.Page
            if tpl_basename and tpl_basename in tpl_index:
                tpl_member = tpl_index[tpl_basename]
                tpl_doc = open_template_cached(tpl_cache, zf, tpl_member)
                src_idx = int(pdf_idx_1based) - 1
                if 0 <= src_idx < tpl_doc.page_count:
                    out_doc.insert_pdf(tpl_doc, from_page=src_idx, to_page=src_idx)
                    out_page = out_doc[-1]
                else:
                    # fallback blank
                    dims = parse_pdfkit_rect(p.get("pdfKitPageRect", "")) or (1200.0, 1600.0)
                    out_page = out_doc.new_page(width=dims[0] * 0.5, height=dims[1] * 0.5)
            else:
                dims = parse_pdfkit_rect(p.get("pdfKitPageRect", "")) or (1200.0, 1600.0)
                out_page = out_doc.new_page(width=dims[0] * 0.5, height=dims[1] * 0.5)

            # 2) spočti scale Noteshelf coords -> PDF coords
            dims = parse_pdfkit_rect(p.get("pdfKitPageRect", "")) or None
            if dims:
                pw, ph = dims
                sx = (out_page.rect.width / pw) if pw else 1.0
                sy = (out_page.rect.height / ph) if ph else 1.0
            else:
                sx = sy = 1.0

            # 3) anotace
            if uuid and uuid in ann_index:
                draw_page_annotations(
                    zf=zf,
                    out_page=out_page,
                    ann_member=ann_index[uuid],
                    sx=sx,
                    sy=sy,
                    resource_index=res_index,
                    highlighter_types=set(hl_types),
                    highlighter_opacity=highlighter_opacity,
                    hl_width_mults=hl_mults,
                    smooth=smooth,
                    epsilon=epsilon,
                )

            if verbose and i % 10 == 0:
                print(f"[i] Processed pages: {i}/{len(pages)}")

        # Close cached template docs
        for d in tpl_cache.values():
            d.close()

        os.makedirs(os.path.dirname(os.path.abspath(out_pdf)) or ".", exist_ok=True)
        out_doc.save(out_pdf)
        out_doc.close()

    if verbose:
        print(f"[+] Written: {out_pdf}")


# -----------------------------
# CLI
# -----------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Convert Noteshelf Android .nsa to PDF (template + annotations).")
    ap.add_argument("input", help="Cesta k .nsa souboru nebo složce s .nsa")
    ap.add_argument("-o", "--output", help="Výstupní PDF (pokud input je jeden soubor)")
    ap.add_argument("--outdir", help="Výstupní složka (pokud input je složka)")
    ap.add_argument("--quiet", action="store_true", help="Méně výpisů")

    ap.add_argument("--highlighter-opacity", type=float, default=0.38,
                    help="Průhlednost zvýrazňovače (0..1). Default 0.38")
    ap.add_argument("--highlighter-ratio", type=float, default=5.0,
                    help="Jak moc má být zvýrazňovač tlustší než pero (poměr). Default 5.0")

    ap.add_argument("--no-smooth", action="store_true",
                    help="Vypnout vyhlazení (RDP).")
    ap.add_argument("--epsilon", type=float, default=0.8,
                    help="RDP epsilon (větší = hladší, menší = přesnější). Default 0.8")

    args = ap.parse_args()
    verbose = not args.quiet
    smooth = not args.no_smooth

    in_path = args.input

    if os.path.isdir(in_path):
        outdir = args.outdir or os.path.join(in_path, "pdf_out")
        os.makedirs(outdir, exist_ok=True)

        nsa_files = [f for f in os.listdir(in_path) if f.lower().endswith(".nsa")]
        if not nsa_files:
            raise SystemExit("Ve složce nejsou žádné .nsa soubory.")

        for f in sorted(nsa_files):
            src = os.path.join(in_path, f)
            base = os.path.splitext(f)[0]
            dst = os.path.join(outdir, base + ".pdf")

            nsa_to_pdf(
                src, dst,
                desired_highlighter_ratio=args.highlighter_ratio,
                highlighter_opacity=args.highlighter_opacity,
                smooth=smooth,
                epsilon=args.epsilon,
                verbose=verbose,
            )

        if verbose:
            print(f"[+] Done. Output dir: {outdir}")
        return

    if not in_path.lower().endswith(".nsa"):
        raise SystemExit("Input není .nsa soubor (ani složka).")

    out_pdf = args.output
    if not out_pdf:
        base = os.path.splitext(os.path.basename(in_path))[0]
        out_pdf = os.path.join(os.path.dirname(in_path) or ".", base + ".pdf")

    nsa_to_pdf(
        in_path, out_pdf,
        desired_highlighter_ratio=args.highlighter_ratio,
        highlighter_opacity=args.highlighter_opacity,
        smooth=smooth,
        epsilon=args.epsilon,
        verbose=verbose,
    )


if __name__ == "__main__":
    main()
