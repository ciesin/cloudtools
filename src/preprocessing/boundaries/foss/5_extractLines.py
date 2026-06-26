#!/usr/bin/env python3
"""
Extract polygon boundaries as line features from dissolved admin boundary FGBs.

For each *_nested_*_pagename_*.fgb, writes a companion *_lines.fgb alongside it
containing only:
  fid         — original feature row index (cross-reference back to the polygon layer)
  admin_level — admin level string (province / antenne / zonesante / airesante)
  geometry    — exterior boundary as LineString / MultiLineString

No other attributes are retained.  The line layer is intended for cartographic
styling of boundary lines in QGIS independent of polygon fill symbolisation.

Requires: geopandas>=1.0, pyogrio

Usage:
  python 5_extractLines.py dissolved_dir/ [--pattern "*_nested_*_pagename_*.fgb"]
  python 5_extractLines.py path/to/file.fgb          # single file
"""

import argparse
import time
from pathlib import Path

import geopandas as gpd


LEVEL_NAMES = ["province", "antenne", "zonesante", "airesante"]


def fmt(t0: float) -> str:
    s = time.time() - t0
    return f"{s/60:.1f}m" if s >= 60 else f"{s:.1f}s"


def detect_admin_level(stem: str) -> str | None:
    for lvl in LEVEL_NAMES:
        if f"_{lvl}_" in stem:
            return lvl
    return None


def extract_lines(path: Path) -> Path | None:
    if path.stem.endswith("_lines"):
        print(f"  Skipping {path.name} — already a lines file", flush=True)
        return None

    level = detect_admin_level(path.stem)
    if level is None:
        print(f"  Skipping {path.name} — cannot detect admin level", flush=True)
        return None

    t = time.time()
    print(f"  {path.name} ({level})...", flush=True)
    gdf = gpd.read_file(path, engine="pyogrio")

    lines = gpd.GeoDataFrame(
        {
            "fid": range(len(gdf)),
            "admin_level": level,
        },
        geometry=gdf.geometry.boundary,
        crs=gdf.crs,
    )

    out_path = path.with_stem(path.stem + "_lines")
    lines.to_file(out_path, driver="FlatGeobuf", engine="pyogrio")
    print(f"    {len(lines):,} line features → {out_path.name} ({fmt(t)})", flush=True)
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract polygon boundaries as companion line FGBs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "target",
        help="Directory of dissolved FGBs, or path to a single polygon FGB",
    )
    parser.add_argument(
        "--pattern", default="*_nested_*_pagename_*.fgb",
        help="Glob pattern when target is a directory "
             "(default: *_nested_*_pagename_*.fgb)",
    )
    args = parser.parse_args()

    target = Path(args.target)

    if target.is_file():
        fgbs = [target]
    else:
        fgbs = sorted(
            f for f in target.glob(args.pattern)
            if f.suffix == ".fgb" and not f.stem.endswith("_lines")
        )

    if not fgbs:
        print(f"No FGB files found at {args.target}")
        return

    print(f"Extracting lines from {len(fgbs)} FGB file(s)...", flush=True)
    for fgb in fgbs:
        extract_lines(fgb)
    print("Done", flush=True)


if __name__ == "__main__":
    main()
