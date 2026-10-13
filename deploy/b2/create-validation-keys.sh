#!/bin/sh
# Phase 9B-8C: create the two SCOPED application keys for the B2 validation inside a THROWAWAY container (run with --rm), writing them
# straight into a Docker named volume mounted at /v (files owned by uid 10001, mode 0400). Only key NAMES and capabilities are printed;
# no key reaches a host file, an argument or shell history.
#
# Bootstrap: the master key is typed at the official b2 CLI's own prompt. Its authorization cache (B2_ACCOUNT_INFO) lives in this
# container's /tmp only; the script clears it, proves it is gone, and the container is discarded on exit. Rotate the master key
# afterwards; the scoped keys stay valid (check-keys.sh confirms).
#
# What the keys can and cannot do (Backblaze docs, cloud-storage-s3-compatible-app-keys and s3-delete-object):
#   napsedge-validation-rw: listFiles, readFiles, writeFiles. writeFiles ALSO allows overwriting an object (a new version) and
#       DeleteObject WITHOUT a version id (a delete marker becomes the current version: the object is hidden). It does NOT include
#       deleteFiles, so it cannot permanently delete a specific version. This is NOT immutability or append-only protection.
#   napsedge-validation-ro: listFiles, readFiles (cannot write, overwrite or hide).
# Both are limited to one bucket and the validation prefix.
# Usage (see docs/backup_b2_validation.md):  sh create-validation-keys.sh <bucket name> <validation prefix, one segment>
set -eu
BUCKET="$1"
PREFIX="$2"
case "$PREFIX" in */*|"") echo "the prefix must be a single segment without slashes"; exit 2 ;; esac
for f in rw-key-id rw-secret ro-key-id ro-secret; do
  [ ! -e "/v/$f" ] || { echo "REFUSED: /v/$f already exists (remove the volume deliberately first)"; exit 3; }
done
python -m pip install --quiet --no-cache-dir --disable-pip-version-check --root-user-action=ignore b2==5.0.0 >/dev/null
export B2_ACCOUNT_INFO=/tmp/b2-account-info
# The master key is read at OUR hidden prompts and handed to b2 in this container's environment only: `b2 account authorize`
# prints its whole login record (including the key and tokens), so its output is discarded entirely, and only success is shown.
printf "MASTER applicationKeyId: "; IFS= read -r B2_APPLICATION_KEY_ID
stty -echo 2>/dev/null || true; printf "MASTER applicationKey (hidden): "; IFS= read -r B2_APPLICATION_KEY; stty echo 2>/dev/null || true; echo
export B2_APPLICATION_KEY_ID B2_APPLICATION_KEY
b2 account authorize >/dev/null 2>&1 || { unset B2_APPLICATION_KEY_ID B2_APPLICATION_KEY; echo "REFUSED: master-key authorization failed (wrong key?)"; exit 7; }
unset B2_APPLICATION_KEY_ID B2_APPLICATION_KEY
echo "master-key authorization: OK (output suppressed)"
umask 077
b2 key create --bucket "$BUCKET" --name-prefix "$PREFIX/" napsedge-validation-rw listFiles,readFiles,writeFiles > /tmp/rw
b2 key create --bucket "$BUCKET" --name-prefix "$PREFIX/" napsedge-validation-ro listFiles,readFiles > /tmp/ro
for k in rw ro; do
  # shellcheck disable=SC2046
  set -- $(cat "/tmp/$k")
  [ "$#" -eq 2 ] || { echo "REFUSED: unexpected 'b2 key create' output format (nothing written for $k)"; exit 4; }
  printf '%s\n' "$1" > "/v/$k-key-id"
  printf '%s\n' "$2" > "/v/$k-secret"
done
chown 10001:10001 /v/rw-key-id /v/rw-secret /v/ro-key-id /v/ro-secret
chmod 0400 /v/rw-key-id /v/rw-secret /v/ro-key-id /v/ro-secret
rm -f /tmp/rw /tmp/ro
b2 account clear >/dev/null 2>&1 || true
rm -f "$B2_ACCOUNT_INFO" "$B2_ACCOUNT_INFO"-journal
# cleanup proof: no b2 authorization cache anywhere in this container, and the volume holds exactly the four key files
leftover=$(find / -xdev \( -name 'b2-account-info*' -o -name '.b2_account_info*' -o -path '*/b2/account_info*' \) 2>/dev/null | wc -l)
[ "$leftover" -eq 0 ] || { echo "WARNING: a b2 authorization cache file remains ($leftover)"; exit 5; }
[ "$(ls -A /v | sort | tr '\n' ' ')" = "ro-key-id ro-secret rw-key-id rw-secret " ] || { echo "WARNING: unexpected files in the volume"; exit 6; }
echo "created napsedge-validation-rw (listFiles,readFiles,writeFiles) and napsedge-validation-ro (listFiles,readFiles); bucket $BUCKET, prefix $PREFIX/"
echo "cleanup: master-key authorization cache removed (0 files); the volume holds only the four key files:"
ls -ln /v | sed 's/^/  /'
