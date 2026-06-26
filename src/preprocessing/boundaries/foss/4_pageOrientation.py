#!/usr/bin/env python3
"""
Add pageOrientation (PORTRAIT or LANDSCAPE) to dissolved admin boundary FGBs.

For each pagename_X group, computes the union of all polygon parts, then finds
the minimum rotated rectangle (MBR). The long-axis angle of the MBR determines:

  PORTRAIT  — long axis within 45° of north/south  (angle_from_north < 45 OR >= 135)
  LANDSCAPE — long axis within 45° of east/west    (45 <= angle_from_north < 135)

This matches the ArcGIS Pro MinimumBoundingGeometry + MBG_Fields orientation logic:
orientation is computed per pagename GROUP (the whole atlas page extent), not per part.

The pagename_X field is auto-detected from the filename.
Rewrites each FGB in-place.

Requires: geopandas>=1.0, shapely>=2.0, numpy, pyogrio

Usage:
  python 4_pageOrientation.py dissolved_dir/ [--pattern "*_nested_*"]
  python 4_pageOrientation.py path/to/file.fgb   # single file
"""

import argparse
import time
from pathlib import Path

import geopandas as gpd
import numpy as np


LEVEL_NAMES = ["province", "antenne", "zonesante", "airesante"]


def fmt(t0: float) -> str:
    s = time.time() - t0
    return f"{s/60:.1f}m" if s >= 60 else f"{s:.1f}s"


def detect_pagename_field(stem: str) -> str | None:
    for lvl in LEVEL_NAMES:
        if f"_{lvl}_" in stem:
            return f"pagename_{lvl}"
    return None


def compute_orientation(geom_union) -> str:
    if geom_union is None or geom_union.is_empty:
        return "LANDSCAPE"

    mbr = geom_union.minimum_rotated_rectangle
    coords = list(mbr.exterior.coords)[:4]

    e0 = (coords[1][0] - coords[0][0], coords[1][1] - coords[0][1])
    e1 = (coords[2][0] - coords[1][0], coords[2][1] - coords[1][1])
    long_edge = e0 if np.hypot(*e0) >= np.hypot(*e1) else e1

    # atan2 gives angle from east, counterclockwise, in (-180, 180]
    # Normalise to [0, 180) (axis direction, not vector direction)
    angle_from_east = np.degrees(np.arctan2(long_edge[1], long_edge[0])) % 180
    # Convert to angle from north, clockwise, in [0, 180)
    angle_from_north = (90.0 - angle_from_east) % 180

    # PORTRAIT: long axis near N–S; LANDSCAPE: near E–W
    return "PORTRAIT" if (angle_from_north < 45 or angle_from_north >= 135) else "LANDSCAPE"


def add_orientation(path: Path) -> None:
    pagename = detect_pagename_field(path.stem)
    if pagename is None:
        print(f"  Skipping {path.name} — cannot detect admin level from filename", flush=True)
        return

    t = time.time()
    print(f"  {path.name} ({pagename})...", flush=True)
    gdf = gpd.read_file(path, engine="pyogrio")

    if pagename not in gdf.columns:
        print(f"    ERROR: {pagename} not found in columns; skipping", flush=True)
        return

    # One MBR per pagename group (covering all disconnected parts together)
    group_unions = gdf.groupby(pagename, sort=False)["geometry"].apply(
        lambda geoms: geoms.union_all()
    )
    orientations = group_unions.map(compute_orientation)
    gdf["pageOrientation"] = gdf[pagename].map(orientations)

    portrait = (gdf["pageOrientation"] == "PORTRAIT").sum()
    landscape = (gdf["pageOrientation"] == "LANDSCAPE").sum()
    null_count = gdf["pageOrientation"].isna().sum()
    print(f"    PORTRAIT={portrait}, LANDSCAPE={landscape}, NULL={null_count}", flush=True)

    gdf.to_file(path, driver="FlatGeobuf", engine="pyogrio")
    print(f"    Written ({fmt(t)})", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add pageOrientation field to dissolved admin boundary FGBs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "target",
        help="Directory of dissolved FGBs, or path to a single FGB",
    )
    parser.add_argument(
        "--pattern", default="*_nested_*",
        help="Glob pattern when target is a directory (default: *_nested_*)",
    )
    args = parser.parse_args()

    target = Path(args.target)

    if target.is_file():
        fgbs = [target]
    else:
        fgbs = sorted(f for f in target.glob(args.pattern) if f.suffix == ".fgb")

    if not fgbs:
        print(f"No FGB files found at {args.target}")
        return

    print(f"Computing orientation for {len(fgbs)} FGB file(s)...", flush=True)
    for fgb in fgbs:
        add_orientation(fgb)
    print("Done", flush=True)


if __name__ == "__main__":
    main()
