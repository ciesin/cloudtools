#!/usr/bin/env python3
"""
Dissolve a pagename-enriched airesante FGB into four nested admin-level FGB files.

For each admin level (province → antenne → zonesante → airesante):
  - Groups by pagename_X
  - Unions geometry (ST_Union_Agg)
  - Preserves parent pagename fields via MIN()
  - Explodes MULTIPOLYGON → POLYGON (for QGIS single-part atlas pagination)
  - Recomputes Shape__Area and Shape__Length per part

Run 1_addPageName.py first to add pagename_* fields to the source FGB.

Small disconnected parts (lake islands, geometry fragments) are absorbed into the
nearest significant part by centroid distance, so they don't generate separate atlas
pages. Control the threshold with --min-part-fraction (default 0.01 = 1% of the
largest part's area).

Requires: duckdb>=1.0 (with spatial extension), geopandas>=1.0, shapely>=2.0, pyogrio

Usage:
  python 2_dissolveAdmin.py input.fgb output_dir/
      [--prefix GRID3_COD] [--version v8_0] [--date YYYYMMDD]
      [--min-part-fraction 0.01]

Output (example with defaults):
  output_dir/GRID3_COD_province_nested_v8_0_pagename_YYYYMMDD.fgb
  output_dir/GRID3_COD_antenne_nested_v8_0_pagename_YYYYMMDD.fgb
  output_dir/GRID3_COD_zonesante_nested_v8_0_pagename_YYYYMMDD.fgb
  output_dir/GRID3_COD_airesante_nested_v8_0_pagename_YYYYMMDD.fgb
"""

import argparse
import time
from datetime import datetime
from pathlib import Path

import warnings

import duckdb
import geopandas as gpd
from shapely import wkb


LEVELS = [
    {
        "level": "province",
        "pagename_field": "pagename_province",
        "own_fields": ["pays", "iso3", "province", "prov_uid"],
        "parent_pagenames": [],
    },
    {
        "level": "antenne",
        "pagename_field": "pagename_antenne",
        "own_fields": ["pays", "iso3", "province", "prov_uid", "antenne"],
        "parent_pagenames": ["pagename_province"],
    },
    {
        "level": "zonesante",
        "pagename_field": "pagename_zonesante",
        "own_fields": ["pays", "iso3", "province", "prov_uid", "antenne", "zonesante", "zs_uid"],
        "parent_pagenames": ["pagename_province", "pagename_antenne"],
    },
    {
        "level": "airesante",
        "pagename_field": "pagename_airesante",
        "own_fields": [
            "pays", "iso3", "province", "prov_uid",
            "antenne", "zonesante", "zs_uid",
            "airesante", "as_uid", "asnom_alt",
        ],
        "parent_pagenames": ["pagename_province", "pagename_antenne", "pagename_zonesante"],
    },
]

METADATA_FIELDS = ["date", "edit_par", "grid3id", "source_acronym", "sourceid"]


def fmt(t0: float) -> str:
    s = time.time() - t0
    return f"{s/60:.1f}m" if s >= 60 else f"{s:.1f}s"


def smart_explode(
    gdf: gpd.GeoDataFrame,
    min_part_fraction: float = 0.01,
) -> gpd.GeoDataFrame:
    """
    Explode MULTIPOLYGON rows to single-part rows, but absorb tiny parts
    (islands, lake fragments) into the nearest large part rather than giving
    them their own atlas page.

    A part is "tiny" if area < min_part_fraction * area_of_largest_part.
    Tiny parts are unioned into whichever large part has the nearest centroid,
    producing a MULTIPOLYGON that renders correctly on the same atlas page.
    """
    non_geom = [c for c in gdf.columns if c != "geometry"]
    rows: list[dict] = []
    absorbed_total = 0

    for _, row in gdf.iterrows():
        geom = row.geometry
        attrs = {c: row[c] for c in non_geom}

        if geom is None or geom.is_empty:
            continue

        if geom.geom_type == "Polygon":
            rows.append({"geometry": geom, **attrs})
            continue

        # Sort parts largest → smallest
        parts = sorted(geom.geoms, key=lambda p: p.area, reverse=True)
        threshold = parts[0].area * min_part_fraction

        large = [p for p in parts if p.area >= threshold]
        small = [p for p in parts if p.area < threshold]

        if not small:
            for part in large:
                rows.append({"geometry": part, **attrs})
            continue

        absorbed_total += len(small)
        # Absorb each tiny part into the nearest large part (centroid distance)
        accumulated = list(large)
        for spart in small:
            sc = spart.centroid
            nearest_i = min(range(len(accumulated)), key=lambda i: sc.distance(accumulated[i].centroid))
            accumulated[nearest_i] = accumulated[nearest_i].union(spart)

        for part in accumulated:
            rows.append({"geometry": part, **attrs})

    if absorbed_total:
        print(f"    absorbed {absorbed_total} tiny part(s) into nearest large part", flush=True)

    return gpd.GeoDataFrame(rows, geometry="geometry", crs=gdf.crs).reset_index(drop=True)


def dissolve_level(
    con: duckdb.DuckDBPyConnection,
    available_cols: set,
    level_cfg: dict,
    output_path: Path,
    min_part_fraction: float = 0.01,
) -> int:
    pagename = level_cfg["pagename_field"]
    own = [f for f in level_cfg["own_fields"] if f in available_cols]
    parents = [p for p in level_cfg["parent_pagenames"] if p in available_cols]
    meta = [f for f in METADATA_FIELDS if f in available_cols and f not in own]

    sel = [f'"{pagename}"']
    for f in parents:
        sel.append(f'MIN("{f}") AS "{f}"')
    for f in own + meta:
        sel.append(f'MIN("{f}") AS "{f}"')
    # Emit geometry as WKB for efficient in-memory transfer
    sel.append("ST_AsWKB(ST_Multi(ST_Union_Agg(geom))) AS geom_wkb")

    sql = f"SELECT {', '.join(sel)} FROM zones GROUP BY \"{pagename}\""

    t = time.time()
    print(f"  Dissolving by {pagename}...", flush=True)
    df = con.execute(sql).df()
    print(f"    {len(df):,} groups ({fmt(t)})", flush=True)

    t = time.time()
    df["geometry"] = df["geom_wkb"].apply(lambda b: wkb.loads(bytes(b)))
    df = df.drop(columns=["geom_wkb"])
    gdf = gpd.GeoDataFrame(df, geometry="geometry", crs="EPSG:4326")

    # Explode MULTIPOLYGON → single-part POLYGON, absorbing tiny parts (islands)
    gdf_exp = smart_explode(gdf, min_part_fraction=min_part_fraction)
    # Suppress geographic-CRS area warning — values are used only for relative ordering
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        gdf_exp["Shape__Area"] = gdf_exp.geometry.area
        gdf_exp["Shape__Length"] = gdf_exp.geometry.length

    output_path.parent.mkdir(parents=True, exist_ok=True)
    gdf_exp.to_file(output_path, driver="FlatGeobuf", engine="pyogrio")
    n = len(gdf_exp)
    print(f"    {n:,} atlas-page features → {output_path.name} ({fmt(t)})", flush=True)
    return n


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dissolve airesante FGB into one FGB per admin level.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input", help="Input FGB with pagename_* fields")
    parser.add_argument("output_dir", help="Directory to write dissolved FGBs")
    parser.add_argument("--prefix", default="GRID3_COD",
                        help="Output filename prefix (default: GRID3_COD)")
    parser.add_argument("--version", default="v8_0",
                        help="Version tag in output filenames (default: v8_0)")
    parser.add_argument("--date", default=None,
                        help="Date string YYYYMMDD (default: today)")
    parser.add_argument(
        "--min-part-fraction", type=float, default=0.01, metavar="F",
        help=(
            "Parts smaller than F × largest-part area are absorbed into the nearest "
            "large part (default: 0.01 = 1%%). Set 0.0 to disable (naive explode)."
        ),
    )
    args = parser.parse_args()

    date_str = args.date or datetime.now().strftime("%Y%m%d")
    src = Path(args.input)
    out_dir = Path(args.output_dir)

    t0 = time.time()
    print(f"Loading {src.name} into DuckDB...", flush=True)
    con = duckdb.connect()
    con.install_extension("spatial")
    con.load_extension("spatial")
    con.execute(f"CREATE TABLE zones AS SELECT * FROM ST_Read('{src}')")
    n_in = con.execute("SELECT COUNT(*) FROM zones").fetchone()[0]
    print(f"  {n_in:,} source features ({fmt(t0)})", flush=True)

    available_cols = {row[0] for row in con.execute("DESCRIBE zones").fetchall()}
    available_cols -= {"geom", "OGC_FID", "OBJECTID"}

    totals = []
    for level_cfg in LEVELS:
        pf = level_cfg["pagename_field"]
        if pf not in available_cols:
            print(f"\nSkipping {level_cfg['level']}: {pf} not found in source", flush=True)
            continue
        fname = (
            f"{args.prefix}_{level_cfg['level']}_nested_"
            f"{args.version}_pagename_{date_str}.fgb"
        )
        print(f"\n[{level_cfg['level'].upper()}]", flush=True)
        n = dissolve_level(
            con, available_cols, level_cfg, out_dir / fname,
            min_part_fraction=args.min_part_fraction,
        )
        totals.append((level_cfg["level"], n))

    con.close()
    print(f"\nSummary:", flush=True)
    for lvl, n in totals:
        print(f"  {lvl:12s} {n:>6,} features", flush=True)
    print(f"Total elapsed: {fmt(t0)}", flush=True)


if __name__ == "__main__":
    main()
