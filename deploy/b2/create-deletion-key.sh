#!/bin/sh
# Phase 9B-8C: create the separate DELETION key for the one-time real-B2 retention-deletion test, inside a THROWAWAY container
# (run with --rm), writing it straight into its OWN Docker named volume at /v (files del-key-id / del-secret, uid 10001, mode 0400).
# It is kept apart from the upload and read-only keys (another volume), mounted read-only only into the deletion run, and revoked
# afterwards. Only key NAMES and capabilities are printed; no key reaches a host file, an argument or shell history.
#
# napsedge-validation-del: listFiles, readFiles, deleteFiles -- it can PERMANENTLY delete any version under the prefix (that is its
# purpose). It cannot upload, overwrite or hide (no writeFiles). Limited to one bucket and the validation prefix.
#
# Bootstrap exactly as create-validation-keys.sh: the master key is typed at the b2 CLI's prompt, its authorization cache lives in
# this container's /tmp only, is cleared and proven gone. Rotate the master key afterwards.
# Usage: sh create-deletion-key.sh <bucket name> <validation prefix, one segment>
set -eu
BUCKET="$1"
PREFIX="$2"
case "$PREFIX" in */*|"") echo "the prefix must be a single segment without slashes"; exit 2 ;; esac
for f in del-key-id del-secret; do
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
b2 key create --bucket "$BUCKET" --name-prefix "$PREFIX/" napsedge-validation-del listFiles,readFiles,deleteFiles > /tmp/del
# shellcheck disable=SC2046
set -- $(cat /tmp/del)
[ "$#" -eq 2 ] || { echo "REFUSED: unexpected 'b2 key create' output format (nothing written)"; rm -f /tmp/del; exit 4; }
printf '%s\n' "$1" > /v/del-key-id
printf '%s\n' "$2" > /v/del-secret
set --
chown 10001:10001 /v/del-key-id /v/del-secret
chmod 0400 /v/del-key-id /v/del-secret
rm -f /tmp/del
b2 account clear >/dev/null 2>&1 || true
rm -f "$B2_ACCOUNT_INFO" "$B2_ACCOUNT_INFO"-journal
leftover=$(find / -xdev \( -name 'b2-account-info*' -o -name '.b2_account_info*' -o -path '*/b2/account_info*' \) 2>/dev/null | wc -l)
[ "$leftover" -eq 0 ] || { echo "WARNING: a b2 authorization cache file remains ($leftover)"; exit 5; }
[ "$(ls -A /v | sort | tr '\n' ' ')" = "del-key-id del-secret " ] || { echo "WARNING: unexpected files in the volume"; exit 6; }
echo "created napsedge-validation-del (listFiles,readFiles,deleteFiles); bucket $BUCKET, prefix $PREFIX/"
echo "cleanup: master-key authorization cache removed (0 files); the volume holds only the two key files:"
ls -ln /v | sed 's/^/  /'
