#!/usr/bin/env bash
# Phase 9B-7B: the runbook's commands for the remote probe host, as shell functions (no framework; each function is one
# runbook command). Source it from the unpacked handoff package:   . ./source/tests/postgres/provider_probe_remote.sh
# Nothing here reads, prints or passes a password: credentials only ever move as files, into read-only volumes.
set -u

: "${PROBE_PACKAGE:=$(pwd)}"                                   # the ABSOLUTE path of the unpacked handoff package
: "${PROBE_NETWORK:=bridge}"                                    # the Docker network (bridge reaches the VPC through the host)
: "${BACKUP_IMAGE:=napsedge-backup:9b8c-20261009160344}"
: "${TOOLING_IMAGE:=napsedge-probe-tooling:9b8b-20261008220349}"
: "${PROBE_LABEL:=org.napsedge.unit=9b7b}"
: "${PYTHON3:=python3}"                                         # the host Python used only to read the job report
V_CONFIG=napsedge-probe-config; V_CRED=napsedge-probe-cred; V_JOBCRED=napsedge-probe-jobcred; V_CA=napsedge-probe-ca
V_EVIDENCE=napsedge-probe-evidence; V_STORE=napsedge-probe-store; V_SCRATCH=napsedge-probe-scratch
PROBE_VOLUMES="$V_CONFIG $V_CRED $V_JOBCRED $V_CA $V_EVIDENCE $V_STORE $V_SCRATCH"

probe_verify_images() {
  # Store-independent identity: the ordered layer diff IDs, user and entrypoint must equal the package's IMAGES.txt record.
  # (The image ID itself depends on the daemon's image store, so both recorded forms are shown, not required.)
  local tag want got
  for tag in "$BACKUP_IMAGE" "$TOOLING_IMAGE"; do
    want=$(awk -v t="$tag" '$1 == t {print $2}' "$PROBE_PACKAGE/IMAGES.txt")
    got=$(docker image inspect --format '{{json .RootFS.Layers}}|{{.Config.User}}|{{json .Config.Entrypoint}}|{{.Os}}/{{.Architecture}}' "$tag" | sha256sum | cut -c1-64)
    if [ -n "$want" ] && [ "$want" = "$got" ]; then echo "OK $tag content $got (id $(docker image inspect --format '{{.Id}}' "$tag"))"; else echo "MISMATCH $tag want=$want got=$got"; return 1; fi
  done
}

probe_helper() {  # a root helper from the tooling image that only prepares or reads volumes; no network
  docker run --rm -i --network none --user 0:0 --label "$PROBE_LABEL" "$@"
}

probe_create_volumes() {
  local v
  for v in $PROBE_VOLUMES; do
    if docker volume inspect "$v" >/dev/null 2>&1; then echo "REFUSED: volume $v already exists (remove it deliberately first)"; return 1; fi
  done
  for v in $PROBE_VOLUMES; do docker volume create --label "$PROBE_LABEL" "$v" >/dev/null || return 1; done
  probe_helper -v "$V_EVIDENCE:/e" -v "$V_STORE:/s" -v "$V_SCRATCH:/t" --entrypoint sh "$TOOLING_IMAGE" -c 'chown 10001:10001 /e /s /t && chmod 0700 /e /s /t'
}

probe_load_credentials() {  # $1: the ABSOLUTE path of the host directory holding admin, migrator, runtime, backup (mode 0700, files 0600)
  local dir="$1"
  probe_helper -v "$dir:/src:ro" -v "$V_CRED:/v" --entrypoint sh "$TOOLING_IMAGE" -c \
    'for f in admin migrator runtime backup; do test -s "/src/$f" || { echo "missing $f"; exit 1; }; cp "/src/$f" "/v/$f" && chown 10001:10001 "/v/$f" && chmod 0400 "/v/$f" || exit 1; done'
  probe_helper -v "$dir:/src:ro" -v "$V_JOBCRED:/v" --entrypoint sh "$TOOLING_IMAGE" -c \
    'cp /src/backup /v/backup && chown 10001:10001 /v/backup && chmod 0400 /v/backup'   # the job container sees ONLY the backup password
}

probe_load_ca() {  # $1: the ABSOLUTE path of the downloaded provider CA certificate file
  probe_helper -v "$1:/src/ca.crt:ro" -v "$V_CA:/v" --entrypoint sh "$TOOLING_IMAGE" -c 'cp /src/ca.crt /v/ca.pem && chmod 0444 /v/ca.pem'
}

probe_load_config() {  # $1: the ABSOLUTE path of the filled-in probe.json (no secret in it)
  probe_helper -v "$1:/src/probe.json:ro" -v "$V_CONFIG:/v" --entrypoint sh "$TOOLING_IMAGE" -c 'cp /src/probe.json /v/probe.json && chmod 0444 /v/probe.json'
}

PROBE() {  # the runbook's probe-tooling command; the repository is the package's source tree, read-only
  docker run --rm --read-only --tmpfs /tmp:rw,mode=1777 --user 10001:10001 --cap-drop ALL --security-opt no-new-privileges \
    --network "$PROBE_NETWORK" --label "$PROBE_LABEL" -e PYTHONDONTWRITEBYTECODE=1 \
    -v "$V_CONFIG:/probe/config:ro" -v "$V_CRED:/probe/credentials:ro" -v "$V_CA:/probe/ca:ro" -v "$V_EVIDENCE:/probe/evidence" \
    -v "$V_STORE:/probe/store:ro" --mount "type=bind,source=$PROBE_PACKAGE/source,target=/repo,readonly" \
    "$TOOLING_IMAGE" --config /probe/config/probe.json "$@"
}

probe_job_env() {  # copies the non-secret job.env written by `PROBE identity` to the host
  probe_helper -v "$V_EVIDENCE:/e:ro" --entrypoint cat "$TOOLING_IMAGE" /e/job.env > "$PROBE_PACKAGE/job.env"
}

JOB() {  # the runbook's backup job command (the retained image, unmodified)
  docker run --rm --read-only --user 10001:10001 --cap-drop ALL --security-opt no-new-privileges --network "$PROBE_NETWORK" \
    --label "$PROBE_LABEL" -v "$V_JOBCRED:/run/napsedge-backup/credentials:ro" -v "$V_CA:/run/napsedge-backup/ca:ro" \
    -v "$V_SCRATCH:/var/lib/napsedge-backup/scratch" -v "$V_STORE:/var/lib/napsedge-backup/store" \
    --env-file "$PROBE_PACKAGE/job.env" -e NAPSEDGE_LOG_LEVEL=WARNING "$BACKUP_IMAGE"
}

probe_evidence_has() {  # exit 0 when the evidence volume holds the named file
  probe_helper -v "$V_EVIDENCE:/e:ro" --entrypoint test "$TOOLING_IMAGE" -f "/e/$1"
}

probe_wait_for_lock() {  # $1: the background holder's PID; $2: seconds. Waits while the holder is alive; never forever.
  local pid="$1" limit="${2:-120}" waited=0
  while ! probe_evidence_has hold_lock.held; do
    if ! kill -0 "$pid" 2>/dev/null; then echo "the lock holder exited before holding the lock (see its evidence)"; return 1; fi
    if [ "$waited" -ge "$limit" ]; then echo "the lock holder did not report the lock within ${limit}s"; return 1; fi
    sleep 1; waited=$((waited + 1))
  done
}

probe_run_all() {  # $1 source database, $2 target database. The runbook's steps 1-8 in order; stops at the first unexpected exit.
  local src="$1" tgt="$2" work="${PROBE_WORK:-$PROBE_PACKAGE/run}" code holder artifact
  mkdir -p "$work" || return 1
  step() { echo "STEP $1 exit=$2"; [ "$2" = "$3" ] || { echo "STOPPED at $1: expected exit $3, got $2 (evidence kept; on exit 2 this is a provider blocker)"; return 1; }; }
  PROBE inspect;                         step inspect $? 0 || return 1
  PROBE setup --confirm "$src";          step setup $? 0 || return 1          # MUTATION: roles + source database
  PROBE identity;                        step identity $? 0 || return 1
  probe_job_env || return 1
  JOB > "$work/job1.json";               step job $? 0 || return 1            # MUTATION: one artifact
  echo "STEP scratch entries=[$(probe_scratch_entries | tr '\n' ' ')]"
  PROBE hold-lock --seconds 60 & holder=$!
  probe_wait_for_lock "$holder" 120 || { wait "$holder"; step lock-holder $? 0; return 1; }
  JOB > "$work/job2.json";               step overlap $? 3 || { wait "$holder"; return 1; }
  wait "$holder";                        step holder $? 0 || return 1
  artifact=$("$PYTHON3" -c 'import json,sys; print(json.loads(open(sys.argv[1]).read().splitlines()[-1])["artifact"])' "$work/job1.json") || return 1
  PROBE verify-restore --confirm "$tgt" --artifact "/probe/store/$artifact"; step verify-restore $? 0 || return 1   # MUTATION: target
  PROBE residue;                         step residue $? 0 || return 1
  PROBE cleanup --confirm cleanup;       step cleanup $? 0 || return 1          # MUTATION: drops the ledger's resources
  echo "ALL STEPS PASSED (this endpoint, now; not recovery readiness)"
}

probe_scratch_entries() {
  probe_helper -v "$V_SCRATCH:/t:ro" --entrypoint ls "$TOOLING_IMAGE" -A /t
}

probe_export_evidence() {  # $1: output tar on the host (sanitized evidence only; never the credential volumes)
  probe_helper -v "$V_EVIDENCE:/e:ro" --entrypoint tar "$TOOLING_IMAGE" -C /e -cf - . > "$1"
}

probe_remove_volumes() {  # only the probe's own labelled volumes
  local v
  for v in $PROBE_VOLUMES; do docker volume rm "$v" >/dev/null 2>&1 && echo "removed $v"; done
  docker ps -aq --filter "label=$PROBE_LABEL" | wc -l | sed 's/^/labelled containers left: /'
}
