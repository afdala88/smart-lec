#!/usr/bin/env bash
# Build the upload bundle for the VM (run in Git Bash on the lab PC, from bagworm_app/).
# Stop the local Flask app first so census.db is not being written.
#
#   bash deploy/make_bundle.sh            -> ../bagworm_bundle.tar.gz
#
# Ships code, the best.pt models actually used, census.db, data_estate.xlsx and the
# Google Calendar credentials. Leaves out old job scratch dirs, training leftovers
# (last.pt, det_yv5/v1), temp_suport outputs, the separate WhatsApp agent and caches.
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=../bagworm_bundle.tar.gz

tar -czf "$OUT" \
    --exclude='./yolov5_inf/_job_*' \
    --exclude='*/last.pt' \
    --exclude='./yolov5_inf/model_fix/det_yv5/v1' \
    --exclude='./temp_suport' \
    --exclude='./uploads' \
    --exclude='./agent' \
    --exclude='./census.db.bak_*' \
    --exclude='*/__pycache__' \
    --exclude='*.pyc' \
    .

ls -lh "$OUT"
echo "Copy it to the VM together with deploy/setup_vm.sh"
