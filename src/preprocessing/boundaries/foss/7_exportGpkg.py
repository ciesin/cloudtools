#!/usr/bin/env python3
"""
Export FlatGeobuf files as layers in a single GeoPackage (.gpkg).

Each FGB becomes a named feature class (layer) in the output GPKG.
Layer names are the FGB filename stems (full name without extension).

If the output GPKG already exists it is overwritten.

Requires: geopandas>=1.0, pyogrio

Usage:
  # All nested boundary layers + any *_pagename* point layers in output_dir/:
  python 7_exportGpkg.py output_dir/ output.gpkg

  # Custom glob pattern:
  python 7_exportGpkg.py output_dir/ output.gpkg --pattern "*_nested_*"

  # Explicit list of FGBs (any order):
  python 7_exportGpkg.py output_dir/ output.gpkg --files a.fgb b.fgb c.fgb

Default pattern matches both dissolved boundary layers (*_nested_*) and
pagename-enriched point layers (*_pagename*), excluding the intermediate
airesante_*_pagename.fgb produced by step 1 (which lacks the 'nested' marker
and is superseded by the dissolved version).
"""

import argparse
import time
from pathlib import Path

import geopandas as gpd


BOUNDARY_LEVELS = ["province", "antenne", "zonesante", "airesante"]
NESTED_PATTERN = "*_nested_*_pagename_*.fgb"
LINES_PATTERN = "*_nested_*_pagename_*_lines.fgb"
POINT_PATTERN = "*_pagename.fgb"


def fmt(t0: float) -> str:
    s = time.time() - t0
    return f"{s/60:.1f}m" if s >= 60 else f"{s:.1f}s"


def _is_intermediate_polygon(path: Path) -> bool:
    """True for step-1 intermediate FGBs (un-dissolved polygon layers)."""
    stem = path.stem
    return (
        "_nested_" not in stem
        and any(f"_{lvl}_v" in stem for lvl in BOUNDARY_LEVELS)
    )


def collect_fgbs(source_dir: Path, pattern: str | None, explicit: list[Path]) -> list[Path]:
    if explicit:
        return [Path(f) for f in explicit]
    if pattern:
        return sorted(f for f in source_dir.glob(pattern) if f.suffix == ".fgb")

    # Default: for each admin level emit polygon then companion lines (if present),
    # then point layers.
    level_order = {lvl: i for i, lvl in enumerate(BOUNDARY_LEVELS)}

    def level_key(p: Path) -> int:
        return next((level_order[lvl] for lvl in BOUNDARY_LEVELS if f"_{lvl}_" in p.stem), 99)

    polygons = {
        f for f in source_dir.glob(NESTED_PATTERN)
        if not f.stem.endswith("_lines")
    }
    lines = {f for f in source_dir.glob(LINES_PATTERN)}

    # Interleave: province poly, province lines, antenne poly, antenne lines, …
    ordered: list[Path] = []
    for poly in sorted(polygons, key=level_key):
        ordered.append(poly)
        companion = poly.with_stem(poly.stem + "_lines")
        if companion in lines:
            ordered.append(companion)

    points = [
        f for f in sorted(source_dir.glob(POINT_PATTERN))
        if not _is_intermediate_polygon(f)
    ]
    return ordered + [p for p in points if p not in ordered]


def export_to_gpkg(fgbs: list[Path], gpkg_path: Path) -> None:
    if gpkg_path.exists():
        gpkg_path.unlink()
        print(f"Removed existing {gpkg_path.name}", flush=True)

    gpkg_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    print(f"Exporting {len(fgbs)} layer(s) → {gpkg_path.name}", flush=True)

    for i, fgb in enumerate(fgbs):
        t = time.time()
        layer_name = fgb.stem
        print(f"  [{i+1}/{len(fgbs)}] {fgb.name}", flush=True)
        print(f"          → layer '{layer_name}'...", flush=True)
        gdf = gpd.read_file(fgb, engine="pyogrio")
        mode = "w" if i == 0 else "a"
        gdf.to_file(gpkg_path, driver="GPKG", layer=layer_name, mode=mode, engine="pyogrio")
        print(f"          {len(gdf):,} features ({fmt(t)})", flush=True)

    size_mb = gpkg_path.stat().st_size / 1e6
    print(f"\nDone — {gpkg_path.name} ({size_mb:.1f} MB, {fmt(t0)})", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export FlatGeobuf files as feature class layers in a GeoPackage.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("source_dir", help="Directory containing FGB files")
    parser.add_argument("output_gpkg", help="Output GeoPackage path (e.g. boundaries.gpkg)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--pattern", metavar="GLOB",
        help="Glob pattern to match FGBs in source_dir (overrides default two-pattern logic)",
    )
    group.add_argument(
        "--files", nargs="+", metavar="FILE",
        help="Explicit list of FGB files to include (overrides source_dir glob)",
    )
    args = parser.parse_args()

    source_dir = Path(args.source_dir)
    gpkg_path = Path(args.output_gpkg)
    explicit = [Path(f) for f in args.files] if args.files else []

    fgbs = collect_fgbs(source_dir, args.pattern, explicit)

    if not fgbs:
        print(f"No FGB files found in {source_dir} — nothing to export.")
        return

    print(f"Found {len(fgbs)} FGB file(s):", flush=True)
    for f in fgbs:
        print(f"  {f.name}", flush=True)
    print(flush=True)

    export_to_gpkg(fgbs, gpkg_path)


if __name__ == "__main__":
    main()
