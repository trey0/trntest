#!/bin/bash
# Phase 4: render the synthetic image using the real SPICE-derived camera from Phase 2/3.
# Run inside the container: docker compose run --rm demo scripts/run_sat_sim.sh
set -euo pipefail

OUT_DIR=/workspace/output
CAMERA="$OUT_DIR/camera_frame440.tsai"

# Written by fetch_lunaserv.py's fetch_dem_and_ortho() -- do not glob the cache dir for "any"
# ortho/DEM tile, it can hold tiles from more than one footprint.
source "$OUT_DIR/lunaserv_result.txt"

echo "$CAMERA" > "$OUT_DIR/camera_list.txt"

sat_sim \
  --dem "$DEM" \
  --ortho "$ORTHO" \
  --camera-list "$OUT_DIR/camera_list.txt" \
  --image-size 256 256 \
  -o "$OUT_DIR/render/run"

# --save-as-csm only applies to cameras sat_sim itself generates, not ones passed via
# --camera-list -- convert the rendered image's exact camera to a CSM Frame model-state JSON
# ("ISD sidecar") with cam_gen instead. --refine-intrinsics none keeps the pose/intrinsics exact
# (no re-solving), so this is purely a format conversion of our already-computed SPICE pose.
cam_gen "$OUT_DIR/render/run-camera_frame440.tif" \
  --input-camera "$CAMERA" \
  --camera-type pinhole \
  --refine-intrinsics none \
  -o "$OUT_DIR/render/run-camera_frame440.json"
