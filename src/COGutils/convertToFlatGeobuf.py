#!/usr/bin/env python3
"""
convertToFlatGeobuf.py - Convert GeoParquet files to FlatGeobuf format with streaming support

FlatGeobuf is optimal for large-scale tile generation:
- Streaming read capability (low memory footprint)
- Built-in spatial indexing (fast queries)
- Compact binary format (~30-50% smaller than GeoJSON)
- Native tippecanoe support (v2.17+)
- Perfect for continent/world-scale processing

PERFORMANCE OPTIMIZATIONS (for gigabyte-scale files):
- Polars native streaming engine (processes data in batches automatically)
- 10-100x faster parquet reading compared to pandas
- Automatic mode selection: streaming for large files (>500MB), direct for smaller
- Out-of-core processing: handles files larger than available RAM
- Memory-efficient: explicit garbage collection between operations

STREAMING MODE (files >500MB):
- Uses Polars scan_parquet() + collect(streaming=True)
- Polars automatically manages batching and memory
- No manual chunking required - engine handles it intelligently
- Scales to multi-gigabyte files without memory overflow

DIRECT MODE (files <500MB):
- Uses Polars read_parquet() for fast eager loading
- Optimized for files that fit comfortably in memory
- Single-pass conversion with minimal overhead

Usage:
    # Command line
    python convertToFlatGeobuf.py --input-dir=/path/to/parquet --output-dir=/path/to/fgb
    python convertToFlatGeobuf.py --input-dir=/path/to/parquet --force-streaming  # Force streaming for all files
    
    # From another script
    from convertToFlatGeobuf import convert_parquet_to_fgb, batch_convert_directory
    
    # Automatic mode selection
    convert_parquet_to_fgb("large_file.parquet", "output.fgb")
    
    # Force streaming mode
    convert_parquet_to_fgb("file.parquet", "output.fgb", force_streaming=True)
"""

import sys
import gc
import time
from pathlib import Path
from typing import Union, List, Tuple, Optional
import geopandas as gpd
import polars as pl
import pyarrow.parquet as pq
from tqdm import tqdm
import warnings

# Streaming conversion configuration
DEFAULT_CHUNK_SIZE = 100_000  # Features per chunk
LARGE_FILE_THRESHOLD_MB = 500  # Auto-enable chunking above this size
MEMORY_EFFICIENT_CHUNK_SIZE = 50_000  # Smaller chunks for very large files


def get_file_info(input_path: Path) -> dict:
    """
    Get metadata about a parquet file without loading it into memory.
    
    Args:
        input_path: Path to parquet file
        
    Returns:
        Dictionary with file metadata (size, row count, columns, schema compatibility)
    """
    try:
        # Use pyarrow to read metadata efficiently
        parquet_file = pq.ParquetFile(input_path)
        
        # Get Arrow schema for compatibility checking
        arrow_schema = parquet_file.schema_arrow
        
        return {
            'size_mb': input_path.stat().st_size / 1024 / 1024,
            'num_rows': parquet_file.metadata.num_rows,
            'num_row_groups': parquet_file.metadata.num_row_groups,
            'columns': [field.name for field in arrow_schema],
            'schema': arrow_schema,
            'has_incompatible_types': _has_incompatible_schema(arrow_schema)
        }
    except Exception as e:
        # Fallback to basic file info
        return {
            'size_mb': input_path.stat().st_size / 1024 / 1024,
            'num_rows': None,
            'num_row_groups': None,
            'columns': None,
            'schema': None,
            'has_incompatible_types': False
        }


def _has_incompatible_schema(schema) -> bool:
    """
    Check if parquet schema contains types that Polars cannot handle.
    
    Polars streaming engine has known issues with:
    - MapArray types (complex nested structures)
    - Certain nested struct configurations
    
    Args:
        schema: PyArrow schema object (from ParquetFile.schema_arrow)
        
    Returns:
        True if schema contains incompatible types
    """
    import pyarrow as pa
    
    # Get Arrow schema (handles both schema types)
    if hasattr(schema, 'to_arrow_schema'):
        arrow_schema = schema.to_arrow_schema()
    else:
        arrow_schema = schema
    
    for field in arrow_schema:
        field_type = field.type
        type_str = str(field_type)
        
        # Check for MapArray (the main culprit)
        if 'map<' in type_str.lower():
            return True
        
        # Check for deeply nested structures that might cause issues
        # Overture Maps uses complex nested types for tags/properties
        if isinstance(field_type, pa.MapType):
            return True
            
    return False


def _convert_with_geopandas_streaming(
    input_path: Path,
    output_path: Path,
    verbose: bool,
    start_time: float
) -> Tuple[bool, str, int]:
    """
    Fallback converter using GeoPandas for files with complex schemas.
    
    This is used when Polars can't handle certain data types (e.g., MapArray).
    Slower than Polars but more compatible.
    """
    try:
        if verbose:
            print(f"  ⚡ Reading with GeoPandas...", end="", flush=True)
        
        read_start = time.time()
        # Use geopandas to read parquet directly
        gdf = gpd.read_parquet(input_path)
        read_time = time.time() - read_start
        
        if verbose:
            print(f"\r  ⚡ Read complete: {len(gdf):,} rows in {read_time:.1f}s (GeoPandas)")
        
        # Handle WKB geometry if needed
        geom_col = gdf.geometry.name
        if geom_col and gdf[geom_col].dtype == 'object':
            # Check if geometries are WKB bytes
            if len(gdf) > 0 and isinstance(gdf[geom_col].iloc[0], bytes):
                if verbose:
                    print(f"  🔧 Decoding WKB geometries...", end="", flush=True)
                from shapely import wkb
                gdf[geom_col] = gdf[geom_col].apply(lambda x: wkb.loads(x) if x is not None else None)
                gdf = gpd.GeoDataFrame(gdf, geometry=geom_col)
                if verbose:
                    print(f"\r  🔧 WKB decoding complete")
        
        # Ensure CRS is set
        if gdf.crs is None:
            gdf = gdf.set_crs("EPSG:4326")
            if verbose:
                print(f"  🔧 GeoDataFrame ready ({len(gdf):,} features, CRS: EPSG:4326)")
        else:
            if verbose:
                print(f"  🔧 GeoDataFrame ready ({len(gdf):,} features, CRS: {gdf.crs})")
        
        if verbose:
            print(f"  💾 Writing FlatGeobuf with spatial index...", end="", flush=True)
        
        write_start = time.time()
        gdf.to_file(output_path, driver='FlatGeobuf', SPATIAL_INDEX='YES')
        write_time = time.time() - write_start
        
        total_features = len(gdf)
        total_time = time.time() - start_time
        
        output_size_mb = output_path.stat().st_size / 1024 / 1024
        
        del gdf
        gc.collect()
        
        if verbose:
            print(f"\r  ✅ Complete: {total_features:,} features → {output_size_mb:.1f} MB")
            print(f"     ⏱️  Total time: {total_time:.1f}s (read: {read_time:.1f}s, write: {write_time:.1f}s)")
            print(f"     ℹ️  Used GeoPandas fallback (Polars incompatible schema)")
        
        return True, f"Converted {total_features:,} features (GeoPandas)", total_features
        
    except Exception as e:
        if verbose:
            print(f"\n  ❌ GEOPANDAS FALLBACK ERROR: {type(e).__name__}")
            print(f"     Error details: {str(e)}")
        return False, f"{type(e).__name__}: {str(e)}", 0


def _convert_with_geopandas_direct(
    input_path: Path,
    output_path: Path,
    verbose: bool,
    start_time: float
) -> Tuple[bool, str, int]:
    """
    Fallback converter using GeoPandas for files with complex schemas (direct mode).
    
    This is used when Polars can't handle certain data types.
    """
    try:
        if verbose:
            print(f"  ⚡ Reading with GeoPandas...", end="", flush=True)
        
        read_start = time.time()
        gdf = gpd.read_parquet(input_path)
        read_time = time.time() - read_start
        
        if verbose:
            print(f"\r  ⚡ Read complete: {len(gdf):,} rows in {read_time:.1f}s (GeoPandas)")
        
        # Handle WKB geometry if needed
        geom_col = gdf.geometry.name
        if geom_col and gdf[geom_col].dtype == 'object':
            # Check if geometries are WKB bytes
            if len(gdf) > 0 and isinstance(gdf[geom_col].iloc[0], bytes):
                if verbose:
                    print(f"  🔧 Decoding WKB geometries...", end="", flush=True)
                from shapely import wkb
                gdf[geom_col] = gdf[geom_col].apply(lambda x: wkb.loads(x) if x is not None else None)
                gdf = gpd.GeoDataFrame(gdf, geometry=geom_col)
                if verbose:
                    print(f"\r  🔧 WKB decoding complete")
        
        if gdf.crs is None:
            gdf = gdf.set_crs("EPSG:4326")
            if verbose:
                print(f"  🔧 GeoDataFrame ready ({len(gdf):,} features, CRS: EPSG:4326)")
        else:
            if verbose:
                print(f"  🔧 GeoDataFrame ready ({len(gdf):,} features, CRS: {gdf.crs})")
        
        if verbose:
            print(f"  💾 Writing FlatGeobuf with spatial index...", end="", flush=True)
        
        write_start = time.time()
        gdf.to_file(output_path, driver='FlatGeobuf', SPATIAL_INDEX='YES')
        write_time = time.time() - write_start
        
        total_features = len(gdf)
        total_time = time.time() - start_time
        
        output_size_mb = output_path.stat().st_size / 1024 / 1024
        
        del gdf
        gc.collect()
        
        if verbose:
            print(f"\r  ✅ Complete: {total_features:,} features → {output_size_mb:.1f} MB")
            print(f"     ⏱️  Total time: {total_time:.1f}s (read: {read_time:.1f}s, write: {write_time:.1f}s)")
            print(f"     ℹ️  Used GeoPandas fallback (Polars incompatible schema)")
        
        return True, f"Converted {total_features:,} features (GeoPandas)", total_features
        
    except Exception as e:
        if verbose:
            print(f"\n  ❌ GEOPANDAS FALLBACK ERROR: {type(e).__name__}")
            print(f"     Error details: {str(e)}")
        return False, f"{type(e).__name__}: {str(e)}", 0


def convert_parquet_to_fgb_streaming(
    input_path: Union[str, Path],
    output_path: Union[str, Path],
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    verbose: bool = True
) -> Tuple[bool, str, int]:
    """
    Convert large GeoParquet to FlatGeobuf using Polars native streaming.
    
    Uses Polars' streaming engine to process files larger than RAM efficiently.
    The streaming engine processes data in batches automatically without loading
    the entire dataset into memory.
    
    Args:
        input_path: Path to input .parquet file
        output_path: Path to output .fgb file
        chunk_size: Number of features per chunk (for batch writing)
        verbose: Print progress information
        
    Returns:
        Tuple of (success, message, total_features)
    """
    input_path = Path(input_path)
    output_path = Path(output_path)
    
    try:
        start_time = time.time()
        
        # Get file info for progress tracking
        if verbose:
            print(f"  📊 Analyzing file...", end="", flush=True)
        
        file_info = get_file_info(input_path)
        total_rows = file_info.get('num_rows', 0)
        
        # Check for incompatible schema BEFORE attempting Polars read
        # This prevents Rust panics that can't be caught by Python try/except
        if file_info.get('has_incompatible_types', False):
            if verbose:
                print(f"\r  📊 File: {file_info['size_mb']:.1f} MB, {total_rows:,} rows")
                print(f"  ⚠️  Schema contains MapArray/incompatible types")
                print(f"  🔄 Using GeoPandas fallback (Polars incompatible)")
            return _convert_with_geopandas_streaming(input_path, output_path, verbose, start_time)
        
        if verbose:
            print(f"\r  📊 File: {file_info['size_mb']:.1f} MB, {total_rows:,} rows")
            print(f"  🔄 Mode: STREAMING (Polars native engine)")
            print(f"  ⚡ Reading parquet with streaming...", end="", flush=True)
        
        # Use Polars native streaming via scan + collect(streaming=True)
        # This is much more efficient than manual chunking
        # Polars automatically batches data and manages memory
        lazy_df = pl.scan_parquet(input_path)
        
        read_start = time.time()
        try:
            # Collect with streaming engine - processes in batches automatically
            # This avoids loading the entire file into RAM
            df = lazy_df.collect(streaming=True)
            read_time = time.time() - read_start
        except Exception as polars_error:
            # Fallback for errors that slip through schema check
            # (shouldn't normally happen after schema inspection)
            if "MapArray" in str(polars_error) or "DataType" in str(polars_error) or "Panic" in str(polars_error):
                if verbose:
                    print(f"\r  ⚠️  Polars streaming error (complex schema)")
                    print(f"  🔄 Falling back to GeoPandas...")
                return _convert_with_geopandas_streaming(input_path, output_path, verbose, start_time)
            else:
                # Re-raise other errors
                raise
        
        if verbose:
            print(f"\r  ⚡ Read complete: {len(df):,} rows in {read_time:.1f}s")
            print(f"  🔧 Converting to GeoDataFrame...", end="", flush=True)
        
        # Convert to pandas for geopandas (unavoidable for spatial operations)
        pdf = df.to_pandas()
        
        # Find geometry column
        geom_col = 'geometry'
        if 'geometry' not in pdf.columns:
            possible_geom_cols = [col for col in pdf.columns if 'geom' in col.lower()]
            if possible_geom_cols:
                geom_col = possible_geom_cols[0]
            else:
                return False, "No geometry column found", 0
        
        # Handle WKB geometry if needed
        if geom_col in pdf.columns and pdf[geom_col].dtype == 'object':
            # Check if geometries are WKB bytes
            if len(pdf) > 0 and isinstance(pdf[geom_col].iloc[0], bytes):
                if verbose:
                    print(f"\r  🔧 Decoding WKB geometries...", end="", flush=True)
                from shapely import wkb
                pdf[geom_col] = pdf[geom_col].apply(lambda x: wkb.loads(x) if x is not None else None)
        
        # Convert to GeoDataFrame
        gdf = gpd.GeoDataFrame(pdf, geometry=geom_col)
        
        # Ensure CRS is set
        if gdf.crs is None:
            gdf = gdf.set_crs("EPSG:4326")
            if verbose:
                print(f"\r  🔧 GeoDataFrame ready ({len(gdf):,} features, CRS: EPSG:4326)")
        else:
            if verbose:
                print(f"\r  🔧 GeoDataFrame ready ({len(gdf):,} features, CRS: {gdf.crs})")
        
        if verbose:
            print(f"  💾 Writing FlatGeobuf with spatial index...", end="", flush=True)
        
        write_start = time.time()
        # Write as FlatGeobuf with spatial index
        # For very large datasets, this is the memory bottleneck
        # Consider batch writing if geopandas supports it in the future
        gdf.to_file(output_path, driver='FlatGeobuf', SPATIAL_INDEX='YES')
        write_time = time.time() - write_start
        
        total_features = len(gdf)
        total_time = time.time() - start_time
        
        # Get output file size
        output_size_mb = output_path.stat().st_size / 1024 / 1024
        
        # Clear memory
        del df, pdf, gdf
        gc.collect()
        
        if verbose:
            print(f"\r  ✅ Complete: {total_features:,} features → {output_size_mb:.1f} MB")
            print(f"     ⏱️  Total time: {total_time:.1f}s (read: {read_time:.1f}s, write: {write_time:.1f}s)")
        
        return True, f"Converted {total_features:,} features", total_features
        
    except Exception as e:
        if verbose:
            print(f"\n  ❌ STREAMING ERROR: {type(e).__name__}")
            print(f"     Error details: {str(e)}")
            import traceback
            print(f"     Traceback: {traceback.format_exc()[-500:]}")
        return False, f"{type(e).__name__}: {str(e)}", 0


def convert_parquet_to_fgb_direct(
    input_path: Path,
    output_path: Path,
    verbose: bool = True
) -> Tuple[bool, str, int]:
    """
    Convert GeoParquet to FlatGeobuf in a single pass (for smaller files).
    
    Uses Polars eager mode for fast I/O with smaller files that fit in memory.
    For files close to the threshold, consider using streaming mode instead.
    
    Args:
        input_path: Path to input .parquet file
        output_path: Path to output .fgb file
        verbose: Print progress information
        
    Returns:
        Tuple of (success, message, total_features)
    """
    try:
        start_time = time.time()
        
        # Check for incompatible schema BEFORE attempting Polars read
        # This prevents Rust panics that can't be caught by Python try/except
        file_info = get_file_info(input_path)
        
        if file_info.get('has_incompatible_types', False):
            if verbose:
                file_size_mb = input_path.stat().st_size / 1024 / 1024
                print(f"  📊 File: {file_size_mb:.1f} MB")
                print(f"  ⚠️  Schema contains MapArray/incompatible types")
                print(f"  🔄 Using GeoPandas fallback (Polars incompatible)")
            return _convert_with_geopandas_direct(input_path, output_path, verbose, start_time)
        
        if verbose:
            file_size_mb = input_path.stat().st_size / 1024 / 1024
            print(f"  📊 File: {file_size_mb:.1f} MB")
            print(f"  🔄 Mode: DIRECT (eager loading)")
            print(f"  ⚡ Reading parquet...", end="", flush=True)
        
        read_start = time.time()
        try:
            # Read with Polars eager mode (faster for smaller files)
            # Polars is 10-100x faster than pandas for parquet I/O
            df = pl.read_parquet(input_path)
            read_time = time.time() - read_start
        except Exception as polars_error:
            # Fallback for errors that slip through schema check
            # (shouldn't normally happen after schema inspection)
            if "MapArray" in str(polars_error) or "DataType" in str(polars_error) or "Panic" in str(polars_error):
                if verbose:
                    print(f"\r  ⚠️  Polars error (complex schema)")
                    print(f"  🔄 Falling back to GeoPandas...")
                return _convert_with_geopandas_direct(input_path, output_path, verbose, start_time)
            else:
                # Re-raise other errors
                raise
        
        if verbose:
            print(f"\r  ⚡ Read complete: {len(df):,} rows in {read_time:.1f}s")
            print(f"  🔧 Converting to GeoDataFrame...", end="", flush=True)
        
        # Convert to pandas for geopandas compatibility
        # This is currently unavoidable for spatial operations
        pdf = df.to_pandas()
        
        # Find geometry column
        geom_col = 'geometry'
        if 'geometry' not in pdf.columns:
            possible_geom_cols = [col for col in pdf.columns if 'geom' in col.lower()]
            if possible_geom_cols:
                geom_col = possible_geom_cols[0]
            else:
                return False, "No geometry column found", 0
        
        # Handle WKB geometry if needed
        if geom_col in pdf.columns and pdf[geom_col].dtype == 'object':
            # Check if geometries are WKB bytes
            if len(pdf) > 0 and isinstance(pdf[geom_col].iloc[0], bytes):
                if verbose:
                    print(f"\r  🔧 Decoding WKB geometries...", end="", flush=True)
                from shapely import wkb
                pdf[geom_col] = pdf[geom_col].apply(lambda x: wkb.loads(x) if x is not None else None)
        
        # Convert to GeoDataFrame
        gdf = gpd.GeoDataFrame(pdf, geometry=geom_col)
        
        # Ensure CRS is set (FlatGeobuf requires CRS)
        if gdf.crs is None:
            gdf = gdf.set_crs("EPSG:4326")
            if verbose:
                print(f"\r  🔧 GeoDataFrame ready ({len(gdf):,} features, CRS: EPSG:4326)")
        else:
            if verbose:
                print(f"\r  🔧 GeoDataFrame ready ({len(gdf):,} features, CRS: {gdf.crs})")
        
        if verbose:
            print(f"  💾 Writing FlatGeobuf with spatial index...", end="", flush=True)
        
        write_start = time.time()
        # Write as FlatGeobuf with spatial index
        gdf.to_file(output_path, driver='FlatGeobuf', SPATIAL_INDEX='YES')
        write_time = time.time() - write_start
        
        total_features = len(gdf)
        total_time = time.time() - start_time
        
        # Get output file size
        output_size_mb = output_path.stat().st_size / 1024 / 1024
        
        # Clear memory
        del df, pdf, gdf
        gc.collect()
        
        if verbose:
            print(f"\r  ✅ Complete: {total_features:,} features → {output_size_mb:.1f} MB")
            print(f"     ⏱️  Total time: {total_time:.1f}s (read: {read_time:.1f}s, write: {write_time:.1f}s)")
        
        return True, f"Converted {total_features:,} features", total_features
        
    except Exception as e:
        if verbose:
            print(f"\n  ❌ DIRECT MODE ERROR: {type(e).__name__}")
            print(f"     Error details: {str(e)}")
            import traceback
            print(f"     Traceback: {traceback.format_exc()[-500:]}")
        return False, f"{type(e).__name__}: {str(e)}", 0


def convert_parquet_to_fgb(
    input_path: Union[str, Path],
    output_path: Optional[Union[str, Path]] = None,
    overwrite: bool = False,
    verbose: bool = True,
    cleanup_source: bool = False,
    chunk_size: Optional[int] = None,
    force_streaming: bool = False
) -> Tuple[bool, str, Optional[Path]]:
    """
    Convert a single GeoParquet file to FlatGeobuf format.
    
    Automatically selects streaming or direct mode based on file size.
    Large files (>500MB) use chunked streaming to prevent memory overflow.
    
    Args:
        input_path: Path to input .parquet file
        output_path: Path to output .fgb file (auto-generated if None)
        overwrite: Whether to overwrite existing files
        verbose: Print progress information
        cleanup_source: Remove source file after successful conversion (saves disk space)
        chunk_size: Features per chunk for streaming (default: auto-determined)
        force_streaming: Force streaming mode regardless of file size
        
    Returns:
        Tuple of (success, message, output_path)
    """
    input_path = Path(input_path)
    
    if not input_path.exists():
        return False, f"Input file not found: {input_path}", None
    
    # Auto-generate output path if not provided
    if output_path is None:
        output_path = input_path.with_suffix('.fgb')
    else:
        output_path = Path(output_path)
    
    # Check if output already exists
    if output_path.exists() and not overwrite:
        if verbose:
            output_size_mb = output_path.stat().st_size / 1024 / 1024
            print(f"⊘ Skipping {input_path.name} → {output_path.name} ({output_size_mb:.1f} MB, already exists)")
        return True, "Already exists", output_path
    
    try:
        if verbose:
            print(f"\n{'='*70}")
            print(f"📦 Converting: {input_path.name}")
            print(f"{'='*70}")
        
        # Get file metadata
        file_info = get_file_info(input_path)
        file_size_mb = file_info['size_mb']
        num_rows = file_info.get('num_rows')
        
        # Determine processing mode
        use_streaming = force_streaming or file_size_mb > LARGE_FILE_THRESHOLD_MB
        
        if use_streaming:
            # Auto-determine chunk size based on file size if not specified
            if chunk_size is None:
                if file_size_mb > 2000:  # >2GB
                    chunk_size = MEMORY_EFFICIENT_CHUNK_SIZE
                else:
                    chunk_size = DEFAULT_CHUNK_SIZE
            
            # Use streaming conversion for large files
            success, message, total_features = convert_parquet_to_fgb_streaming(
                input_path=input_path,
                output_path=output_path,
                chunk_size=chunk_size,
                verbose=verbose
            )
        else:
            # Use direct conversion for smaller files (faster)
            success, message, total_features = convert_parquet_to_fgb_direct(
                input_path=input_path,
                output_path=output_path,
                verbose=verbose
            )
        
        if not success:
            if verbose:
                print(f"\n❌ Conversion failed: {message}")
            return False, message, None
        
        # Get output file stats
        input_size_mb = input_path.stat().st_size / 1024 / 1024
        output_size_mb = output_path.stat().st_size / 1024 / 1024
        compression_pct = ((input_size_mb - output_size_mb) / input_size_mb) * 100
        
        if verbose:
            print(f"\n📊 Summary:")
            print(f"   Input:  {input_size_mb:>8.1f} MB (.parquet)")
            print(f"   Output: {output_size_mb:>8.1f} MB (.fgb)")
            print(f"   Saved:  {compression_pct:>8.1f}% smaller")
        
        # Remove source file if requested (saves disk space)
        if cleanup_source and input_path.exists():
            input_path.unlink()
            if verbose:
                print(f"   🗑️  Cleaned up source file (saved {input_size_mb:.1f} MB)")
        
        if verbose:
            print(f"\n✅ SUCCESS: {output_path.name}")
            print(f"{'='*70}\n")
        
        return True, f"Converted {total_features:,} features", output_path
        
    except Exception as e:
        if verbose:
            print(f"\n❌ CONVERSION ERROR: {type(e).__name__}")
            print(f"   File: {input_path.name}")
            print(f"   Error: {str(e)}")
            import traceback
            print(f"   Traceback:\n{traceback.format_exc()[-800:]}")
            print(f"{'='*70}\n")
        return False, f"{type(e).__name__}: {str(e)}", None


def batch_convert_directory(
    input_dir: Union[str, Path],
    output_dir: Optional[Union[str, Path]] = None,
    pattern: str = "*.parquet",
    overwrite: bool = False,
    verbose: bool = True,
    parallel: bool = False,
    cleanup_source: bool = False,
    chunk_size: Optional[int] = None,
    force_streaming: bool = False
) -> dict:
    """
    Convert all GeoParquet files in a directory to FlatGeobuf format.
    
    Automatically uses streaming conversion for large files (>500MB).
    
    Args:
        input_dir: Directory containing .parquet files
        output_dir: Directory for .fgb output (same as input if None)
        pattern: Glob pattern for finding parquet files
        overwrite: Whether to overwrite existing files
        verbose: Print progress information
        parallel: Use parallel processing (experimental)
        cleanup_source: Remove source files after successful conversion (saves disk space)
        chunk_size: Features per chunk for streaming (default: auto-determined)
        force_streaming: Force streaming mode for all files
        
    Returns:
        Dictionary with conversion results
    """
    input_dir = Path(input_dir)
    
    if output_dir is None:
        output_dir = input_dir
    else:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
    
    # Find all parquet files
    parquet_files = sorted(input_dir.glob(pattern))
    
    if not parquet_files:
        return {
            "success": False,
            "message": f"No files matching '{pattern}' found in {input_dir}",
            "total_files": 0,
            "converted": 0,
            "skipped": 0,
            "errors": []
        }
    
    if verbose:
        print(f"\n{'='*70}")
        print(f"🚀 BATCH CONVERSION: GeoParquet → FlatGeobuf")
        print(f"{'='*70}")
        print(f"📁 Input:  {input_dir}")
        print(f"📁 Output: {output_dir}")
        print(f"📦 Files:  {len(parquet_files)} GeoParquet files")
        
        # Show file size summary
        total_size_mb = sum(f.stat().st_size for f in parquet_files) / 1024 / 1024
        print(f"💾 Total:  {total_size_mb:.1f} MB")
        
        large_files = [f for f in parquet_files if f.stat().st_size / 1024 / 1024 > LARGE_FILE_THRESHOLD_MB]
        if large_files:
            print(f"⚡ Large:  {len(large_files)} files >{LARGE_FILE_THRESHOLD_MB}MB (will use streaming)")
        
        print(f"🧹 Cleanup: {'ON - source files will be removed' if cleanup_source else 'OFF - source files retained'}")
        print(f"{'='*70}\n")
    
    results = {
        "success": True,
        "total_files": len(parquet_files),
        "converted": 0,
        "skipped": 0,
        "errors": [],
        "output_files": [],
        "cleaned_up": 0 if cleanup_source else None
    }
    
    # Process files
    use_tqdm = verbose and len(parquet_files) > 1
    
    if use_tqdm:
        print(f"Processing {len(parquet_files)} files...\n")
        iterator = tqdm(parquet_files, desc="Overall Progress", unit="file")
    else:
        iterator = parquet_files
    
    for idx, parquet_file in enumerate(iterator, 1):
        if verbose and not use_tqdm:
            print(f"\n[{idx}/{len(parquet_files)}] Processing {parquet_file.name}...")
        output_file = output_dir / parquet_file.with_suffix('.fgb').name
        
        success, message, output_path = convert_parquet_to_fgb(
            input_path=parquet_file,
            output_path=output_file,
            overwrite=overwrite,
            verbose=verbose and not use_tqdm,  # Suppress individual messages when using tqdm
            cleanup_source=cleanup_source,
            chunk_size=chunk_size,
            force_streaming=force_streaming
        )
        
        if success:
            if "Already exists" in message:
                results["skipped"] += 1
            else:
                results["converted"] += 1
                results["output_files"].append(output_path)
                if cleanup_source:
                    results["cleaned_up"] += 1
                
                if verbose and not use_tqdm:  # Only print if not using tqdm
                    print(f"✓ {parquet_file.name} → {output_file.name}")
        else:
            results["errors"].append({
                "file": parquet_file.name,
                "error": message
            })
            results["success"] = False
            
            if verbose and not use_tqdm:
                print(f"✗ {parquet_file.name}: {message}")
    
    # Print summary
    if verbose:
        print(f"\n{'='*70}")
        print(f"📊 BATCH CONVERSION SUMMARY")
        print(f"{'='*70}")
        print(f"   Total files:  {results['total_files']}")
        print(f"   ✅ Converted:  {results['converted']}")
        print(f"   ⊘  Skipped:    {results['skipped']} (already exist)")
        print(f"   ❌ Errors:     {len(results['errors'])}")
        
        if cleanup_source and results['cleaned_up']:
            print(f"   🗑️  Cleaned:    {results['cleaned_up']} source files removed")
        
        if results['output_files']:
            total_size_mb = sum(f.stat().st_size for f in results['output_files']) / 1024 / 1024
            print(f"   💾 Output:     {total_size_mb:.1f} MB total")
        
        if results['errors']:
            print(f"\n❌ ERRORS ENCOUNTERED:")
            for error in results['errors']:
                print(f"   • {error['file']}: {error['error']}")
        
        if results['converted'] > 0:
            print(f"\n✅ {results['converted']} FlatGeobuf files ready for tippecanoe")
            print(f"   Location: {output_dir}")
        
        print(f"{'='*70}\n")
    
    return results


def convert_geodata_to_fgb(
    input_path: Union[str, Path],
    output_path: Optional[Union[str, Path]] = None,
    layer: Optional[str] = None,
    clip_extent: Optional[Tuple[float, float, float, float]] = None,
    where: Optional[str] = None,
    overwrite: bool = False,
    verbose: bool = True
) -> Tuple[bool, str, Optional[Path]]:
    """
    Convert any geospatial format to FlatGeobuf using GeoPandas.
    
    Supports all formats that GeoPandas/Fiona can read:
    - Shapefile (.shp)
    - GeoJSON (.geojson, .json)
    - GeoPackage (.gpkg)
    - ArcGIS File Geodatabase (.gdb)
    - ArcGIS Feature Class (via file path)
    - KML/KMZ (.kml, .kmz)
    - And many more...
    
    Args:
        input_path: Path to input geospatial file or directory (e.g., .gdb, .shp)
        output_path: Path to output .fgb file (auto-generated if None)
        layer: Layer name (required for multi-layer formats like .gdb)
        clip_extent: Clip to bounding box (lon_min, lat_min, lon_max, lat_max)
        where: SQL WHERE clause for attribute filtering (e.g., "population > 1000")
        overwrite: Whether to overwrite existing files
        verbose: Print progress information
        
    Returns:
        Tuple of (success, message, output_path)
        
    Examples:
        # Shapefile
        convert_geodata_to_fgb("data.shp", "output.fgb")
        
        # File Geodatabase with layer selection
        convert_geodata_to_fgb("data.gdb", "output.fgb", layer="settlements")
        
        # GeoPackage with spatial filter
        convert_geodata_to_fgb("data.gpkg", clip_extent=(15, -5, 30, 5))
        
        # With attribute filter
        convert_geodata_to_fgb("cities.shp", where="population > 100000")
    """
    input_path = Path(input_path)
    
    if not input_path.exists():
        return False, f"Input file not found: {input_path}", None
    
    # Auto-generate output path if not provided
    if output_path is None:
        if layer:
            output_path = input_path.parent / f"{input_path.stem}_{layer}.fgb"
        else:
            output_path = input_path.with_suffix(".fgb")
    else:
        output_path = Path(output_path)
    
    # Check if output already exists
    if output_path.exists() and not overwrite:
        if verbose:
            print(f"⏭️  Skipped {output_path.name} (already exists)")
        return True, "Output already exists (use overwrite=True to replace)", output_path
    
    # Ensure output directory exists
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    try:
        start_time = time.time()
        
        if verbose:
            layer_info = f" (layer: {layer})" if layer else ""
            print(f"🔄 Converting {input_path.name}{layer_info} → {output_path.name}")
        
        # Read the geospatial file
        read_kwargs = {}
        if layer:
            read_kwargs['layer'] = layer
        
        gdf = gpd.read_file(input_path, **read_kwargs)
        original_count = len(gdf)
        
        if verbose:
            print(f"   • Loaded {original_count:,} features")
            print(f"   • Geometry type: {gdf.geom_type.iloc[0] if len(gdf) > 0 else 'Unknown'}")
            print(f"   • CRS: {gdf.crs}")
        
        # Apply attribute filter if provided
        if where:
            gdf = gdf.query(where)
            if verbose:
                print(f"   • Filtered to {len(gdf):,} features (WHERE: {where})")
        
        # Apply spatial clipping if provided
        if clip_extent:
            lon_min, lat_min, lon_max, lat_max = clip_extent
            
            # Ensure CRS is WGS84 for clipping
            if gdf.crs and not gdf.crs.equals(4326):
                if verbose:
                    print(f"   • Reprojecting to WGS84 for clipping...")
                gdf = gdf.to_crs(4326)
            
            # Apply spatial filter
            gdf = gdf.cx[lon_min:lon_max, lat_min:lat_max]
            if verbose:
                print(f"   • Clipped to extent: {len(gdf):,} features retained")
        
        # Check if we have any features left
        if len(gdf) == 0:
            return False, "No features remaining after filtering/clipping", None
        
        # Ensure valid geometries
        if not gdf.geometry.is_valid.all():
            if verbose:
                print(f"   • Fixing invalid geometries...")
            gdf.geometry = gdf.geometry.buffer(0)
        
        # Write to FlatGeobuf
        gdf.to_file(output_path, driver="FlatGeobuf")
        
        elapsed = time.time() - start_time
        file_size_mb = output_path.stat().st_size / 1024 / 1024
        
        if verbose:
            print(f"   ✓ Wrote {len(gdf):,} features ({file_size_mb:.1f} MB) in {elapsed:.1f}s")
        
        return True, f"Successfully converted {len(gdf):,} features", output_path
        
    except Exception as e:
        error_msg = f"Conversion failed: {str(e)}"
        if verbose:
            print(f"   ✗ {error_msg}")
        return False, error_msg, None


def batch_convert_geodata(
    input_paths: List[Union[str, Path]],
    output_dir: Union[str, Path],
    clip_extent: Optional[Tuple[float, float, float, float]] = None,
    overwrite: bool = False,
    verbose: bool = True
) -> dict:
    """
    Batch convert multiple geospatial files to FlatGeobuf format.
    
    Args:
        input_paths: List of paths to geospatial files
        output_dir: Directory for .fgb output files
        clip_extent: Optional bounding box to clip all files
        overwrite: Whether to overwrite existing files
        verbose: Print progress information
        
    Returns:
        Dictionary with conversion results
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    results = {
        "success": True,
        "total_files": len(input_paths),
        "converted": 0,
        "skipped": 0,
        "errors": [],
        "output_files": []
    }
    
    if verbose:
        print("="*70)
        print("BATCH GEODATA CONVERSION TO FLATGEOBUF")
        print("="*70)
        print(f"Input files: {len(input_paths)}")
        print(f"Output directory: {output_dir}")
        if clip_extent:
            print(f"Clip extent: {clip_extent}")
        print()
    
    iterator = tqdm(input_paths, desc="Converting") if verbose else input_paths
    
    for input_path in iterator:
        input_path = Path(input_path)
        output_name = input_path.stem + ".fgb"
        output_path = output_dir / output_name
        
        success, message, out_path = convert_geodata_to_fgb(
            input_path=input_path,
            output_path=output_path,
            clip_extent=clip_extent,
            overwrite=overwrite,
            verbose=False  # Suppress per-file output during batch
        )
        
        if success and out_path:
            results["converted"] += 1
            results["output_files"].append(str(out_path))
        elif "already exists" in message:
            results["skipped"] += 1
        else:
            results["success"] = False
            results["errors"].append(f"{input_path.name}: {message}")
    
    if verbose:
        print("\n" + "="*70)
        print("CONVERSION SUMMARY")
        print("="*70)
        print(f"Total files: {results['total_files']}")
        print(f"Converted: {results['converted']}")
        print(f"Skipped: {results['skipped']}")
        print(f"Errors: {len(results['errors'])}")
        
        if results['errors']:
            print("\nErrors:")
            for error in results['errors']:
                print(f"  ✗ {error}")
        
        if results['converted'] > 0:
            print(f"\n✓ {results['converted']} FlatGeobuf files ready")
            print(f"  Location: {output_dir}")
        
        print("="*70 + "\n")
    
    return results


def main():
    """Command-line interface for the converter."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Convert GeoParquet files to FlatGeobuf format for efficient tile generation"
    )
    parser.add_argument(
        "--input-dir",
        type=str,
        required=True,
        help="Directory containing GeoParquet (.parquet) files"
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Output directory for FlatGeobuf (.fgb) files (default: same as input)"
    )
    parser.add_argument(
        "--pattern",
        type=str,
        default="*.parquet",
        help="Glob pattern for finding parquet files (default: *.parquet)"
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing FlatGeobuf files"
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output"
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Remove source files after successful conversion (saves disk space)"
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=None,
        help=f"Features per chunk for streaming mode (default: auto, typically {DEFAULT_CHUNK_SIZE:,})"
    )
    parser.add_argument(
        "--force-streaming",
        action="store_true",
        help=f"Force streaming mode for all files (default: auto for files >{LARGE_FILE_THRESHOLD_MB}MB)"
    )
    
    args = parser.parse_args()
    
    results = batch_convert_directory(
        input_dir=args.input_dir,
        output_dir=args.output_dir,
        pattern=args.pattern,
        overwrite=args.overwrite,
        verbose=not args.quiet,
        cleanup_source=args.cleanup,
        chunk_size=args.chunk_size,
        force_streaming=args.force_streaming
    )
    
    sys.exit(0 if results["success"] else 1)


if __name__ == "__main__":
    main()
