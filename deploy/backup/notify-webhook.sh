#!/bin/sh
# Phase 9B-8C: PREPARED, NOT ACTIVE -- the one sender behind the optional alert channels (installed as /usr/local/lib/napsedge-backup/notify-webhook.sh).
#   notify-webhook.sh discord <unit>      post "ATTENTION: <unit> on <host> did not succeed" to a Discord webhook
#   notify-webhook.sh discord-test        post ONE clearly labelled test message (only when Anthony authorizes it)
#   notify-webhook.sh deadman             ping a dead-man's-switch check URL (only called after a successful cycle)
# The URL is a SECRET: it is read from the systemd credential directory and given to curl on STDIN (`-K -`), never as an argument,
# so it never appears in the process list or the journal. Messages name the unit and host only. Exit status is curl's.
set -eu
KIND="${1:?discord|deadman}"
CURL="${NAPSEDGE_NOTIFY_CURL:-/usr/bin/curl}"
case "$KIND" in
  discord)
    UNIT="${2:?unit name}"
    URL_FILE="${CREDENTIALS_DIRECTORY:?not started by systemd with credentials}/discord-webhook"
    BODY=$(printf '{"content":"NapsEdge backup ATTENTION: %s on %s did not succeed. Check: journalctl -u %s"}' "$UNIT" "$(hostname)" "$UNIT")
    { printf 'url = "'; tr -d '\r\n' < "$URL_FILE"; printf '"\n'; } | "$CURL" -fsS --max-time 20 -K - -H "Content-Type: application/json" -d "$BODY"
    ;;
  discord-test)
    # the ONE authorized test message (says it is a test; names the host only)
    URL_FILE="${CREDENTIALS_DIRECTORY:?not started by systemd with credentials}/discord-webhook"
    BODY=$(printf '{"content":"NapsEdge backup alert TEST from %s: this channel will receive backup failure alerts. No action needed."}' "$(hostname)")
    { printf 'url = "'; tr -d '
' < "$URL_FILE"; printf '"
'; } | "$CURL" -fsS --max-time 20 -K - -H "Content-Type: application/json" -d "$BODY"
    ;;
  deadman)
    URL_FILE="${CREDENTIALS_DIRECTORY:?not started by systemd with credentials}/deadman-ping-url"
    { printf 'url = "'; tr -d '\r\n' < "$URL_FILE"; printf '"\n'; } | "$CURL" -fsS --max-time 20 --retry 3 -K - -o /dev/null
    ;;
  *) echo "unknown kind" >&2; exit 2 ;;
esac
