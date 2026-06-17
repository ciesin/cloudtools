#!/usr/bin/env python3
"""
convertToCloudOptimized.py - Convert geospatial files to cloud-optimized formats

Conversions:
  .tif / .tiff  → Cloud Optimized GeoTIFF (COG) via gdal_translate
  .gpkg         → GeoParquet via ogr2ogr

COG parameters follow https://cogeo.org/ and https://guide.cloudnativegeo.org/:
  - LZW compression, 512×512 block tiles, auto overviews
  - Metadata-first internal layout for efficient HTTP range requests

GeoParquet parameters follow GeoParquet 1.1 best practices:
  - ZSTD compression (level 15), covering bbox struct, spatial sort
  - Row groups tuned to 100k rows for balanced query performance

Usage:
    # Single folder
    python convertToCloudOptimized.py --input-dir /path/to/dataset

    # Loop through subfolders
    python convertToCloudOptimized.py --input-dir /path/to/root --recursive

    # Specific format only
    python convertToCloudOptimized.py --input-dir /path --format cog
    python convertToCloudOptimized.py --input-dir /path --format parquet

    # Overwrite existing outputs
    python convertToCloudOptimized.py --input-dir /path --overwrite
"""

import sys
import gc
import time
import subprocess
import shutil
from pathlib import Path
from typing import Union, List, Tuple, Optional

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False


# ── COG creation options (gdal_translate -of COG) ─────────────────────────────
COG_CREATION_OPTIONS = [
    "COMPRESS=LZW",
    "BLOCKSIZE=512",           # 512×512 tiles for efficient HTTP range requests
    "OVERVIEW_LEVEL=AUTO",     # Auto-generate overviews (2x, 4x, 8x, ...)
    "OVERVIEWS=AUTO",
    "INTERLEAVE=PIXEL",
    "BIGTIFF=IF_SAFER",
]

# ── GeoParquet creation options (ogr2ogr -f Parquet) ──────────────────────────
PARQUET_CREATION_OPTIONS = [
    "COMPRESSION=ZSTD",
    "COMPRESSION_LEVEL=15",    # ZSTD-15: 3-5x faster decompression vs GZIP
    "WRITE_COVERING_BBOX=YES", # Bbox struct enables 10-100x faster spatial filter
    "ROW_GROUP_SIZE=100000",   # 100k rows ≈ 50-100MB compressed; good query balance
    "GEOMETRY_ENCODING=WKB",   # ISO WKB: maximum tool compatibility
    "SORT_BY_BBOX=YES",        # Spatial Hilbert ordering (GDAL 3.9+)
]

# Sidecar extensions to clean up alongside source TIFs
TIF_SIDECAR_EXTENSIONS = [".aux", ".aux.xml", ".tif.aux", ".tif.ovr", ".ovr", ".prj", ".xml"]


def _check_gdal_tools() -> Tuple[bool, str]:
    """Verify gdal_translate and ogr2ogr are on PATH."""
    for tool in ("gdal_translate", "ogr2ogr"):
        if shutil.which(tool) is None:
            return False, f"{tool} not found on PATH — install GDAL"
    return True, "OK"


def _check_parquet_driver() -> Tuple[bool, str]:
    """Verify the OGR Parquet driver is compiled into the installed GDAL."""
    try:
        result = subprocess.run(
            ["ogr2ogr", "--formats"],
            capture_output=True, text=True, check=False
        )
        combined = result.stdout + result.stderr
        if "Parquet" in combined or "parquet" in combined:
            return True, "OK"
        return False, (
            "OGR Parquet driver not available in this GDAL build. "
            "Install a GDAL package with Arrow/Parquet support "
            "(e.g. libgdal-arrow-parquet on Ubuntu, or conda-forge gdal)."
        )
    except FileNotFoundError:
        return False, "ogr2ogr not found on PATH"


def _run_command(cmd: List[str]) -> Tuple[bool, str]:
    """Run a subprocess command, return (success, stderr_or_error)."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False
        )
        if result.returncode != 0:
            return False, result.stderr.strip() or result.stdout.strip()
        return True, result.stderr.strip()
    except FileNotFoundError as e:
        return False, str(e)


def _cleanup_sidecars(tif_path: Path, verbose: bool) -> int:
    """Remove sidecar files associated with a TIF. Returns count removed."""
    removed = 0
    stem = tif_path.stem
    parent = tif_path.parent

    seen: set = set()
    candidates = []
    for ext in TIF_SIDECAR_EXTENSIONS:
        for candidate in (parent / f"{tif_path.name}{ext}", parent / f"{stem}{ext}"):
            if candidate not in seen:
                seen.add(candidate)
                candidates.append(candidate)

    for candidate in candidates:
        if candidate.exists() and candidate != tif_path:
            candidate.unlink()
            removed += 1
            if verbose:
                print(f"       Removed sidecar: {candidate.name}")
    return removed


def convert_tif_to_cog(
    input_path: Union[str, Path],
    output_path: Optional[Union[str, Path]] = None,
    overwrite: bool = False,
    cleanup_source: bool = False,
    verbose: bool = True
) -> Tuple[bool, str, Optional[Path]]:
    """
    Convert a GeoTIFF to Cloud Optimized GeoTIFF (COG).

    Uses gdal_translate with the COG driver. Sidecar files (.aux.xml, .ovr, etc.)
    are removed when cleanup_source=True since COG embeds overviews internally.

    Returns:
        Tuple of (success, message, output_path)
    """
    input_path = Path(input_path)

    if not input_path.exists():
        return False, f"Input not found: {input_path}", None

    if output_path is None:
        output_path = input_path.with_stem(input_path.stem + "_cog")
    else:
        output_path = Path(output_path)

    if output_path.exists() and not overwrite:
        if verbose:
            size_mb = output_path.stat().st_size / 1024 / 1024
            print(f" Skip {input_path.name} -> {output_path.name} ({size_mb:.1f} MB, exists)")
        return True, "Already exists", output_path

    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        start_time = time.time()

        if verbose:
            size_mb = input_path.stat().st_size / 1024 / 1024
            print(f"\n{'='*70}")
            print(f"COG: {input_path.name}  ({size_mb:.1f} MB)")
            print(f"{'='*70}")

        cmd = ["gdal_translate", "-of", "COG"]
        for opt in COG_CREATION_OPTIONS:
            cmd += ["-co", opt]
        cmd += [str(input_path), str(output_path)]

        if verbose:
            print(f"  Running gdal_translate...", end="", flush=True)

        ok, stderr = _run_command(cmd)

        if not ok:
            if output_path.exists():
                output_path.unlink()
            if verbose:
                print(f"\n  gdal_translate failed:\n     {stderr}")
            return False, stderr, None

        elapsed = time.time() - start_time
        in_mb = input_path.stat().st_size / 1024 / 1024
        out_mb = output_path.stat().st_size / 1024 / 1024
        ratio = ((in_mb - out_mb) / in_mb * 100) if in_mb > 0 else 0

        if verbose:
            print(f"\r   Complete in {elapsed:.1f}s")
            print(f"     Input:  {in_mb:.1f} MB  ->  Output: {out_mb:.1f} MB  ({ratio:+.1f}%)")

        if cleanup_source:
            sidecars = _cleanup_sidecars(input_path, verbose)
            input_path.unlink()
            if verbose:
                print(f"       Removed source ({in_mb:.1f} MB) + {sidecars} sidecar(s)")

        if verbose:
            print(f"{'='*70}\n")

        return True, f"COG: {out_mb:.1f} MB", output_path

    except Exception as e:
        if output_path.exists():
            output_path.unlink()
        return False, f"{type(e).__name__}: {e}", None


def convert_gpkg_to_parquet(
    input_path: Union[str, Path],
    output_path: Optional[Union[str, Path]] = None,
    layer: Optional[str] = None,
    overwrite: bool = False,
    cleanup_source: bool = False,
    verbose: bool = True
) -> Tuple[bool, str, Optional[Path]]:
    """
    Convert a GeoPackage to GeoParquet via ogr2ogr.

    Applies ZSTD-15 compression, covering bbox, spatial ordering, and
    100k row groups per GeoParquet 1.1 best practices.

    Args:
        layer: Specific layer to convert (default: first layer)

    Returns:
        Tuple of (success, message, output_path)
    """
    input_path = Path(input_path)

    if not input_path.exists():
        return False, f"Input not found: {input_path}", None

    if output_path is None:
        suffix = f"_{layer}" if layer else ""
        output_path = input_path.with_name(f"{input_path.stem}{suffix}.parquet")
    else:
        output_path = Path(output_path)

    if output_path.exists() and not overwrite:
        if verbose:
            size_mb = output_path.stat().st_size / 1024 / 1024
            print(f" Skip {input_path.name} -> {output_path.name} ({size_mb:.1f} MB, exists)")
        return True, "Already exists", output_path

    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        start_time = time.time()

        if verbose:
            size_mb = input_path.stat().st_size / 1024 / 1024
            print(f"\n{'='*70}")
            print(f"GeoParquet: {input_path.name}  ({size_mb:.1f} MB)")
            if layer:
                print(f"   Layer: {layer}")
            print(f"{'='*70}")

        cmd = ["ogr2ogr", "-f", "Parquet"]
        for opt in PARQUET_CREATION_OPTIONS:
            cmd += ["-lco", opt]
        if layer:
            cmd += ["-sql", f'SELECT * FROM "{layer}"', "-dialect", "OGRSQL"]

        # Reproject to WGS84 if needed (GeoParquet convention)
        cmd += ["-t_srs", "EPSG:4326"]
        cmd += [str(output_path), str(input_path)]

        if verbose:
            print(f"  Running ogr2ogr...", end="", flush=True)

        ok, stderr = _run_command(cmd)

        if not ok:
            if output_path.exists():
                output_path.unlink()
            if verbose:
                print(f"\n  ogr2ogr failed:\n     {stderr}")
            return False, stderr, None

        elapsed = time.time() - start_time
        in_mb = input_path.stat().st_size / 1024 / 1024
        out_mb = output_path.stat().st_size / 1024 / 1024
        ratio = ((in_mb - out_mb) / in_mb * 100) if in_mb > 0 else 0

        if verbose:
            print(f"\r   Complete in {elapsed:.1f}s")
            print(f"     Input:  {in_mb:.1f} MB  ->  Output: {out_mb:.1f} MB  ({ratio:+.1f}%)")

        if cleanup_source:
            input_path.unlink()
            if verbose:
                print(f"       Removed source ({in_mb:.1f} MB)")

        if verbose:
            print(f"{'='*70}\n")

        return True, f"GeoParquet: {out_mb:.1f} MB", output_path

    except Exception as e:
        if output_path.exists():
            output_path.unlink()
        return False, f"{type(e).__name__}: {e}", None


def _get_gpkg_layers(gpkg_path: Path) -> List[str]:
    """List layers in a GeoPackage via ogrinfo."""
    try:
        result = subprocess.run(
            ["ogrinfo", "-al", "-so", str(gpkg_path)],
            capture_output=True, text=True, check=False
        )
        layers = []
        for line in result.stdout.splitlines():
            if line.startswith("Layer name:"):
                layers.append(line.split(":", 1)[1].strip())
        return layers
    except Exception:
        return []


def batch_convert_directory(
    input_dir: Union[str, Path],
    output_dir: Optional[Union[str, Path]] = None,
    recursive: bool = False,
    fmt: Optional[str] = None,
    overwrite: bool = False,
    verbose: bool = True,
    cleanup_source: bool = False,
    split_gpkg_layers: bool = False
) -> dict:
    """
    Convert all TIFFs and/or GeoPackages in a directory to cloud-optimized formats.

    Args:
        input_dir:         Directory (or root of subdirectories) to process
        output_dir:        Output root (mirrors input structure; default: same as input)
        recursive:         If True, process each immediate subdirectory independently
        fmt:               Restrict conversion: 'cog' | 'parquet' | None (both)
        overwrite:         Overwrite existing outputs
        verbose:           Print progress
        cleanup_source:    Remove source files after successful conversion
        split_gpkg_layers: Write each GPKG layer to a separate .parquet file

    Returns:
        dict with summary statistics
    """
    input_dir = Path(input_dir)

    ok, msg = _check_gdal_tools()
    if not ok:
        print(f" {msg}")
        return {"success": False, "message": msg}

    if fmt in (None, "parquet"):
        ok, msg = _check_parquet_driver()
        if not ok:
            print(f" {msg}")
            if fmt == "parquet":
                return {"success": False, "message": msg}
            print("  Skipping GeoParquet conversions; continuing with COG only.")
            fmt = "cog"

    # Build list of (input_folder, output_folder) pairs
    if recursive:
        subdirs = sorted([d for d in input_dir.iterdir() if d.is_dir()])
        if not subdirs:
            subdirs = [input_dir]
    else:
        subdirs = [input_dir]

    results = {
        "success": True,
        "total_files": 0,
        "converted": 0,
        "skipped": 0,
        "errors": [],
        "output_files": [],
    }

    for folder in subdirs:
        out_root = Path(output_dir) / folder.name if output_dir else folder

        tif_files: List[Path] = []
        gpkg_files: List[Path] = []

        if fmt in (None, "cog"):
            tif_files = sorted(
                f for f in folder.glob("*.tif")
                if "_cog" not in f.stem
            ) + sorted(
                f for f in folder.glob("*.tiff")
                if "_cog" not in f.stem
            )

        if fmt in (None, "parquet"):
            gpkg_files = sorted(folder.glob("*.gpkg"))

        all_files = tif_files + gpkg_files
        results["total_files"] += len(all_files)

        if not all_files:
            if verbose and len(subdirs) == 1:
                print(f"No convertible files found in {folder}")
            continue

        if verbose:
            print(f"\n{'='*70}")
            print(f"Folder: {folder.name}")
            print(f"   TIFs:  {len(tif_files)}   GPKGs: {len(gpkg_files)}")
            print(f"{'='*70}")

        iterator = tqdm(all_files, desc=folder.name) if (HAS_TQDM and len(all_files) > 1 and verbose) else all_files

        for f in iterator:
            ext = f.suffix.lower()

            if ext in (".tif", ".tiff"):
                out_path = out_root / f.with_stem(f.stem + "_cog").name
                success, msg, out = convert_tif_to_cog(
                    f, out_path, overwrite=overwrite,
                    cleanup_source=cleanup_source,
                    verbose=verbose and len(all_files) == 1
                )
                _record(results, f, out, success, msg)

            elif ext == ".gpkg":
                if split_gpkg_layers:
                    layers = _get_gpkg_layers(f)
                    if not layers:
                        layers = [None]
                else:
                    layers = [None]

                for lyr in layers:
                    suffix = f"_{lyr}" if lyr else ""
                    out_path = out_root / f"{f.stem}{suffix}.parquet"
                    success, msg, out = convert_gpkg_to_parquet(
                        f, out_path, layer=lyr,
                        overwrite=overwrite,
                        cleanup_source=cleanup_source and lyr == layers[-1],
                        verbose=verbose and len(all_files) == 1
                    )
                    _record(results, f, out, success, msg)

        gc.collect()

    if verbose:
        _print_summary(results)

    return results


def _record(results: dict, src: Path, out: Optional[Path], success: bool, msg: str):
    if success:
        if "Already exists" in msg:
            results["skipped"] += 1
        else:
            results["converted"] += 1
            if out:
                results["output_files"].append(out)
    else:
        results["errors"].append({"file": src.name, "error": msg})
        results["success"] = False


def _print_summary(results: dict):
    print(f"\n{'='*70}")
    print(f" SUMMARY")
    print(f"{'='*70}")
    print(f"   Total:     {results['total_files']}")
    print(f"    Done:   {results['converted']}")
    print(f"     Skip:   {results['skipped']}")
    print(f"   Errors: {len(results['errors'])}")
    if results["output_files"]:
        total_mb = sum(f.stat().st_size for f in results["output_files"] if f.exists()) / 1024 / 1024
        print(f"    Output: {total_mb:.1f} MB")
    if results["errors"]:
        print(f"\n ERRORS:")
        for e in results["errors"]:
            print(f"   - {e['file']}: {e['error']}")
    print(f"{'='*70}\n")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Convert TIFFs -> COG GeoTIFF and GeoPackages -> GeoParquet"
    )
    parser.add_argument("--input-dir", required=True,
                        help="Input directory (or root directory with --recursive)")
    parser.add_argument("--output-dir", default=None,
                        help="Output directory root (default: same as input)")
    parser.add_argument("--recursive", action="store_true",
                        help="Process each immediate subdirectory of --input-dir")
    parser.add_argument("--format", choices=["cog", "parquet"], default=None,
                        help="Limit to one output format (default: both)")
    parser.add_argument("--overwrite", action="store_true",
                        help="Overwrite existing output files")
    parser.add_argument("--cleanup", action="store_true",
                        help="Remove source files after successful conversion")
    parser.add_argument("--split-layers", action="store_true",
                        help="Write each GPKG layer as a separate .parquet file")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress progress output")

    args = parser.parse_args()

    results = batch_convert_directory(
        input_dir=args.input_dir,
        output_dir=args.output_dir,
        recursive=args.recursive,
        fmt=args.format,
        overwrite=args.overwrite,
        verbose=not args.quiet,
        cleanup_source=args.cleanup,
        split_gpkg_layers=args.split_layers
    )

    sys.exit(0 if results.get("success") else 1)


if __name__ == "__main__":
    main()
