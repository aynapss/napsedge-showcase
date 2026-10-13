#!/bin/sh
# Phase 9B-8C: store ONE alert URL (a secret) in the protected Docker volume mounted at /v, typed or pasted at a HIDDEN prompt.
# The URL never reaches an argument, shell history, the screen, a host file or the repository. Only its host and length are printed;
# a refusal prints a MASKED description (host and the first two path words, then digits as 9 and letters as a), never the token.
#   discord-webhook   https://discord.com/api/webhooks/<id>/<token>   (also discordapp.com, canary./ptb., /api/v10/)
#   deadman-ping-url  https://hc-ping.com/<uuid> or https://hc-ping.com/<ping-key>/<slug>   (healthchecks.io)
# Usage: run in the backup image as root with the volume at /v and -it:  sh store-alert-secret.sh discord-webhook|deadman-ping-url
set -eu
NAME="${1:?discord-webhook|deadman-ping-url}"
case "$NAME" in
  discord-webhook) PATTERN='^https://((canary|ptb)\.)?(discord|discordapp)\.com/api(/v[0-9]+)?/webhooks/[0-9]+/[A-Za-z0-9_.-]+$' ;;
  deadman-ping-url) PATTERN='^https://hc-ping\.com/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[A-Za-z0-9_-]{22}/[a-z0-9_-]+)$' ;;
  *) echo "unknown secret name"; exit 2 ;;
esac
[ ! -e "/v/$NAME" ] || { echo "REFUSED: /v/$NAME already exists (remove it deliberately first)"; exit 3; }
stty -echo 2>/dev/null || true
printf "Paste the %s URL, then Enter (nothing is shown): " "$NAME"
IFS= read -r url || true
stty echo 2>/dev/null || true
echo
raw_len=${#url}
# drop terminal paste markers (ESC[200~ ... ESC[201~), every control character, and surrounding spaces
ESC=$(printf '\033')
url=$(printf '%s' "$url" | sed "s/${ESC}\[20[01]~//g" | tr -d '\000-\037\177' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
if ! printf '%s' "$url" | grep -Eq "$PATTERN"; then
  start=$(printf '%s' "$url" | sed -E 's#^([a-z]+://[^/]*/?[^/]*/?[^/]*/?).*$#\1#' | cut -c1-60)
  rest=$(printf '%s' "$url" | cut -c$(( ${#start} + 1 ))- | sed 's/[0-9]/9/g; s/[A-Za-z]/a/g' | cut -c1-12)
  echo "REFUSED: that does not look like a $NAME URL (nothing stored)."
  echo "  received $raw_len characters ($((raw_len - ${#url})) removed as control/paste characters); kept ${#url}"
  echo "  starts with: '$start'   then (masked): '$rest...'"
  exit 4
fi
umask 077
printf '%s\n' "$url" > "/v/$NAME"
chown 10001:10001 "/v/$NAME"
chmod 0400 "/v/$NAME"
host=$(printf '%s' "$url" | sed -E 's#^https://([^/]+)/.*#\1#')
echo "stored $NAME (host $host, ${#url} characters); volume now holds: $(ls -A /v | tr '\n' ' ')"
