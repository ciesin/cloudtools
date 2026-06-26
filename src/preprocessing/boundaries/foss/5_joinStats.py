#!/usr/bin/env python3
"""
Join summary statistics to a dissolved admin boundary FGB.

Joins on the pagename field. Statistics file can be CSV or GeoParquet.
Columns already present in the polygon FGB are skipped (no overwrite).

Requires: duckdb>=1.0 (with spatial extension)

Usage:
  python 5_joinStats.py polygon.fgb stats.csv pagename_field [output.fgb]
  python 5_joinStats.py polygon.fgb stats.parquet pagename_field [output.fgb]

If output.fgb is omitted, writes to polygon_withStats.fgb beside the input.
"""

import sys
import time
from pathlib import Path

import duckdb


def fmt(t0: float) -> str:
    s = time.time() - t0
    return f"{s/60:.1f}m" if s >= 60 else f"{s:.1f}s"


def join_stats(
    poly_path: Path, stats_path: Path, join_field: str, out_path: Path
) -> None:
    t0 = time.time()
    con = duckdb.connect()
    con.install_extension("spatial")
    con.load_extension("spatial")

    print(f"Loading polygons from {poly_path.name}...", flush=True)
    con.execute(f"CREATE TABLE polys AS SELECT * FROM ST_Read('{poly_path}')")
    n_poly = con.execute("SELECT COUNT(*) FROM polys").fetchone()[0]
    print(f"  {n_poly:,} features", flush=True)

    print(f"Loading stats from {stats_path.name}...", flush=True)
    ext = stats_path.suffix.lower()
    if ext == ".csv":
        con.execute(f"CREATE TABLE stats AS SELECT * FROM read_csv_auto('{stats_path}')")
    elif ext in (".parquet", ".gpq", ".geoparquet"):
        con.execute(f"CREATE TABLE stats AS SELECT * FROM read_parquet('{stats_path}')")
    else:
        raise ValueError(f"Unsupported stats format: {ext!r} — use .csv or .parquet")

    poly_cols = {r[0] for r in con.execute("DESCRIBE polys").fetchall()}
    stats_cols = {r[0] for r in con.execute("DESCRIBE stats").fetchall()}

    # Only join columns not already in the polygon layer
    new_cols = stats_cols - {join_field} - poly_cols
    if not new_cols:
        print("  No new columns to join; stats table duplicates existing polygon fields")
        con.close()
        return

    print(f"  Joining {len(new_cols)} new column(s): {sorted(new_cols)}", flush=True)
    stats_sel = ", ".join(f's."{c}"' for c in sorted(new_cols))

    con.execute(f"""
        CREATE TABLE joined AS
        SELECT p.*, {stats_sel}
        FROM polys p
        LEFT JOIN stats s ON p."{join_field}" = s."{join_field}"
    """)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"""
        COPY joined TO '{out_path}'
        WITH (FORMAT GDAL, DRIVER 'FlatGeobuf', SRS 'EPSG:4326',
              LAYER_CREATION_OPTIONS 'SPATIAL_INDEX=YES')
    """)
    con.close()

    print(f"  Written {out_path.name} ({fmt(t0)})", flush=True)


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)

    poly = Path(sys.argv[1])
    stats = Path(sys.argv[2])
    field = sys.argv[3]
    out = (
        Path(sys.argv[4])
        if len(sys.argv) > 4
        else poly.with_stem(poly.stem + "_withStats")
    )
    join_stats(poly, stats, field, out)
