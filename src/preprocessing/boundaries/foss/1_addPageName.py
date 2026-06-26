#!/usr/bin/env python3
"""
Add pagename_X fields to an airesante-level FlatGeobuf.

Input FGB must contain: iso3, province, antenne, zonesante, airesante
Output: FGB with four new pagename_* columns:
  pagename_province   = clean(iso3_province)
  pagename_antenne    = clean(iso3_province_antenne)
  pagename_zonesante  = clean(iso3_province_antenne_zonesante)
  pagename_airesante  = clean(iso3_province_antenne_zonesante_airesante)

"clean" = NFD transliteration + space→hyphen + strip invalid chars + collapse hyphens

Requires: geopandas>=1.0, pyogrio

Usage: python 1_addPageName.py input.fgb [output.fgb]
  If output.fgb is omitted, input.fgb is overwritten in place.
"""

import re
import sys
import time
import unicodedata
from pathlib import Path

import geopandas as gpd


ADMIN_LEVELS = ["province", "antenne", "zonesante", "airesante"]


def fmt(t0: float) -> str:
    s = time.time() - t0
    return f"{s/60:.1f}m" if s >= 60 else f"{s:.1f}s"


def clean_pagename(text: str) -> str:
    nfd = unicodedata.normalize("NFD", text)
    ascii_text = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    cleaned = ascii_text.replace(" ", "-")
    cleaned = re.sub(r'[\\/:*?"<>|]', "-", cleaned)
    cleaned = re.sub(r"-+", "-", cleaned)
    return cleaned.strip("-")


def add_pagename_fields(src: Path, dst: Path) -> None:
    print(f"Reading {src.name}...", flush=True)
    t = time.time()
    gdf = gpd.read_file(src, engine="pyogrio")
    print(f"  {len(gdf):,} features ({fmt(t)})", flush=True)

    required = ["iso3"] + ADMIN_LEVELS
    missing = [c for c in required if c not in gdf.columns]
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    print("Computing pagename fields...", flush=True)
    for i, level in enumerate(ADMIN_LEVELS):
        parts = ["iso3"] + ADMIN_LEVELS[: i + 1]
        raw = gdf[parts].apply(lambda row: "_".join(str(v) for v in row), axis=1)
        gdf[f"pagename_{level}"] = raw.map(clean_pagename)
        print(f"  pagename_{level}: e.g. {gdf[f'pagename_{level}'].iloc[0]!r}", flush=True)

    print(f"Writing {dst.name}...", flush=True)
    t = time.time()
    gdf.to_file(dst, driver="FlatGeobuf", engine="pyogrio")
    print(f"  {len(gdf):,} features written ({fmt(t)})", flush=True)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src
    add_pagename_fields(src, dst)
