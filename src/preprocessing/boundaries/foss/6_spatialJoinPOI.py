#!/usr/bin/env python3
"""
Spatial join: assign pagename_X fields from admin boundaries to point features.

Points that intersect a polygon get their pagename_* fields populated.
Points outside all polygons retain existing values (NULL or pre-existing).

Uses the airesante-level dissolved FGB as the polygon source, since it carries
all four pagename fields (province, antenne, zonesante, airesante).

Requires: geopandas>=1.0, pyogrio, shapely>=2.0

Usage:
  python 6_spatialJoinPOI.py points.fgb polygons.fgb [output.fgb]

If output.fgb is omitted, writes to <points_stem>_pagename.fgb.
"""

import sys
import time
from pathlib import Path

import geopandas as gpd


def fmt(t0: float) -> str:
    s = time.time() - t0
    return f"{s/60:.1f}m" if s >= 60 else f"{s:.1f}s"


def spatial_join_poi(points_path: Path, poly_path: Path, out_path: Path) -> None:
    t0 = time.time()

    print(f"Reading points {points_path.name}...", flush=True)
    pts = gpd.read_file(points_path, engine="pyogrio")
    print(f"  {len(pts):,} points", flush=True)

    print(f"Reading polygons {poly_path.name}...", flush=True)
    polys = gpd.read_file(poly_path, engine="pyogrio")
    pagename_cols = [c for c in polys.columns if c.startswith("pagename_")]
    if not pagename_cols:
        raise ValueError(f"Polygon layer {poly_path.name} has no pagename_* columns")
    print(f"  {len(polys):,} polygons, pagename columns: {pagename_cols}", flush=True)

    # Ensure the point layer has the pagename columns (add as None if absent)
    for col in pagename_cols:
        if col not in pts.columns:
            pts[col] = None

    # Left spatial join — all points retained
    t = time.time()
    join_cols = pagename_cols + ["geometry"]
    joined = gpd.sjoin(
        pts,
        polys[join_cols],
        how="left",
        predicate="intersects",
    )
    print(f"  Spatial join done ({fmt(t)})", flush=True)

    # Points on polygon boundaries may match multiple polygons → keep first match only
    joined = joined[~joined.index.duplicated(keep="first")]

    # Overwrite pagename columns only where the join succeeded
    for col in pagename_cols:
        right_col = f"{col}_right"
        if right_col in joined.columns:
            mask = joined[right_col].notna()
            joined.loc[mask, col] = joined.loc[mask, right_col]

    # Drop sjoin artefacts (_left, _right, index_right)
    artefacts = [c for c in joined.columns
                 if c.endswith("_right") or c.endswith("_left") or c == "index_right"]
    joined = joined.drop(columns=artefacts, errors="ignore").reset_index(drop=True)

    intersected = joined[pagename_cols[0]].notna().sum()
    print(
        f"  {intersected:,} / {len(pts):,} points intersected a polygon "
        f"({len(pts) - intersected:,} unmatched)",
        flush=True,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    joined.to_file(out_path, driver="FlatGeobuf", engine="pyogrio")
    print(f"  Written {out_path.name} ({fmt(t0)})", flush=True)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    pts_path = Path(sys.argv[1])
    poly_path = Path(sys.argv[2])
    out_path = (
        Path(sys.argv[3])
        if len(sys.argv) > 3
        else pts_path.with_stem(pts_path.stem + "_pagename")
    )
    spatial_join_poi(pts_path, poly_path, out_path)
