gdal_translate GRID3_COD_mix_travel_time_friction_surface_v1.tif GRID3_COD_mix_travel_time_friction_surface_v1_cog.tif -of COG -co COMPRESS=LZW -co BIGTIFF=YES

gdal_translate GRID3_COD_walk_travel_time_friction_surface_v1.tif GRID3_COD_walk_travel_time_friction_surface_v1_cog.tif -of COG -co COMPRESS=LZW -co BIGTIFF=YES

gdal_translate GRID3_NGA_mix_travel_time_friction_surface_v1_0.tif GRID3_NGA_mix_travel_time_friction_surface_v1_0_cog.tif -of COG -co COMPRESS=LZW -co BIGTIFF=YES

gdal_translate GRID3_NGA_mix_travel_time_friction_surface_v1_0_cog.tif GRID3_NGA_mix_travel_time_friction_surface_v1_0_cog-lossy.tif \
  -of COG \
  -co COMPRESS=JPEG \
  -co NUM_THREADS=ALL_CPUS \
  -co BIGTIFF=IF_SAFER

gdal_translate -expand rgb GRID3_NGA_mix_travel_time_friction_surface_v1_0_grayExp.tif GRID3_NGA_mix_travel_time_friction_surface_v1_0_rgb.tif

gdal_translate GRID3_NGA_mix_travel_time_friction_surface_v1_0_gray.tif GRID3_NGA_mix_travel_time_friction_surface_v1_0_grayExp.tif -ot Byte -scale


gdal raster color-map GRID3_NGA_mix_travel_time_friction_surface_v1_0.tif GRID3_NGA_mix_travel_time_friction_surface_v1_0_gray.tif --color-map color.txt


gdal_translate GRID3_NGA_mix_travel_time_friction_surface_v1_0.tif GRID3_NGA_mix_travel_time_friction_surface_v1_0_gray-byte.tif -ot Byte -scale -expand gray

rio pmtiles GRID3_NGA_mix_travel_time_friction_surface_v1_0_grayExp.tif GRID3_NGA_mix_travel_time_friction_surface_v1_0_grayExp.pmtiles --format PNG --resampling bilinear


