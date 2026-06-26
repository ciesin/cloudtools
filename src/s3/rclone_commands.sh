# Multi-GB: few parallel streams, large chunks
LARGE_FILE_FLAGS = {
    "--s3-chunk-size":        "256M",
    "--s3-upload-concurrency": "8",
    "--multi-thread-streams": "8",
    "--multi-thread-cutoff":  "64M",
    "--buffer-size":          "64M",
    "--transfers":            "4",
    "--checkers":             "8",
    "--fast-list":            None,
    "--retries":              "3",
    "--progress":             None,
    "--stats":                "30s",
    "--s3-no-check-bucket":   None,
}

# Many small files: STAC JSON, metadata, map images
SMALL_FILES_FLAGS = {
    "--transfers":          "32",
    "--checkers":           "32",
    "--fast-list":          None,
    "--size-only":          None,
    # "--stats":              "30s",
    "--s3-no-check-bucket": None,
}

# R2->R2 bucket sync: server-side copy, skip checksum (avoid R2 hash charges)
R2_SYNC_FLAGS = {
    "--s3-chunk-size":        "256M",
    "--s3-upload-concurrency": "8",
    "--fast-list":            None,
    "--size-only":            None,
    "--transfers":            "4",
    "--checkers":             "8",
    "--stats":                "30s",
    "--s3-no-check-bucket":   None,
}

rclone moveto ciesin-r2:ciesin-dev/tiles/terrain.pmtiles ciesin-r2:ciesin-dev/alpha/terrain.pmtiles --progress --s3-no-check-bucket --s3-chunk-size=256M --header-upload "Content-Type: application/vnd.pmtiles"

rclone copy ciesin-r2:ciesin-dev/tiles/ ciesin-r2:ciesin-prod/tiles \
  --progress \
  --stats=1m \
  --s3-no-check-bucket \
  --s3-chunk-size=256M \
  --multi-thread-streams=16 \
  --multi-thread-cutoff=64M \
  --buffer-size=64M \
  --header-upload "Content-Type: application/vnd.pmtiles" \
  --dry-run


rclone copyto buildings_overview.pmtiles \
  ciesin-r2:ciesin-prod/tiles/overture/buildings_overview.pmtiles \
  --s3-chunk-size=256M --s3-upload-concurrency=8 --multi-thread-streams=8 --multi-thread-cutoff=64M --buffer-size=64M --transfers=4 --checkers=8 --fast-list --retries=3 --progress --stats=30s --s3-no-check-bucket \
  --header-upload "Content-Type: application/vnd.pmtiles"

rclone copyto GRID3_AFRICA_settlement_extents_v3_0.pmtiles \
  ciesin-r2:ciesin-dev/tiles/grid3/africa/GRID3_AFRICA_settlement_extents_v3_0.pmtiles \
  --progress \
  --s3-no-check-bucket \
  --s3-chunk-size=256M \
  --header-upload "Content-Type: application/vnd.pmtiles"


rclone copy . ciesin-r2:ciesin-dev/tiles/grid3/cod \
  --include "GRID3_COD_*.pmtiles" \
  --progress \
  --s3-no-check-bucket \
  --s3-chunk-size=256M \
  --header-upload "Content-Type: application/vnd.pmtiles"

rclone copy . ciesin-r2:ciesin-dev/tiles/grid3/nga \
  --include "GRID3_NGA_*.pmtiles" \
  --progress \
  --s3-no-check-bucket \
  --s3-chunk-size=256M \
  --multi-thread-streams=16 \
  --multi-thread-cutoff=64M \
  --buffer-size=64M \
  --header-upload "Content-Type: application/vnd.pmtiles" \
  --dry-run

rclone copy ciesin-r2:ciesin-dev/stac/ ciesin-r2:ciesin-prod/stac \
  --progress \
  --s3-no-check-bucket \
  --s3-chunk-size=256M \
  --multi-thread-streams=32 \
  --multi-thread-cutoff=64M \
  --checkers=32 \
  --buffer-size=64M \
  --header-upload "Content-Type: application/json" \
  --dry-run \
  --transfers=16 \
  --stats=1m \
  --retries=3 \
  --contimeout=60s


# between buckets
rclone sync ciesin-r2:ciesin-dev/ ciesin-r2:ciesin-prod \
  --server-side-across-configs \
  --fast-list \
  --size-only \
  --transfers 64 \
  --checkers 64 \
  -P

# checks
rclone check ciesin-r2:ciesin-dev ciesin-r2:ciesin-prod \
  --differ diff_$(date +%Y%m%d%H%M%S).txt \
  --missing-on-dst missing_on_dest_$(date +%Y%m%d%H%M%S).txt \
  --missing-on-src missing_on_src_$(date +%Y%m%d%H%M%S).txt \
  --match match_$(date +%Y%m%d%H%M%S).txt

rclone check ciesin-r2:ciesin-dev/tiles ciesin-r2:ciesin-prod/tiles \
  --differ diff_tiles_$(date +%Y%m%d%H%M%S).txt \
  --missing-on-dst missing_on_prod_$(date +%Y%m%d%H%M%S).txt \
  --missing-on-src missing_on_dev_$(date +%Y%m%d%H%M%S).txt \
  --match match_$(date +%Y%m%d%H%M%S).txt


