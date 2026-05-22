#!/usr/bin/env python3
"""
PDF Reflow for Kindle Oasis 2 (7", 1264x1680, 300ppi)

Image-level reflow: detects columns (via text coords or pixel analysis),
cuts pages into regions, applies uniform scale, outputs Kindle-ready PDF.
No OCR — tables, figures, formulas stay intact as images.

Usage:
    python3 kindle-reflow.py input.pdf [-o output.pdf] [--margin 40] [--max-zoom 2.0]
"""

import argparse
import sys
import os
import fitz  # PyMuPDF
from PIL import Image, ImageFilter, ImageEnhance
from concurrent.futures import ProcessPoolExecutor
import io

DEVICE_W_PX = 1264
DEVICE_H_PX = 1680
DEVICE_DPI = 300


def render_page(page, render_dpi):
    zoom = render_dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)


def row_dark(data, w, y, x0, x1, t=240):
    return sum(1 for x in range(x0, x1) if data[y * w + x] < t)


def col_dark(data, w, x, y0, y1, t=240):
    return sum(1 for y in range(y0, y1) if data[y * w + x] < t)


def find_bbox(gray, l, t, r, b, thresh=240):
    w = gray.size[0]
    d = gray.tobytes()
    span_h = max(1, r - l)
    while t < b and row_dark(d, w, t, l, r, thresh) / span_h < 0.003:
        t += 1
    while b > t + 1 and row_dark(d, w, b - 1, l, r, thresh) / span_h < 0.003:
        b -= 1
    if b <= t:
        return None
    span_v = max(1, b - t)
    while l < r and col_dark(d, w, l, t, b, thresh) / span_v < 0.005:
        l += 1
    while r > l + 1 and col_dark(d, w, r - 1, t, b, thresh) / span_v < 0.005:
        r -= 1
    if r - l < 10 or b - t < 5:
        return None
    return l, t, r, b


def detect_columns_from_text(page, render_dpi):
    """Use PyMuPDF text block coordinates to detect column split."""
    blocks = page.get_text("blocks")
    # blocks: (x0, y0, x1, y1, text, block_no, block_type)
    text_blocks = [(b[0], b[1], b[2], b[3]) for b in blocks if b[6] == 0 and len(b[4].strip()) > 10]

    if len(text_blocks) < 4:
        return None

    scale = render_dpi / 72.0
    page_w = page.rect.width

    # Find block x-centers
    centers = [((b[0] + b[2]) / 2) for b in text_blocks]

    # Cluster into left vs right
    mid = page_w / 2
    left_blocks = [b for b, c in zip(text_blocks, centers) if c < mid * 0.9]
    right_blocks = [b for b, c in zip(text_blocks, centers) if c > mid * 1.1]

    if len(left_blocks) < 2 or len(right_blocks) < 2:
        return None

    # Column gap: between max right edge of left blocks and min left edge of right blocks
    left_max_x = max(b[2] for b in left_blocks)
    right_min_x = min(b[0] for b in right_blocks)

    gap = right_min_x - left_max_x
    if gap < 5:  # Less than 5pt gap — not really two columns
        return None

    split_pt = (left_max_x + right_min_x) / 2

    # Find where columns start/end vertically
    col_block_tops = [min(b[1] for b in left_blocks), min(b[1] for b in right_blocks)]
    col_block_bots = [max(b[3] for b in left_blocks), max(b[3] for b in right_blocks)]
    col_top = min(col_block_tops)
    col_bot = max(col_block_bots)

    # Check for full-width blocks above/below the columns
    full_width_blocks = [b for b, c in zip(text_blocks, centers)
                         if mid * 0.9 <= c <= mid * 1.1
                         or (b[2] - b[0]) > page_w * 0.6]

    header_bottom = 0
    footer_top = page.rect.height
    for b in full_width_blocks:
        if b[3] < col_top + 20:
            header_bottom = max(header_bottom, b[3])
        if b[1] > col_bot - 20:
            footer_top = min(footer_top, b[1])

    # Convert to pixel coordinates
    return {
        "split_x": int(split_pt * scale),
        "col_top": int(max(col_top, header_bottom) * scale),
        "col_bot": int(min(col_bot, footer_top) * scale),
        "header_bot": int(header_bottom * scale) if header_bottom > 0 else None,
        "footer_top": int(footer_top * scale) if footer_top < page.rect.height else None,
    }


def detect_columns_from_pixels(gray, left, top, right, bottom, min_gap_px=12):
    """Fallback: scan for vertical blank strip (for scanned books)."""
    w = gray.size[0]
    d = gray.tobytes()
    rw = right - left
    rh = bottom - top
    if rw < 300 or rh < 200:
        return None

    scan_l = left + int(rw * 0.3)
    scan_r = left + int(rw * 0.7)

    # Use a stricter threshold for scanned pages (lower = more contrast)
    densities = []
    for x in range(scan_l, scan_r):
        dark = col_dark(d, w, x, top, bottom, 200)
        densities.append(dark / max(1, rh))

    best_s, best_len = -1, 0
    cur_s, cur_len = -1, 0
    for i, dens in enumerate(densities):
        if dens < 0.015:
            if cur_s < 0:
                cur_s = i
            cur_len = i - cur_s + 1
        else:
            if cur_len > best_len:
                best_s, best_len = cur_s, cur_len
            cur_s, cur_len = -1, 0
    if cur_len > best_len:
        best_s, best_len = cur_s, cur_len

    if best_len >= min_gap_px:
        return scan_l + best_s + best_len // 2
    return None


def h_splits(gray, left, top, right, bottom, min_gap=8):
    w = gray.size[0]
    d = gray.tobytes()
    splits = []
    in_gap = False
    gap_start = top
    span = max(1, right - left)
    for y in range(top, bottom):
        blank = row_dark(d, w, y, left, right, 240) / span < 0.003
        if blank and not in_gap:
            in_gap = True
            gap_start = y
        elif not blank and in_gap:
            if y - gap_start >= min_gap:
                splits.append((gap_start, y))
            in_gap = False
    return splits


def column_regions(img, gray, left, top, right, bottom, min_gap):
    splits = h_splits(gray, left, top, right, bottom, min_gap)
    spans = []
    cur = top
    for gt, gb in splits:
        if gt > cur + 3:
            spans.append((cur, gt))
        cur = gb
    if bottom > cur + 3:
        spans.append((cur, bottom))
    if not spans:
        spans.append((top, bottom))

    regions = []
    for rt, rb in spans:
        bbox = find_bbox(gray, left, rt, right, rb)
        if bbox is None:
            continue
        cl, ct, cr, cb = bbox
        if cr - cl < 10 or cb - ct < 5:
            continue
        regions.append((img.crop((cl, ct, cr, cb)), cr - cl, cb - ct))
    return regions


def process_page(page, render_dpi, min_gap):
    img = render_page(page, render_dpi)
    gray = img.convert("L")
    w, h = gray.size

    bbox = find_bbox(gray, 0, 0, w, h)
    if bbox is None:
        return []
    cl, ct, cr, cb = bbox
    if cr - cl < 30 or cb - ct < 30:
        return []

    # Method 1: try text-based column detection (works for native PDFs)
    col_info = detect_columns_from_text(page, render_dpi)

    if col_info and col_info["split_x"] > cl + 50 and col_info["split_x"] < cr - 50:
        sx = col_info["split_x"]
        c_top = col_info["col_top"]
        c_bot = col_info["col_bot"]
        result = []

        # Full-width header
        if col_info["header_bot"] and col_info["header_bot"] > ct + 10:
            result.extend(column_regions(img, gray, cl, ct, cr, col_info["header_bot"], min_gap))

        # Left then right column
        result.extend(column_regions(img, gray, cl, c_top, sx - 5, c_bot, min_gap))
        result.extend(column_regions(img, gray, sx + 5, c_top, cr, c_bot, min_gap))

        # Full-width footer
        if col_info["footer_top"] and col_info["footer_top"] < cb - 10:
            result.extend(column_regions(img, gray, cl, col_info["footer_top"], cr, cb, min_gap))

        if result:
            return result

    # Method 2: pixel-based column detection (for scanned books)
    split_x = detect_columns_from_pixels(gray, cl, ct, cr, cb)

    if split_x is not None:
        result = []
        result.extend(column_regions(img, gray, cl, ct, split_x - 5, cb, min_gap))
        result.extend(column_regions(img, gray, split_x + 5, ct, cr, cb, min_gap))
        return result

    # Single column
    return column_regions(img, gray, cl, ct, cr, cb, min_gap)


def compute_base_scale(all_regions, target_w, max_zoom):
    """Compute scale from the widest common region (body text paragraphs).

    The key insight: scale must be based on the WIDEST body text, because
    that's what fills the target width. Narrower regions (short lines,
    headers) then appear at the same scale — never over-zoomed.
    """
    if not all_regions:
        return 1.0

    # Weight by area: body text paragraphs have the most total area
    width_area = {}
    for _, rw, rh in all_regions:
        if rh < 10:
            continue
        bucket = (rw // 20) * 20
        width_area[bucket] = width_area.get(bucket, 0) + rw * rh

    if not width_area:
        return 1.0

    # The dominant width is the bucket with the most total pixel area
    dominant_bucket = max(width_area, key=width_area.get)

    # Get actual median width of regions near the dominant bucket
    near = [rw for _, rw, rh in all_regions if abs(rw - dominant_bucket) < 40 and rh > 10]
    if near:
        body_width = sorted(near)[len(near) // 2]
    else:
        body_width = dominant_bucket

    scale = target_w / max(1, body_width)
    return min(scale, max_zoom)


def _scan_page_worker(args):
    """Pass 1: return only region dimensions (no image data)."""
    input_path, page_idx, render_dpi, min_gap = args
    doc = fitz.open(input_path)
    regions = process_page(doc[page_idx], render_dpi, min_gap)
    doc.close()
    return [(rw, rh) for _, rw, rh in regions]


def _process_page_worker(args):
    """Pass 2: return full region image data."""
    input_path, page_idx, render_dpi, min_gap = args
    doc = fitz.open(input_path)
    regions = process_page(doc[page_idx], render_dpi, min_gap)
    doc.close()
    return [(img.tobytes(), img.mode, img.size, rw, rh) for img, rw, rh in regions]


def reflow_pdf(input_path, output_path, margin=40, max_zoom=2.0, render_dpi=300,
               min_gap=None, gap_between=10, workers=None):
    doc = fitz.open(input_path)
    total = len(doc)
    doc.close()
    target_w = DEVICE_W_PX - 2 * margin
    target_h = DEVICE_H_PX - 2 * margin

    if min_gap is None:
        min_gap = max(6, int(render_dpi * 0.02))
    if workers is None:
        workers = min(os.cpu_count() or 4, 8)

    args_list = [(input_path, i, render_dpi, min_gap) for i in range(total)]

    # Pass 1: scan all pages for region dimensions only (lightweight)
    print(f"[1/2] Scanning {total} pages with {workers} workers...", flush=True)
    all_dims = []
    with ProcessPoolExecutor(max_workers=workers) as executor:
        for i, dims in enumerate(executor.map(_scan_page_worker, args_list)):
            all_dims.extend(dims)
            if (i + 1) % 50 == 0 or i == total - 1:
                print(f"  Scanned {i+1}/{total}: {len(all_dims)} regions", flush=True)

    base_scale = compute_base_scale(
        [(None, rw, rh) for rw, rh in all_dims], target_w, max_zoom)
    print(f"  Base scale: {base_scale:.2f}x", flush=True)

    # Pass 2: re-process pages and write output PDF streaming
    print(f"[2/2] Rendering and writing...", flush=True)
    out_doc = fitz.open()
    page_img = Image.new("L", (DEVICE_W_PX, DEVICE_H_PX), 255)
    cursor_y = margin
    n_pages = 0
    n_regions = 0

    def flush():
        nonlocal page_img, cursor_y, n_pages
        buf = io.BytesIO()
        page_img.save(buf, format="PNG")
        p = out_doc.new_page(width=DEVICE_W_PX, height=DEVICE_H_PX)
        p.insert_image(fitz.Rect(0, 0, DEVICE_W_PX, DEVICE_H_PX), stream=buf.getvalue())
        n_pages += 1
        page_img = Image.new("L", (DEVICE_W_PX, DEVICE_H_PX), 255)
        cursor_y = margin

    with ProcessPoolExecutor(max_workers=workers) as executor:
        for i, serialized in enumerate(executor.map(_process_page_worker, args_list)):
            for raw, mode, size, rw, rh in serialized:
                region_img = Image.frombytes(mode, size, raw)
                scale = base_scale
                if rw * scale > target_w:
                    scale = target_w / rw
                new_w = int(rw * scale)
                new_h = int(rh * scale)
                if new_h > target_h:
                    scale = target_h / rh
                    new_w = int(rw * scale)
                    new_h = target_h
                if cursor_y + new_h > DEVICE_H_PX - margin and cursor_y > margin:
                    flush()
                resized = region_img.convert("L").resize((new_w, new_h), Image.LANCZOS)
                resized = ImageEnhance.Contrast(resized).enhance(1.3)
                resized = resized.filter(ImageFilter.UnsharpMask(radius=1.0, percent=80, threshold=3))
                page_img.paste(resized, (margin, cursor_y))
                cursor_y += new_h + gap_between
                n_regions += 1
            if (i + 1) % 50 == 0 or i == total - 1:
                print(f"  Page {i+1}/{total}: {n_regions} regions, {n_pages} output pages", flush=True)

    if cursor_y > margin:
        flush()

    out_doc.save(output_path, deflate=True)
    out_doc.close()

    mb = os.path.getsize(output_path) / (1024 * 1024)
    print(f"Done! {total} -> {n_pages} pages ({mb:.1f} MB) -> {output_path}", flush=True)


def main():
    p = argparse.ArgumentParser(description="Reflow PDF for Kindle Oasis 2")
    p.add_argument("input", help="Input PDF")
    p.add_argument("-o", "--output", help="Output PDF (default: input_kindle.pdf)")
    p.add_argument("--margin", type=int, default=40, help="Margin px (default: 40)")
    p.add_argument("--max-zoom", type=float, default=2.0, help="Max zoom (default: 2.0)")
    p.add_argument("--render-dpi", type=int, default=300, help="Render DPI (default: 300)")
    p.add_argument("--min-gap", type=int, default=None, help="Min split gap px")
    p.add_argument("--gap", type=int, default=10, help="Output region gap px (default: 10)")
    p.add_argument("--workers", type=int, default=None, help="Parallel workers (default: cpu count)")
    args = p.parse_args()

    if not os.path.isfile(args.input):
        print(f"Error: {args.input} not found"); sys.exit(1)
    if args.output is None:
        base, ext = os.path.splitext(args.input)
        args.output = f"{base}_kindle{ext}"

    reflow_pdf(args.input, args.output, args.margin, args.max_zoom,
               args.render_dpi, args.min_gap, args.gap, args.workers)


if __name__ == "__main__":
    main()
