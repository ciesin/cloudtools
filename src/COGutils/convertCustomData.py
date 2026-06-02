#!/usr/bin/env python3
"""
convertForTipp.py - Convert various geospatial formats to newline-delimited GeoJSON for Tippecanoe

This script converts common geospatial data formats (GeoPackage, Shapefile, 
SQLite/SpatiaLite, PostGIS, FileGDB, and more) to newline-delimited GeoJSON 
files that are suitable for processing with Tippecanoe to create PMTiles.

Designed to be used as a preprocessing step for runCreateTiles.py, which will handle
the actual Tippecanoe calls to generate PMTiles.

Can be used as a standalone script or imported as a module in runCreateTiles.py.

Standalone usage:
    python convertForTipp.py input_data.gpkg output.geojsonseq --layer=layer_name
    python convertForTipp.py input.shp output.geojsonseq
    python convertForTipp.py PG:"host=localhost dbname=mydb user=user password=pass" output.geojsonseq --layer=table_name
    python convertForTipp.py input.gdb output.geojsonseq --layer=feature_class_name
    
Additional options:
    --where         SQL WHERE clause to filter features
    --sql           Custom SQL query to execute
    --limit         Limit the number of features to process
    --simplify      Simplify geometries (tolerance in layer units)
    --reproject     Reproject to another CRS (e.g., EPSG:4326)
    --id-field      Field to use as feature ID
    --property-list Comma-separated list of properties to include (default: all)
    --exclude-props Comma-separated list of properties to exclude
    --buffer        Create a buffer around geometries (in layer units)
    --verbose       Show detailed progress
"""

import argparse
import os
import sys
import json
import time
from pathlib import Path
from tqdm import tqdm
import warnings
from osgeo import gdal, ogr, osr

def create_parser():
    """Create command line argument parser"""
    parser = argparse.ArgumentParser(
        description='Convert geospatial data to newline-delimited GeoJSON for Tippecanoe',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    
    parser.add_argument('input', help='Input data source (file or connection string)')
    parser.add_argument('output', help='Output newline-delimited GeoJSON file')
    parser.add_argument('--layer', help='Layer/table name (required for multi-layer sources)')
    parser.add_argument('--where', help='SQL WHERE clause to filter features')
    parser.add_argument('--sql', help='Custom SQL query to execute')
    parser.add_argument('--limit', type=int, help='Limit the number of features to process')
    parser.add_argument('--simplify', type=float, help='Simplify geometries (tolerance in layer units)')
    parser.add_argument('--reproject', help='Reproject to another CRS (e.g., EPSG:4326)')
    parser.add_argument('--id-field', help='Field to use as feature ID')
    parser.add_argument('--property-list', help='Comma-separated list of properties to include (default: all)')
    parser.add_argument('--exclude-props', help='Comma-separated list of properties to exclude')
    parser.add_argument('--buffer', type=float, help='Create a buffer around geometries (in layer units)')
    parser.add_argument('--verbose', action='store_true', help='Show detailed progress')
    parser.add_argument('--batch-size', type=int, default=10000, 
                        help='Number of features to process in each batch')
    
    return parser 

def open_dataset(input_path):
    """Open the input dataset using OGR"""
    try:
        ds = ogr.Open(input_path, 0)  # 0 = read-only
        if ds is None:
            sys.exit(f"ERROR: Could not open {input_path}")
        return ds
    except Exception as e:
        sys.exit(f"ERROR: Failed to open {input_path}: {str(e)}")

def get_layer_from_dataset(ds, layer_name=None, sql=None, where=None):
    """Get layer from dataset, apply filters if specified"""
    if sql:
        # Execute SQL query
        layer = ds.ExecuteSQL(sql, dialect='SQLITE')
        if layer is None:
            sys.exit(f"ERROR: SQL query failed: {sql}")
        return layer

    if layer_name:
        # Get specified layer
        layer = ds.GetLayerByName(layer_name)
        if layer is None:
            # List available layers
            layer_list = [ds.GetLayer(i).GetName() for i in range(ds.GetLayerCount())]
            sys.exit(f"ERROR: Layer '{layer_name}' not found. Available layers: {', '.join(layer_list)}")
    else:
        # If no layer specified, use the first layer
        if ds.GetLayerCount() > 1:
            layer_list = [ds.GetLayer(i).GetName() for i in range(ds.GetLayerCount())]
            print(f"WARNING: Multiple layers found. Using first layer. Available layers: {', '.join(layer_list)}")
            print("TIP: Specify a layer with --layer=<name>")
        
        layer = ds.GetLayer(0)
        if layer is None:
            sys.exit("ERROR: No layers found in dataset")

    # Apply attribute filter if where clause provided
    if where:
        layer.SetAttributeFilter(where)

    return layer

def setup_reprojection(layer, target_epsg=4326):
    """Setup coordinate transformation if reprojection is needed"""
    source_srs = layer.GetSpatialRef()
    if not source_srs:
        print("WARNING: Source layer has no spatial reference system defined.")
        return None

    target_srs = osr.SpatialReference()
    if isinstance(target_epsg, str) and target_epsg.startswith("EPSG:"):
        target_epsg = int(target_epsg.split(":")[-1])
    
    target_srs.ImportFromEPSG(target_epsg)
    
    # Check if source and target are the same
    if source_srs.IsSame(target_srs):
        return None
    
    # Create transformation
    return osr.CoordinateTransformation(source_srs, target_srs)

def process_feature(feat, coord_transform=None, simplify=None, buffer=None,
                   id_field=None, property_list=None, exclude_props=None):
    """Process a single feature and convert to GeoJSON"""
    # Clone the feature to avoid modifying the original
    feat_clone = feat.Clone()
    
    # Get geometry
    geom = feat_clone.GetGeometryRef()
    if geom is None:
        return None  # Skip features without geometry
    
    # Apply transformations to geometry
    if coord_transform:
        geom.Transform(coord_transform)
    
    if simplify is not None and simplify > 0:
        geom = geom.SimplifyPreserveTopology(simplify)
    
    if buffer is not None and buffer > 0:
        geom = geom.Buffer(buffer)
    
    # Create GeoJSON feature
    feature = {
        "type": "Feature",
        "geometry": json.loads(geom.ExportToJson()),
        "properties": {}
    }
    
    # Set ID if specified
    if id_field:
        if feat_clone.GetField(id_field) is not None:
            feature["id"] = feat_clone.GetField(id_field)
    
    # Get all fields
    feat_defn = feat_clone.GetDefnRef()
    field_count = feat_defn.GetFieldCount()
    
    # Convert property list to set for faster lookups
    if property_list:
        property_set = set(property_list.split(','))
    else:
        property_set = None
    
    # Convert exclude list to set
    if exclude_props:
        exclude_set = set(exclude_props.split(','))
    else:
        exclude_set = set()
    
    # Add properties
    for i in range(field_count):
        field_defn = feat_defn.GetFieldDefn(i)
        field_name = field_defn.GetName()
        
        # Skip if not in property_list or in exclude_props
        if (property_set and field_name not in property_set) or field_name in exclude_set:
            continue
        
        # Get field value
        if feat_clone.IsFieldNull(i):
            continue  # Skip null fields
        
        field_type = field_defn.GetType()
        
        if field_type == ogr.OFTInteger:
            value = feat_clone.GetFieldAsInteger(i)
        elif field_type == ogr.OFTInteger64:
            value = feat_clone.GetFieldAsInteger64(i)
        elif field_type == ogr.OFTReal:
            value = feat_clone.GetFieldAsDouble(i)
        elif field_type == ogr.OFTString:
            value = feat_clone.GetFieldAsString(i)
        elif field_type == ogr.OFTBinary:
            continue  # Skip binary fields
        elif field_type == ogr.OFTDate or field_type == ogr.OFTTime or field_type == ogr.OFTDateTime:
            value = feat_clone.GetFieldAsString(i)
        else:
            value = feat_clone.GetFieldAsString(i)
        
        feature["properties"][field_name] = value
    
    return feature

def convert_to_geojsonseq(input_path, output_path, layer_name=None, where=None, sql=None, 
                     limit=None, simplify=None, reproject=None, id_field=None,
                     property_list=None, exclude_props=None, buffer=None, verbose=False,
                     batch_size=10000):
    """Convert input data to newline-delimited GeoJSON
    
    Can be called directly from other scripts like runCreateTiles.py
    
    Args:
        input_path (str): Input data source (file or connection string)
        output_path (str): Output newline-delimited GeoJSON file
        layer_name (str, optional): Layer/table name (required for multi-layer sources)
        where (str, optional): SQL WHERE clause to filter features
        sql (str, optional): Custom SQL query to execute
        limit (int, optional): Limit the number of features to process
        simplify (float, optional): Simplify geometries (tolerance in layer units)
        reproject (str, optional): Reproject to another CRS (e.g., EPSG:4326)
        id_field (str, optional): Field to use as feature ID
        property_list (str, optional): Comma-separated list of properties to include
        exclude_props (str, optional): Comma-separated list of properties to exclude
        buffer (float, optional): Create a buffer around geometries (in layer units)
        verbose (bool, optional): Show detailed progress
        batch_size (int, optional): Number of features to process in each batch
        
    Returns:
        tuple: (processed_count, skipped_count, output_path)
    """
    # Open dataset
    ds = open_dataset(input_path)
    
    # Get layer
    layer = get_layer_from_dataset(ds, layer_name, sql, where)
    
    # Setup reprojection if needed
    coord_transform = None
    if reproject:
        if reproject.lower() == "epsg:4326" or reproject == "4326":
            coord_transform = setup_reprojection(layer, 4326)
        else:
            try:
                epsg = int(reproject.split(":")[-1]) if ":" in reproject else int(reproject)
                coord_transform = setup_reprojection(layer, epsg)
            except ValueError:
                sys.exit(f"ERROR: Invalid EPSG code: {reproject}")
    
    # Get feature count if possible
    feat_count = layer.GetFeatureCount(force=0)  # Don't force count
    if feat_count < 0:
        if verbose:
            print("Feature count unknown, processing without progress bar")
        progress_bar = None
    else:
        if limit and limit < feat_count:
            feat_count = limit
        
        if verbose:
            print(f"Processing {feat_count} features")
        
        progress_bar = tqdm(total=feat_count, desc="Converting", unit="features")
    
    # Process the data in batches
    processed = 0
    skipped = 0
    
    # Create output directory if needed
    output_dir = os.path.dirname(output_path)
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir)
    
    # Process features and write to output
    with open(output_path, 'w') as f:
        batch_start_time = time.time()
        
        # Iterate through features
        layer.ResetReading()
        feat = layer.GetNextFeature()
        batch_count = 0
        
        while feat is not None:
            # Process and write feature
            geojson_feat = process_feature(
                feat, 
                coord_transform=coord_transform,
                simplify=simplify,
                buffer=buffer,
                id_field=id_field,
                property_list=property_list,
                exclude_props=exclude_props
            )
            
            if geojson_feat:
                f.write(json.dumps(geojson_feat) + '\n')
                processed += 1
            else:
                skipped += 1
            
            # Update progress
            if progress_bar:
                progress_bar.update(1)
            
            # Print batch statistics if verbose
            batch_count += 1
            if verbose and batch_count % batch_size == 0:
                batch_time = time.time() - batch_start_time
                feat_per_sec = batch_size / max(0.001, batch_time)
                print(f"Batch processed: {batch_count} features, {feat_per_sec:.1f} features/sec")
                batch_start_time = time.time()
            
            # Check if we've reached the limit
            if limit and processed >= limit:
                break
            
            # Get next feature
            feat = layer.GetNextFeature()
    
    # Clean up
    if progress_bar:
        progress_bar.close()
    
    # Release resources
    if sql:
        ds.ReleaseResultSet(layer)
    layer = None
    ds = None
    
    if verbose:
        print(f"Conversion complete: {processed} features processed, {skipped} features skipped")
        print(f"Output written to: {output_path}")
    
    return (processed, skipped, output_path)

def get_dataset_info(input_path):
    """Get basic information about the dataset"""
    ds = open_dataset(input_path)
    
    info = {
        "driver": ds.GetDriver().GetName(),
        "layer_count": ds.GetLayerCount(),
        "layers": []
    }
    
    for i in range(ds.GetLayerCount()):
        layer = ds.GetLayer(i)
        layer_info = {
            "name": layer.GetName(),
            "feature_count": layer.GetFeatureCount(),
            "geometry_type": ogr.GeometryTypeToName(layer.GetGeomType()),
            "spatial_ref": layer.GetSpatialRef().GetName() if layer.GetSpatialRef() else "Unknown"
        }
        info["layers"].append(layer_info)
    
    return info

def main():
    """Main entry point for command line usage"""
    parser = create_parser()
    args = parser.parse_args()
    
    # Check if input file exists (for file-based sources)
    if ":" not in args.input and not os.path.exists(args.input):
        sys.exit(f"ERROR: Input file not found: {args.input}")
    
    # Print dataset info if verbose
    if args.verbose:
        try:
            info = get_dataset_info(args.input)
            print(f"Input dataset: {args.input}")
            print(f"Driver: {info['driver']}")
            print(f"Layers: {info['layer_count']}")
            
            for layer in info["layers"]:
                print(f"  - {layer['name']}: {layer['feature_count']} features, {layer['geometry_type']}")
        except Exception as e:
            print(f"WARNING: Could not get dataset info: {str(e)}")
    
    # Perform the conversion
    convert_to_geojsonseq(
        args.input, args.output, args.layer, args.where, args.sql,
        args.limit, args.simplify, args.reproject, args.id_field,
        args.property_list, args.exclude_props, args.buffer, args.verbose,
        args.batch_size
    )

def convert_file(input_path, output_path, **kwargs):
    """
    Convenience function for external scripts to convert a file.
    
    Args:
        input_path: Path to input file or data source
        output_path: Path to output GeoJSONSeq file
        **kwargs: Additional arguments for convert_to_geojsonseq
    
    Returns:
        tuple: (processed_count, skipped_count, output_path)
    """
    # Set default verbose to True for external calls
    if 'verbose' not in kwargs:
        kwargs['verbose'] = True
        
    # Register all OGR drivers if not already done
    ogr.RegisterAll()
        
    # Call the main conversion function
    return convert_to_geojsonseq(input_path, output_path, **kwargs)

if __name__ == "__main__":
    # Register all OGR drivers
    ogr.RegisterAll()
    main()
