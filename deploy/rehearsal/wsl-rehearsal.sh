#!/bin/sh
# Phase 9B-8C: a NO-COST local rehearsal of the deployment's systemd activation, inside WSL Ubuntu (systemd + Docker Desktop
# integration). It installs the REAL units from deploy/systemd unchanged, against ONE disposable synthetic PostgreSQL source held
# open by tests/postgres/test_wsl_rehearsal_source.py, with the off-site copy DISABLED (no B2 keys exist) and NO alert sender
# installed (the journal-only hook only). Nothing is sent anywhere; healthchecks.io stays paused; no timer is enabled.
#
#   sudo sh wsl-rehearsal.sh install <rehearsal dir> <repo dir> <backup image tag>
#   sudo sh wsl-rehearsal.sh refresh-units <repo dir>   (after a unit-file fix; keeps credentials, config and volumes)
#   sudo sh wsl-rehearsal.sh run
#   sudo sh wsl-rehearsal.sh remove
#
# Rehearsal-only differences from a Droplet (stated, not hidden):
#   - Docker Desktop provides the engine, so there is no docker.service; a stand-in oneshot `docker.service` that only checks the
#     engine answers is installed (the real units keep their Requires=/After=docker.service unchanged);
#   - the off-site credentials are DUMMY values (off-site is disabled; the units still load them);
#   - systemd-creds uses this WSL instance's own host key (not a TPM).
# Everything it creates is removed by `remove`: the units, the stand-in, /etc/napsedge-backup, the encrypted credentials and the three
# named volumes it created (only if they carry its label). The protected volume bonnieswebscraping_napsedge-local-db-data is never touched.
set -eu
LABEL="org.napsedge.unit=9b8c-wsl"
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)  # absolute: Docker treats a relative -v source as a volume name
VOLUMES="napsedge-backup-scratch napsedge-backup-store napsedge-backup-state"
UNITS="napsedge-backup.service napsedge-backup.timer napsedge-backup-status.service napsedge-backup-status.timer napsedge-backup-notify@.service"
CREDS="napsedge-backup-db-password napsedge-backup-offsite-key-id napsedge-backup-offsite-secret-key napsedge-backup-offsite-read-key-id napsedge-backup-offsite-read-secret-key"
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 2; }
grep -qi microsoft /proc/version || { echo "REFUSED: this rehearsal runs only inside WSL"; exit 2; }
[ "$(ps -p 1 -o comm=)" = systemd ] || { echo "REFUSED: systemd is not PID 1 (set [boot] systemd=true in /etc/wsl.conf)"; exit 2; }
docker info >/dev/null 2>&1 || { echo "REFUSED: Docker is not reachable (enable Docker Desktop WSL integration for this distro)"; exit 2; }

case "${1:-}" in
install)
  DIR="${2:?rehearsal dir}"; REPO="${3:?repo dir}"; TAG="${4:?backup image tag}"
  [ -f "$DIR/READY" ] || { echo "REFUSED: $DIR/READY missing (start the disposable source first)"; exit 3; }
  for u in $UNITS; do [ ! -e "/etc/systemd/system/$u" ] || { echo "REFUSED: /etc/systemd/system/$u already exists (run remove first)"; exit 3; }; done
  docker image inspect "napsedge-backup:$TAG" >/dev/null || { echo "REFUSED: image napsedge-backup:$TAG not found"; exit 3; }
  install -d -m 0755 /etc/napsedge-backup /etc/napsedge-backup/ca /etc/napsedge-backup/recipients
  install -d -m 0700 /etc/credstore.encrypted
  install -m 0444 "$DIR/ca.pem" /etc/napsedge-backup/ca/ca.pem
  install -m 0640 "$DIR/backup.env" /etc/napsedge-backup/backup.env
  printf 'NAPSEDGE_BACKUP_IMAGE_TAG=%s\n' "$TAG" > /etc/napsedge-backup/unit.env
  # the public age recipient only (off-site is disabled; present so the configuration is complete)
  printf 'age144t6j57jc443d0csp0s7emt69n69ptv7p0wlzm4nym4xczucae7qzp6yy4\n' > /etc/napsedge-backup/recipients/recipients.txt
  # the SYNTHETIC password of the throwaway cluster goes in through stdin, then its plaintext file is removed
  systemd-creds encrypt --name=db-password - /etc/credstore.encrypted/napsedge-backup-db-password < "$DIR/db-password"
  rm -f "$DIR/db-password"
  for c in offsite-key-id offsite-secret-key; do
    printf 'unused-off-site-disabled\n' | systemd-creds encrypt --name="$c" - "/etc/credstore.encrypted/napsedge-backup-$c"
    printf 'unused-off-site-disabled\n' | systemd-creds encrypt --name="$c" - "/etc/credstore.encrypted/napsedge-backup-$(echo "$c" | sed 's/^offsite-/offsite-read-/')"
  done
  for v in $VOLUMES; do
    docker volume inspect "$v" >/dev/null 2>&1 && { echo "REFUSED: volume $v already exists"; exit 3; }
    docker volume create --label "$LABEL" "$v" >/dev/null
  done
  docker run --rm --network none --user 0:0 -v napsedge-backup-scratch:/a -v napsedge-backup-store:/b -v napsedge-backup-state:/c \
    --entrypoint sh "napsedge-backup:$TAG" -c 'chown 10001:10001 /a /b /c && chmod 0700 /a /b /c'
  if [ ! -e /etc/systemd/system/docker.service ] && ! systemctl cat docker.service >/dev/null 2>&1; then
    cat > /etc/systemd/system/docker.service <<'EOF'
# Phase 9B-8C WSL REHEARSAL STAND-IN (removed by wsl-rehearsal.sh remove): Docker Desktop provides the engine; this only checks it answers.
[Unit]
Description=Docker Desktop engine check (rehearsal stand-in)
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/docker info --format {{.ServerVersion}}
EOF
  fi
  for u in $UNITS; do install -m 0644 "$REPO/deploy/systemd/$u" "/etc/systemd/system/$u"; done
  systemctl daemon-reload
  echo "installed: $UNITS (timers NOT enabled), the docker.service stand-in, /etc/napsedge-backup, 5 encrypted credentials, 3 labelled volumes"
  systemd-analyze calendar --iterations=1 '*-*-* 03:30:00 UTC' | sed -n 's/^ *Next elapse: /timer would next run: /p'
  ;;
refresh-units)
  # replace ONLY the installed unit files with the repository's current versions (keeps credentials, config and volumes)
  REPO="${2:?repo dir}"
  for u in $UNITS; do [ -e "/etc/systemd/system/$u" ] || { echo "REFUSED: $u is not installed (run install)"; exit 3; }; done
  for u in $UNITS; do install -m 0644 "$REPO/deploy/systemd/$u" "/etc/systemd/system/$u"; done
  systemctl daemon-reload; systemctl reset-failed napsedge-backup.service napsedge-backup-status.service 2>/dev/null || true
  echo "refreshed: $UNITS"
  ;;
run)
  echo "== 1. one real cycle through systemd (off-site disabled)"
  systemctl start napsedge-backup.service && echo "napsedge-backup.service: success" || echo "napsedge-backup.service: FAILED (exit $?)"
  systemctl show napsedge-backup.service -p Result -p ExecMainStatus | tr '\n' ' '; echo
  docker run --rm --network none -v napsedge-backup-state:/s:ro --entrypoint sh "napsedge-backup:$(sed -n 's/^NAPSEDGE_BACKUP_IMAGE_TAG=//p' /etc/napsedge-backup/unit.env)" \
    -c 'tail -n 1 /s/cycles.jsonl' | python3 -c 'import json,sys; r=json.loads(sys.stdin.read()); print("cycle record: exit", r["exit_code"], "| outcome", r["outcome"], "| capture", r["capture"]["report"].get("outcome"), "| offsite", r["offsite"]["outcome"], "| states", r["states"])'
  echo "== 2. the status check through systemd (exit 10 would trigger the journal-only hook)"
  systemctl start napsedge-backup-status.service && echo "napsedge-backup-status.service: success (ok)" || echo "napsedge-backup-status.service: attention (exit 10 or failure)"
  journalctl -u napsedge-backup-status.service -n 40 -o cat | grep 'napsedge.backup-status/1' | tail -n 1 | python3 -c '
import json,sys
line=sys.stdin.read().strip(); r=json.loads(line[line.index("{"):])
print("status:", r["status"], "| alerts:", r["alerts"])
rr=r["recovery_ready"]; print("recovery ready:", rr["ready"])
for k,v in rr["criteria"].items(): print("   ", k, "met" if v["met"] else "NOT met", "-", v["detail"])'
  echo "== 3. the dead-man heartbeat gate on this run's record (off-site disabled -> must NOT ping)"
  TAG=$(sed -n 's/^NAPSEDGE_BACKUP_IMAGE_TAG=//p' /etc/napsedge-backup/unit.env)
  docker run --rm --read-only --network none --user 10001:10001 -v napsedge-backup-state:/s:ro -v "$SCRIPT_DIR/../backup/verified-offsite-gate.py:/g.py:ro" \
    --entrypoint python "napsedge-backup:$TAG" -c "import subprocess,os; n=os.path.getsize('/s/cycles.jsonl'); data=open('/s/cycles.jsonl','rb').read(); start=data.rstrip(b'\n').rfind(b'\n')+1; print(subprocess.run(['python','/g.py','/s/cycles.jsonl','--offset',str(start)],capture_output=True,text=True).stdout.strip())"
  echo "== 4. a forced failure triggers OnFailure= (the journal-only hook; nothing is sent)"
  install -d /run/systemd/system/napsedge-backup.service.d
  printf '[Service]\nExecStartPre=/bin/false\n' > /run/systemd/system/napsedge-backup.service.d/rehearsal-fail.conf
  systemctl daemon-reload
  systemctl start napsedge-backup.service 2>/dev/null && echo "UNEXPECTED: the forced failure succeeded" || echo "forced failure: the unit failed as intended"
  rm -f /run/systemd/system/napsedge-backup.service.d/rehearsal-fail.conf; systemctl daemon-reload
  sleep 2
  journalctl -t napsedge-backup-notify -n 1 -o cat
  systemctl reset-failed napsedge-backup.service 2>/dev/null || true
  ;;
remove)
  for u in napsedge-backup.timer napsedge-backup-status.timer napsedge-backup.service napsedge-backup-status.service; do systemctl stop "$u" 2>/dev/null || true; done
  for u in $UNITS; do rm -f "/etc/systemd/system/$u"; done
  rm -rf /run/systemd/system/napsedge-backup.service.d
  if grep -q "WSL REHEARSAL STAND-IN" /etc/systemd/system/docker.service 2>/dev/null; then systemctl stop docker.service 2>/dev/null || true; rm -f /etc/systemd/system/docker.service; fi
  for c in $CREDS; do rm -f "/etc/credstore.encrypted/$c"; done
  rmdir /etc/credstore.encrypted 2>/dev/null || true
  rm -rf /etc/napsedge-backup
  for v in $VOLUMES; do
    if [ "$(docker volume inspect --format '{{index .Labels "org.napsedge.unit"}}' "$v" 2>/dev/null)" = "9b8c-wsl" ]; then docker volume rm "$v" >/dev/null && echo "removed volume $v"; fi
  done
  systemctl daemon-reload; systemctl reset-failed 2>/dev/null || true
  echo "removed: units, stand-in, /etc/napsedge-backup, encrypted credentials, labelled volumes"
  ;;
*) echo "usage: sudo sh wsl-rehearsal.sh install <rehearsal dir> <repo dir> <tag> | refresh-units <repo dir> | run | remove"; exit 2 ;;
esac
