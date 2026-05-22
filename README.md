# kindle-reflow

Image-level PDF reflow for Kindle e-readers.

Detects columns (text-based for native PDFs, pixel-based for scanned books), splits pages into regions, applies uniform scaling, and outputs a Kindle-ready PDF. No OCR — tables, figures, charts, and formulas stay intact as images.

## Features

- **Column detection**: text-coordinate-based (native PDFs) and pixel-gap-based (scanned books)
- **Uniform font size**: computes a single scale factor from the dominant body text width — no sudden size jumps
- **Lossless output**: PNG-embedded pages, no JPEG compression artifacts
- **Image enhancement**: contrast boost + unsharp mask for sharper text on e-ink
- **Left-aligned layout**: regions are left-aligned with configurable margins

## Install

```bash
pip install .
```

Or with uv:

```bash
uv pip install .
```

## Usage

```bash
kindle-reflow input.pdf [-o output.pdf] [--margin 40] [--max-zoom 2.0]
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `-o, --output` | `input_kindle.pdf` | Output file path |
| `--margin` | `40` | Margin in pixels |
| `--max-zoom` | `2.0` | Maximum zoom factor |
| `--render-dpi` | `300` | Render DPI |
| `--min-gap` | auto | Minimum gap (px) for horizontal splitting |
| `--gap` | `10` | Gap between regions in output (px) |

### Examples

```bash
# Basic usage
kindle-reflow paper.pdf

# Custom output path and margins
kindle-reflow textbook.pdf -o textbook_kindle.pdf --margin 30

# Limit zoom for scanned books
kindle-reflow scan.pdf --max-zoom 1.5
```

## Target Device

Default settings target **Kindle Oasis 2** (7", 1264x1680px, 300ppi). Edit `DEVICE_W_PX`, `DEVICE_H_PX`, and `DEVICE_DPI` in the script for other devices.

## How It Works

1. Render each PDF page at 300 DPI
2. Detect content bounding box (strip margins)
3. Detect columns via text block coordinates or vertical blank-strip scanning
4. Split columns into horizontal regions at paragraph gaps
5. Compute a uniform base scale from the widest body text regions (area-weighted)
6. Layout regions onto Kindle-sized pages with uniform scaling
7. Apply contrast enhancement and sharpening
8. Save as PNG-embedded PDF (lossless)

## Limitations

- For scanned books wider than the Kindle screen, text will be physically smaller (image-level reflow cannot enlarge text without OCR)
- Column detection assumes standard layouts — unusual multi-column arrangements may not be detected
