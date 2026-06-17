#!/usr/bin/env python3
"""
raster_pmtiles.py - Convert a single-band raster to COG + PMTiles for web mapping

Pipeline:
  1. Expand single-band -> 3-band RGB GeoTIFF
     - If band has a color table: gdal_translate -expand rgb
     - Otherwise (grayscale/continuous): duplicate band 1 to all three channels
  2. Convert RGB GeoTIFF -> Cloud Optimized GeoTIFF (COG)
  3. Convert COG -> PMTiles archive via rio-pmtiles

Usage:
    python raster_pmtiles.py --input /path/to/single_band.tif
    python raster_pmtiles.py --input raster.tif --format WEBP --tile-size 512
    python raster_pmtiles.py --input raster.tif --zoom-levels 0..12 --resampling nearest

Dependencies:
    GDAL (gdal_translate, gdalinfo)
    rio-pmtiles  (pip install rio-pmtiles)
"""

import sys
import subprocess
import shutil
import time
from pathlib import Path
from typing import Optional, Tuple


# ── COG creation options (mirrors convertToCloudOptimized.py) ─────────────────
COG_OPTIONS = [
    "COMPRESS=LZW",
    "BLOCKSIZE=512",
    "OVERVIEW_LEVEL=AUTO",
    "OVERVIEWS=AUTO",
    "INTERLEAVE=PIXEL",
    "BIGTIFF=IF_SAFER",
]


def _run(cmd: list) -> Tuple[bool, str]:
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return False, result.stderr.strip() or result.stdout.strip()
    return True, result.stderr.strip()


def _has_color_table(input_path: Path) -> bool:
    """Return True if band 1 carries a color table (paletted/indexed raster)."""
    result = subprocess.run(
        ["gdalinfo", str(input_path)],
        capture_output=True, text=True, check=False,
    )
    return "Color Table" in result.stdout


def _get_band_count(path: Path) -> int:
    """Return the number of raster bands via gdalinfo, or 0 on failure."""
    result = subprocess.run(
        ["gdalinfo", str(path)],
        capture_output=True, text=True, check=False,
    )
    count = 0
    for line in result.stdout.splitlines():
        if line.strip().startswith("Band ") and "Block=" in line:
            count += 1
    return count


def expand_to_rgb(
    input_path: Path,
    output_path: Path,
    overwrite: bool = False,
    verbose: bool = True,
) -> Tuple[bool, str, Optional[Path]]:
    """
    Produce a 3-band RGB GeoTIFF from a single-band input.

    - Paletted rasters (have a color table): gdal_translate -expand rgb
    - Continuous/grayscale rasters (no color table): duplicate band 1 -> R G B
      This avoids the "band 1 has no color table" error that -expand rgb raises.
    """
    if output_path.exists() and not overwrite:
        if verbose:
            print(f"  Skip RGB expand (exists): {output_path.name}")
        return True, "Already exists", output_path

    output_path.parent.mkdir(parents=True, exist_ok=True)

    if _has_color_table(input_path):
        if verbose:
            print("  Color table detected — using -expand rgb")
        cmd = ["gdal_translate", "-expand", "rgb",
               str(input_path), str(output_path)]
    else:
        if verbose:
            print("  No color table — duplicating band 1 into RGB channels")
        cmd = ["gdal_translate", "-b", "1", "-b", "1", "-b", "1",
               str(input_path), str(output_path)]

    ok, err = _run(cmd)
    if not ok:
        if output_path.exists():
            output_path.unlink()
        return False, f"RGB expand failed: {err}", None

    return True, "OK", output_path


def convert_to_cog(
    input_path: Path,
    output_path: Path,
    overwrite: bool = False,
    verbose: bool = True,
) -> Tuple[bool, str, Optional[Path]]:
    """Convert a GeoTIFF to Cloud Optimized GeoTIFF via gdal_translate."""
    if output_path.exists() and not overwrite:
        if verbose:
            print(f"  Skip COG (exists): {output_path.name}")
        return True, "Already exists", output_path

    output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = ["gdal_translate", "-of", "COG"]
    for opt in COG_OPTIONS:
        cmd += ["-co", opt]
    cmd += [str(input_path), str(output_path)]

    ok, err = _run(cmd)
    if not ok:
        if output_path.exists():
            output_path.unlink()
        return False, f"COG conversion failed: {err}", None

    return True, "OK", output_path


def convert_to_pmtiles(
    input_path: Path,
    output_path: Path,
    fmt: str = "PNG",
    tile_size: int = 512,
    resampling: str = "bilinear",
    zoom_levels: Optional[str] = None,
    overwrite: bool = False,
    verbose: bool = True,
) -> Tuple[bool, str, Optional[Path]]:
    """Convert a COG to a PMTiles archive via rio-pmtiles."""
    if output_path.exists() and not overwrite:
        if verbose:
            print(f"  Skip PMTiles (exists): {output_path.name}")
        return True, "Already exists", output_path

    output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "rio", "pmtiles",
        str(input_path), str(output_path),
        "--format", fmt.upper(),
        "--tile-size", str(tile_size),
        "--resampling", resampling,
    ]
    if zoom_levels:
        cmd += ["--zoom-levels", zoom_levels]
    if not verbose:
        cmd.append("--silent")

    ok, err = _run(cmd)
    if not ok:
        if output_path.exists():
            output_path.unlink()
        return False, f"PMTiles conversion failed: {err}", None

    return True, "OK", output_path


def process_raster(
    input_path: Path,
    output_dir: Optional[Path] = None,
    fmt: str = "PNG",
    tile_size: int = 512,
    resampling: str = "bilinear",
    zoom_levels: Optional[str] = None,
    overwrite: bool = False,
    keep_intermediates: bool = False,
    verbose: bool = True,
) -> dict:
    """
    Full pipeline: single-band raster -> RGB GeoTIFF -> COG -> PMTiles.

    Returns a dict with keys: success, cog_path, pmtiles_path, errors.
    """
    out_dir = output_dir or input_path.parent
    stem = input_path.stem

    rgb_path = out_dir / f"{stem}_rgb.tif"
    cog_path = out_dir / f"{stem}_cog.tif"
    pmtiles_path = out_dir / f"{stem}.pmtiles"

    results = {"success": False, "cog_path": None, "pmtiles_path": None, "errors": []}

    if verbose:
        size_mb = input_path.stat().st_size / 1024 ** 2
        print(f"\n{'='*70}")
        print(f"Input:  {input_path.name}  ({size_mb:.1f} MB)")
        print(f"Output: {out_dir}")
        print(f"{'='*70}")

    # Step 1: Expand to RGB
    if verbose:
        print("\n[1/3] Expanding to RGB...")
    t = time.time()
    ok, msg, _ = expand_to_rgb(input_path, rgb_path, overwrite=overwrite, verbose=verbose)
    if not ok:
        results["errors"].append(msg)
        return results
    if verbose and "Already exists" not in msg:
        print(f"      Done in {time.time()-t:.1f}s  ({rgb_path.stat().st_size/1024**2:.1f} MB)")

    # Step 2: Convert RGB to COG
    if verbose:
        print("\n[2/3] Converting to COG...")
    t = time.time()
    ok, msg, cog_out = convert_to_cog(rgb_path, cog_path, overwrite=overwrite, verbose=verbose)
    if not ok:
        results["errors"].append(msg)
        if not keep_intermediates:
            _safe_remove(rgb_path)
        return results
    if verbose and "Already exists" not in msg:
        print(f"      Done in {time.time()-t:.1f}s  ({cog_path.stat().st_size/1024**2:.1f} MB)")
    results["cog_path"] = cog_out

    # Verify the COG has 3 bands before handing it to rio-pmtiles.
    # A stale single-band _cog.tif from a previous run will cause a cryptic
    # WEBP/JPEG "doesn't support 1 bands" error deep inside the worker process.
    bands = _get_band_count(cog_path)
    if bands != 3:
        err = (
            f"COG has {bands} band(s), expected 3. "
            f"A stale {cog_path.name} may exist from a previous run — "
            f"re-run with --overwrite to regenerate it."
        )
        results["errors"].append(err)
        if not keep_intermediates:
            _safe_remove(rgb_path)
        return results

    # Step 3: Convert COG to PMTiles
    if verbose:
        print(f"\n[3/3] Converting to PMTiles (format={fmt}, tile={tile_size}px)...")
    t = time.time()
    ok, msg, pmtiles_out = convert_to_pmtiles(
        cog_path, pmtiles_path,
        fmt=fmt, tile_size=tile_size,
        resampling=resampling, zoom_levels=zoom_levels,
        overwrite=overwrite, verbose=verbose,
    )
    if not ok:
        results["errors"].append(msg)
        if not keep_intermediates:
            _safe_remove(rgb_path)
        return results
    if verbose and "Already exists" not in msg:
        print(f"      Done in {time.time()-t:.1f}s  ({pmtiles_path.stat().st_size/1024**2:.1f} MB)")

    results["pmtiles_path"] = pmtiles_out
    results["success"] = True

    if not keep_intermediates:
        _safe_remove(rgb_path)
        if verbose:
            print(f"\n  Removed intermediate: {rgb_path.name}")

    if verbose:
        print(f"\n{'='*70}")
        print(f"  COG:     {cog_path}")
        print(f"  PMTiles: {pmtiles_path}")
        print(f"{'='*70}\n")

    return results


def _safe_remove(path: Optional[Path]):
    if path and path.exists():
        path.unlink()


def _check_deps() -> Tuple[bool, str]:
    for tool in ("gdal_translate", "gdalinfo"):
        if shutil.which(tool) is None:
            return False, f"{tool} not found — install GDAL"
    if shutil.which("rio") is None:
        return False, "rio not found — pip install rio-pmtiles"
    return True, "OK"


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Convert single-band raster -> COG GeoTIFF + PMTiles"
    )
    parser.add_argument("--input", "-i", required=True,
                        help="Input raster path (single-band GeoTIFF)")
    parser.add_argument("--output-dir", "-o", default=None,
                        help="Output directory (default: same as input)")
    parser.add_argument("--format", "-f", choices=["PNG", "JPEG", "WEBP"], default="PNG",
                        help="PMTiles tile format — PNG: lossless, WEBP: smallest (default: PNG)")
    parser.add_argument("--tile-size", type=int, default=512,
                        help="Tile size in pixels, 512 recommended for MapLibre GL (default: 512)")
    parser.add_argument("--resampling",
                        choices=["bilinear", "nearest", "average", "lanczos"],
                        default="bilinear",
                        help="Resampling method — use 'nearest' for categorical data (default: bilinear)")
    parser.add_argument("--zoom-levels", default=None,
                        help="Zoom range, e.g. '0..12' (default: auto from input resolution)")
    parser.add_argument("--overwrite", action="store_true",
                        help="Overwrite existing output files")
    parser.add_argument("--keep-intermediates", action="store_true",
                        help="Keep intermediate _rgb.tif file (COG is always kept)")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress progress output")

    args = parser.parse_args()

    ok, msg = _check_deps()
    if not ok:
        print(f"Error: {msg}")
        sys.exit(1)

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: Input not found: {input_path}")
        sys.exit(1)

    output_dir = Path(args.output_dir) if args.output_dir else None

    results = process_raster(
        input_path=input_path,
        output_dir=output_dir,
        fmt=args.format,
        tile_size=args.tile_size,
        resampling=args.resampling,
        zoom_levels=args.zoom_levels,
        overwrite=args.overwrite,
        keep_intermediates=args.keep_intermediates,
        verbose=not args.quiet,
    )

    if not results["success"]:
        for err in results["errors"]:
            print(f"Error: {err}")
        sys.exit(1)


if __name__ == "__main__":
    main()
