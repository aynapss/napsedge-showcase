#!/bin/sh
# Phase 9B-8C: confirm the two scoped keys still authenticate (for example after the master key was rotated), using the DELIVERED
# adapter. Read-only: it only LISTS the validation prefix with each key. Prints OK/FAILED per key (exit 1 if any failed); never a key.
# Usage: run in the backup image with the credential volume mounted read-only at /run/napsedge-backup/b2:
#   sh check-keys.sh <endpoint url> <region> <bucket> <prefix>
set -eu
python - "$@" <<'PY'
import sys
from src.deployment import backup_offsite as off
failed = 0
endpoint, region, bucket, prefix = sys.argv[1:5]
for key in ("rw", "ro"):
    try:
        store = off.S3ObjectStore(endpoint_url=endpoint, region=region, bucket=bucket,
                                  key_id_file=f"/run/napsedge-backup/b2/{key}-key-id", secret_key_file=f"/run/napsedge-backup/b2/{key}-secret")
        store.list(prefix + "/")
        print(f"{key} key: OK (listed {prefix}/)")
    except off.OffsiteError as exc:
        print(f"{key} key: FAILED ({exc})")
        failed += 1
sys.exit(1 if failed else 0)
PY
