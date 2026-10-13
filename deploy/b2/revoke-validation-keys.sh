#!/bin/sh
# Phase 9B-8C: revoke the validation keys with the official b2 CLI, inside a THROWAWAY container (run with --rm, -it), for when the
# web console does not list them. It deletes ONLY the keys whose IDs are stored in the mounted credential volume (/v, read-only) AND
# whose B2 names match the expected `napsedge-validation-<rw|ro|del>`; any mismatch refuses before anything is deleted. It never
# prints a key, a key ID or a secret -- only key names and outcomes -- and proves afterwards that the IDs are gone from B2.
# Bootstrap as for key creation: the master key is typed at a hidden prompt (output of `b2 account authorize` suppressed); its cache stays in /tmp, is cleared and proven gone.
# Usage: sh revoke-validation-keys.sh rw ro     (volume napsedge-b2-cred)   |   sh revoke-validation-keys.sh del   (napsedge-b2-del-cred)
set -eu
[ "$#" -ge 1 ] || { echo "usage: revoke-validation-keys.sh rw|ro|del ..."; exit 2; }
for k in "$@"; do
  case "$k" in rw|ro|del) ;; *) echo "REFUSED: unknown key '$k'"; exit 2 ;; esac
  [ -r "/v/$k-key-id" ] || { echo "REFUSED: /v/$k-key-id is not in the mounted volume"; exit 3; }
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
b2 key list > /tmp/keys
# every requested key must be present under its expected name BEFORE anything is deleted
for k in "$@"; do
  id=$(tr -d '\r\n' < "/v/$k-key-id")
  awk -v id="$id" -v name="napsedge-validation-$k" '$1 == id && $2 == name {found=1} END {exit found ? 0 : 1}' /tmp/keys \
    || { echo "REFUSED: napsedge-validation-$k is not listed under its stored ID (already revoked, or a different key); nothing deleted"; rm -f /tmp/keys; exit 4; }
done
for k in "$@"; do
  b2 key delete "$(tr -d '\r\n' < "/v/$k-key-id")" >/dev/null
  echo "deleted napsedge-validation-$k"
done
b2 key list > /tmp/keys
for k in "$@"; do
  id=$(tr -d '\r\n' < "/v/$k-key-id")
  if awk -v id="$id" '$1 == id {found=1} END {exit found ? 0 : 1}' /tmp/keys; then echo "WARNING: napsedge-validation-$k is still listed"; exit 5; fi
  echo "confirmed: napsedge-validation-$k no longer listed"
done
rm -f /tmp/keys
b2 account clear >/dev/null 2>&1 || true
rm -f "$B2_ACCOUNT_INFO" "$B2_ACCOUNT_INFO"-journal
leftover=$(find / -xdev \( -name 'b2-account-info*' -o -name '.b2_account_info*' -o -path '*/b2/account_info*' \) 2>/dev/null | wc -l)
[ "$leftover" -eq 0 ] || { echo "WARNING: a b2 authorization cache file remains ($leftover)"; exit 6; }
echo "cleanup: master-key authorization cache removed (0 files)"
