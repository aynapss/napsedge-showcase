#!/bin/sh
# Phase 9C (decision: Streamlit is PRIVATE only -- an SSH tunnel or a private network, no public login; approved 2026-10-09).
# Run ON THE SERVER (read-only; changes nothing). It fails (exit 1) if Streamlit (8501) or the API (8000) could be reached from
# outside the machine, and prints one line per check. Acceptance (readiness 15, 9C): "Streamlit is verifiably unreachable by an
# unauthenticated party in the target deployment environment" -- this script is the on-host half; the off-host half is the
# `curl` from your own PC in docs/phase9c_private_access.md, which must FAIL.
#
#   sh check-private-exposure.sh            (uses `ss` and `docker`; run as a user that may call docker)
#   NAPSEDGE_EXPOSURE_SS=<cmd> NAPSEDGE_EXPOSURE_DOCKER=<cmd>   override the two tools (tests only)
set -u
SS="${NAPSEDGE_EXPOSURE_SS:-ss}"
DOCKER="${NAPSEDGE_EXPOSURE_DOCKER:-docker}"
PORTS="8000 8501"
failures=0
fail() { echo "FAIL: $*"; failures=$((failures + 1)); }
ok() { echo "ok:   $*"; }

# 1. listening sockets: each private port may listen only on loopback (127.0.0.0/8 or ::1)
listeners=$($SS -ltnH 2>/dev/null | awk '{print $4}')
for port in $PORTS; do
  bound=$(printf '%s\n' "$listeners" | grep -E "[:.]${port}\$" || true)
  if [ -z "$bound" ]; then
    ok "port $port is not listening (service stopped)"
    continue
  fi
  public=$(printf '%s\n' "$bound" | grep -vE '^(127\.[0-9.]+|\[::1\]|::1):' || true)
  if [ -n "$public" ]; then fail "port $port listens on a non-loopback address: $(echo "$public" | tr '\n' ' ')"; else ok "port $port listens on loopback only"; fi
done

# 2. docker-published ports: nothing may be published on 0.0.0.0 / :: (all interfaces)
published=$($DOCKER ps --format '{{.Names}} {{.Ports}}' 2>/dev/null)
wide=$(printf '%s\n' "$published" | grep -E '(0\.0\.0\.0|\[::\]|:::)[0-9]*:?[0-9]*->' || true)
if [ -n "$wide" ]; then fail "containers publish on all interfaces: $(echo "$wide" | cut -d' ' -f1 | tr '\n' ' ')"; else ok "no container publishes a port on all interfaces"; fi

# 3. the API docs are reachable only where the API is (loopback); remind, do not probe outward
echo "note: /v1/docs and /v1/openapi.json are unauthenticated and stay private only because the API is loopback-bound"
if [ "$failures" -gt 0 ]; then echo "RESULT: EXPOSED ($failures problem(s))"; exit 1; fi
echo "RESULT: private (on-host checks passed; now run the off-host check from your own PC)"
