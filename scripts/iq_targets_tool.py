"""Authoring helper for the IQ book drawing targets (Phase 17.7, private content).

Renders contact sheets into ``<src>/work/`` so zones ("draw what is missing") and
reference lines ("divide the square") can be authored in ``<src>/layout.json`` by
looking at the pictures, then checked again after every edit:

    python -m scripts.iq_targets_tool cells [--src DIR]   # T6 cells + 10 % grid + zones
    python -m scripts.iq_targets_tool lines [--src DIR]   # T8 items + square + fitted lines

The grid lines are every 0.1 of the picture (red; 0.5 darker), so a zone can be read
off as normalized coordinates. Holds no book content; reads only the private folder.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from app.core.logging import configure_logging, get_logger
from scripts.build_iq_book import DEFAULT_SRC, Builder, _zone_poly, find_blank_square

log = get_logger("scripts.iq_targets_tool")

TILE = 420


def _grid(img: Image.Image) -> Image.Image:
    w, h = img.size
    out = img.convert("RGB")
    d = ImageDraw.Draw(out)
    for i in range(1, 10):
        col = (255, 60, 60) if i == 5 else (255, 170, 170)
        d.line([(i * w / 10, 0), (i * w / 10, h)], fill=col, width=1)
        d.line([(0, i * h / 10), (w, i * h / 10)], fill=col, width=1)
    return out


def _sheet(tiles: list[tuple[str, Image.Image]], per_row: int, path: Path) -> None:
    cell_w = TILE + 10
    cell_h = max(t.size[1] for _, t in tiles) + 26
    rows = (len(tiles) + per_row - 1) // per_row
    sheet = Image.new("RGB", (per_row * cell_w, rows * cell_h), "white")
    d = ImageDraw.Draw(sheet)
    for k, (label, tile) in enumerate(tiles):
        x, y = (k % per_row) * cell_w, (k // per_row) * cell_h
        d.text((x + 4, y + 4), label, fill=(0, 0, 200))
        sheet.paste(tile, (x, y + 20))
    sheet.save(path)
    log.info("wrote %s", path)


def _scaled(img: Image.Image) -> Image.Image:
    w, h = img.size
    return img.resize((TILE, round(h * TILE / w)), Image.Resampling.LANCZOS)


def cells(b: Builder, work: Path) -> None:
    zones: dict[str, Any] = b.layout["t6"]["zones"]
    tiles = []
    for n, img in b._cell_images().items():
        tile = _grid(_scaled(img))
        w, h = tile.size
        d = ImageDraw.Draw(tile, "RGBA")
        for z in zones.get(str(n), {}).get("zones", []):
            poly = [(x * w, y * h) for x, y in _zone_poly(z)]
            d.polygon(poly, fill=(0, 120, 255, 60), outline=(0, 80, 220, 255))
        tiles.append((f"T6 Q{n}", tile))
    for part in range(0, len(tiles), 10):
        _sheet(tiles[part : part + 10], 5, work / f"t6_cells_{part // 10 + 1}.png")


def lines(b: Builder, work: Path) -> None:
    sec = b.t8()
    tiles = []
    for it in sec["items"]:
        img = b.assets[it["asset_id"]]
        tile = _scaled(img).convert("RGB")
        w, h = tile.size
        d = ImageDraw.Draw(tile)
        sq = it["target"]["square"]
        x0, y0 = sq["x"] * w, sq["y"] * h
        sw, sh = sq["w"] * w, sq["h"] * h
        d.rectangle([x0, y0, x0 + sw, y0 + sh], outline=(0, 90, 255), width=2)
        for s in it["target"]["segments"]:
            (ax, ay), (bx, by) = s["a"], s["b"]
            d.line(
                [(x0 + ax * sw, y0 + ay * sh), (x0 + bx * sw, y0 + by * sh)],
                fill=(230, 0, 0),
                width=3,
            )
        tiles.append((it["id"].rsplit("-", 1)[-1], tile))
    _sheet(tiles, 3, work / "t8_items.png")
    find_blank_square  # noqa: B018 — the square comes from the build's detector


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render IQ drawing-target sheets.")
    parser.add_argument("what", choices=["cells", "lines"])
    parser.add_argument("--src", default=str(DEFAULT_SRC))
    args = parser.parse_args(argv)
    configure_logging("INFO")
    b = Builder(Path(args.src))
    work = Path(args.src) / "work"
    work.mkdir(exist_ok=True)
    if args.what == "cells":
        cells(b, work)
    else:
        lines(b, work)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
