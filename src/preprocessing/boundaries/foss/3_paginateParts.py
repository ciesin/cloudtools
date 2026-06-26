#!/usr/bin/env python3
"""
Add pageNum and pageTotal fields to dissolved admin boundary FGBs.

For each pagename_X group:
  pageNum   = rank of this polygon by area (1 = largest part)
  pageTotal = total number of polygon parts for this pagename

The pagename_X field is auto-detected from the filename
(looks for _province_, _antenne_, _zonesante_, or _airesante_).

Rewrites each FGB in-place (via a temp file).

Requires: duckdb>=1.0 (with spatial extension)

Usage:
  python 3_paginateParts.py dissolved_dir/ [--pattern "*_nested_*"]
  python 3_paginateParts.py path/to/file.fgb   # single file
"""

import argparse
import time
from pathlib import Path

import duckdb


LEVEL_NAMES = ["province", "antenne", "zonesante", "airesante"]


def fmt(t0: float) -> str:
    s = time.time() - t0
    return f"{s/60:.1f}m" if s >= 60 else f"{s:.1f}s"


def detect_pagename_field(stem: str) -> str | None:
    for lvl in LEVEL_NAMES:
        if f"_{lvl}_" in stem:
            return f"pagename_{lvl}"
    return None


def paginate_fgb(path: Path) -> None:
    pagename = detect_pagename_field(path.stem)
    if pagename is None:
        print(f"  Skipping {path.name} — cannot detect admin level from filename", flush=True)
        return

    t = time.time()
    print(f"  {path.name} ({pagename})...", flush=True)
    tmp = path.with_suffix(".paginate_tmp.fgb")

    con = duckdb.connect()
    con.install_extension("spatial")
    con.load_extension("spatial")

    existing_cols = [r[0] for r in con.execute(f"DESCRIBE (SELECT * FROM ST_Read('{path}'))").fetchall()]
    n = con.execute(f"SELECT COUNT(*) FROM ST_Read('{path}')").fetchone()[0]
    print(f"    {n:,} features", flush=True)

    # Only EXCLUDE pageNum/pageTotal if they already exist (re-run safety)
    drop = [c for c in ("pageNum", "pageTotal") if c in existing_cols]
    exclude_clause = f"EXCLUDE ({', '.join(drop)})" if drop else ""

    con.execute(f"""
        COPY (
            SELECT
                * {exclude_clause},
                ROW_NUMBER() OVER (
                    PARTITION BY "{pagename}"
                    ORDER BY "Shape__Area" DESC
                )::INTEGER AS pageNum,
                COUNT(*) OVER (
                    PARTITION BY "{pagename}"
                )::INTEGER AS pageTotal
            FROM ST_Read('{path}')
        ) TO '{tmp}'
        WITH (FORMAT GDAL, DRIVER 'FlatGeobuf', SRS 'EPSG:4326',
              LAYER_CREATION_OPTIONS 'SPATIAL_INDEX=YES')
    """)
    con.close()

    tmp.replace(path)
    print(f"    Done ({fmt(t)})", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add pageNum/pageTotal to dissolved admin boundary FGBs.",
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

    print(f"Paginating {len(fgbs)} FGB file(s)...", flush=True)
    for fgb in fgbs:
        paginate_fgb(fgb)
    print("Done", flush=True)


if __name__ == "__main__":
    main()
