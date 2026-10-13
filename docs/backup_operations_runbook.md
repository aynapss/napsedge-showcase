# NapsEdge Backup Operations Runbook (Phase 9B-8B)

**Status (2026-10-08):**
- **Implemented and verified locally.** Scheduling, encrypted off-site transfer, retrieval, restore drills and retention were exercised against disposable PostgreSQL clusters, the real `age` tool and boto3 over HTTP to an S3 **emulator** (moto). They were not exercised against Backblaze B2.
- **Live storage validation: NOT done.** No storage account exists.
- **Provider compatibility: NOT verified.** The DigitalOcean probe is deferred.
- **Demonstrated recovery on real infrastructure: NOT done.**
- **Inactive:** nothing is scheduled; off-site transfer and deletion are disabled by default.
- Phase 9B remains incomplete.

---

## 1. Configured targets (initial defaults; configured, not measured)

| Setting | Default | Variable |
|---|---|---|
| Capture cycle | once daily, **03:30 UTC** (`OnCalendar=*-*-* 03:30:00 UTC`, up to 10 min random delay) | systemd timer |
| Extra cycle after a controlled import | optional: `systemctl start napsedge-backup.service` once the import has succeeded | operator / import hook |
| Recovery-point target (RPO) | **24 h**: the newest remotely verified copy must be younger | `NAPSEDGE_BACKUP_RPO_HOURS` |
| Recovery-time target (RTO) | **4 h**, from the decision to restore to a verified restored database | `NAPSEDGE_BACKUP_RTO_HOURS` |
| Retention | **7 daily, 4 weekly, 6 monthly**, bucketed in UTC | `NAPSEDGE_BACKUP_RETAIN_*` |
| Restore drill | **one per calendar month**; overdue after 31 days | `NAPSEDGE_BACKUP_DRILL_INTERVAL_DAYS` |
| Overlap retries | 4 capture attempts, 900 s apart, within a 2 h cycle deadline | `NAPSEDGE_BACKUP_OVERLAP_*`, `NAPSEDGE_BACKUP_CYCLE_DEADLINE_SECONDS` |
| Upload retries | 3 attempts, backoff 30 s × attempt | `NAPSEDGE_BACKUP_UPLOAD_*` |
| Off-site transfer / deletion | **disabled** | `NAPSEDGE_BACKUP_OFFSITE_ENABLED`, `NAPSEDGE_BACKUP_RETENTION_DELETE_ENABLED` |

**Measured results are a different thing.** A drill records its measured `duration_seconds_measured` beside `rto_target_hours_configured`. The target is met only if a measurement says so.

The local drill measured a few seconds on synthetic data. That says nothing about production size or the provider.

**Timezone:** everything is UTC — the schedule, the retention buckets (calendar days, ISO weeks, calendar months), and every timestamp in every record.

**Missed runs:** `Persistent=true` runs **one** catch-up cycle at the next boot, not one per missed day.

**Overlap:**
- systemd never starts a second cycle while one is running; an elapsed timer leaves the running cycle alone.
- A cross-job overlap (for example an import holding the advisory lock) makes the capture report `skipped_overlap` (exit 3). The cycle then retries within its bounds.
- A cycle still overlapped after its attempts exits **3**, which the unit counts as success. The status check alerts after **2 consecutive** skipped cycles.

## 2. What a cycle does, and its outcomes (kept separate)

1. **Capture:** the accepted job, unchanged. Its report and exit code are copied verbatim into the cycle record.
2. **Off-site copy:** only after `published` (exit 0) and only when enabled.
   1. The accepted verifier must pass, with format `/2`.
   2. The four files are put into a deterministic tar, then encrypted with `age` to the **public** recipients.
   3. The ciphertext is uploaded, **read back in full**, and its SHA-256 compared. An ETag is never taken as a digest.
   4. Only then is the completion record `offsite.json` written, then read back and compared byte for byte.
3. **Records:**
   - one cycle record (stdout plus `<state>/cycles.jsonl`), with separate `capture` and `offsite` sections;
   - one off-site state record (`<state>/offsite/<artifact_id>.json`).
   - An upload failure never touches the capture writer's records (`FAILED`, partials, the artifact).

**Cycle exit status:**

| Exit | Meaning |
|---|---|
| 0 | captured, and stored-verified (or off-site disabled) |
| 3 | all attempts overlapped (not a failure) |
| 1, 4, 5, 64, 130 | the capture's own status; nothing uploaded |
| 20 | off-site failed |
| 21 | uncertain (payload verified, record unconfirmed: **not** stored) |
| 22 | refused |
| 23 | off-site configuration refused |
| 65 | operations configuration refused |

A capture exiting 4 or 5 is never uploaded automatically. Review it, then use `backup-offsite <artifact-dir>`, which runs the same verified path.

**Three states; none alone is recovery readiness:**
1. *locally published* (capture exit 0);
2. *remotely stored* (a verified read-back plus a confirmed completion record);
3. *restore demonstrated* (a passing drill on a downloaded copy).

## 3. Key custody (separate from the backup host)

- **Generate on a trusted machine, not the backup host:** `age-keygen -o napsedge-backup-identity.txt`. It prints the public key, and `age-keygen -y napsedge-backup-identity.txt` re-derives it.
- **The backup host receives ONLY the public recipients file** (`age1...` lines) at `/etc/napsedge-backup/recipients/recipients.txt`. A recipients file containing a secret key is refused.
- **The private identity is never on the backup host.** Keep two offline copies, for example a password-manager entry and a printed or offline copy. The drill must use one of them, which proves it is recoverable.
- **Losing the identity makes every backup unreadable.** There is no recovery path for that.
- **Rotation:** add the new recipient, keeping the old one until every artifact encrypted to the old key has expired under retention. Keep the old identity until then.

## 4. Store credentials and permissions (Backblaze B2 application keys; to create at deployment)

| Use | Capabilities | Where it lives |
|---|---|---|
| Backup host (upload plus read-back) | `listFiles`, `readFiles`, `writeFiles`; **no** `deleteFiles`; restricted to the bucket and prefix. `writeFiles` still allows **overwriting** and **delete-by-name (a delete marker that hides the current version)**; only permanent version deletion is withheld. This is not immutability (see `docs/backup_b2_validation.md` section 5) | systemd encrypted credentials `napsedge-backup-offsite-key-id` / `-secret-key` |
| Status check | `listFiles`, `readFiles` | `napsedge-backup-offsite-read-key-id` / `-read-secret-key` |
| Restore drill (recovery host) | `listFiles`, `readFiles`; `writeFiles` only under `<prefix>/drills/` to publish drill records | files on the recovery host, never the backup host |
| Retention deletion | `listFiles`, `readFiles`, `deleteFiles`; used **only** when deletion is activated | held by Anthony; loaded only for the deletion run |

**Credentials are file paths only.** They are read under the job's password-file rules, never put in arguments, environment values, logs or records, and errors name the store's error code only.

Optional B2 Object Lock (governance mode) is not configured. Enabling it is a separate, irreversible bucket decision.

**Observed on real B2 (9B-8C validation, 2026-10-09; details in `docs/backup_b2_validation.md`):**
- **Bucket names:** the off-site validator accepts only lowercase S3-style names. B2 accepts capitals, so create buckets in lowercase.
- **Missing objects return 403, not 404,** with a prefix-restricted key (HEAD `Forbidden`; GET `AccessDenied: Cannot access bucket`).
  - The adapter (image `napsedge-backup:9b8c-20261009022330` onwards) treats a 403 as "missing" only when a listing of exactly that name succeeds and omits it.
  - The 9B-8B image `9b8b-20261008215925` cannot store to B2.
  - A hidden object (current delete marker) returns 404.
- **The read-only key:** an upload attempt is refused by B2 closing the connection (`ConnectionClosedError`), and nothing is stored.
- **An interrupted upload leaves a partial payload version** (here 64 B). It is harmless (no completion record names it) but stays stored and billed.
- **New keys and buckets:** in the first minutes after creating them, two acknowledged sentinel uploads did not persist (unexplained). Later uploads all persisted. Every real upload is still accepted only after a full read-back.
- **Docker Desktop on Windows** could not bind-mount a USB stick inserted after it started, even after a restart. Key copy A is therefore streamed on stdin into a container's memory, never mounted.

## 5. Activation (deployment time; in order; each step is manual)

1. Build the backup image with the pinned command, or load it from the handoff package, and record its ID.
2. Create the bucket and the application keys in section 4.
3. Generate the age key pair off-host (section 3). Copy the recipients file to `/etc/napsedge-backup/recipients/`.
4. Store the credentials: `systemd-creds encrypt --name=db-password - /etc/credstore.encrypted/napsedge-backup-db-password`, typed on stdin. Repeat for the other credentials in section 4.
5. Install the non-secret configuration:
   - `deploy/backup/backup.env.example` → `/etc/napsedge-backup/backup.env`. Fill it in, leaving off-site disabled.
   - `deploy/systemd/unit.env.example` → `/etc/napsedge-backup/unit.env`.
6. Install the units: `cp deploy/systemd/napsedge-backup*.service deploy/systemd/napsedge-backup*.timer deploy/systemd/napsedge-backup-notify@.service /etc/systemd/system/ && systemctl daemon-reload`. Then check them with `systemd-analyze verify /etc/systemd/system/napsedge-backup*.service`.
7. **First manual cycle, off-site still disabled:** `systemctl start napsedge-backup.service; journalctl -u napsedge-backup.service`. Expect exit 0 with `offsite: disabled`.
8. **Enable off-site:** set `NAPSEDGE_BACKUP_OFFSITE_ENABLED=true` and run one manual cycle. Expect `stored_verified`.
9. **Run a restore drill from the stored copy** (section 7). Only after it passes:
10. **Enable the schedules:** `systemctl enable --now napsedge-backup.timer napsedge-backup-status.timer`.
11. **Retention:** run the dry run (section 8) and review it. Deletion stays disabled until section 8's activation.

**WSL systemd rehearsal (9B-8C, 2026-10-09; `deploy/rehearsal/wsl-rehearsal.sh`, Ubuntu 26.04 with systemd 259 in WSL, Docker Desktop):**
- The **real** units were installed against one disposable synthetic source, with off-site disabled and no alert sender.
- **Results:**
  - one cycle through systemd: exit 0, published;
  - `backup-status`: ok, with the recovery-ready report **not ready** (criteria 1, 2, 3 and 5 unmet, each stated);
  - heartbeat gate `NO PING (off-site outcome 'disabled')`;
  - a forced failure fired `OnFailure=`, giving one journal ATTENTION line and nothing sent.
- **Bug found and fixed:** `install -o 10001 -g 10001` fails under uutils coreutils (`invalid user: '10001'`; Ubuntu 26.04's default). The units now `chown 10001:10001` after `mkdir`/`install`, which works with both GNU and uutils.
- **Rehearsal-only differences:** a stand-in `docker.service` (Docker Desktop has none), dummy off-site credentials, and systemd-creds sealed with the WSL host key.

## 6. Detecting missed or failed backups

**The status check:** `napsedge-backup-status.timer` runs `backup-status` hourly. Exit **10** means attention, raised when:
- no remotely verified copy exists within the RPO;
- 2 or more consecutive cycles were skipped because of overlap;
- the last cycle exited something other than 0 or 3;
- no passing drill exists within the drill interval.

**The notification hook:** both units' `OnFailure=` start `napsedge-backup-notify@.service`. It is **inactive: it writes one journal line only.** Choosing a delivery channel (email, a Discord webhook, an external dead-man's switch) is an open decision. Replace its `ExecStart` only then, with any webhook secret held in a systemd credential.

**Manual checks:**
- `journalctl -t napsedge-backup-notify`
- `journalctl -u napsedge-backup.service -n 50`
- `systemctl list-timers napsedge-backup*`
- `docker run --rm -v napsedge-backup-state:/s:ro --entrypoint tail napsedge-backup:<tag> -n 3 /s/cycles.jsonl`

## 7. Restore drill (monthly, on any host; no original application host needed)

**Version history (9B-8C):** before anything is downloaded, the drill checks the artifact's version history. It refuses if:
- the completion record was overwritten or hidden;
- the verified payload version (named by the record from 9B-8C on) is no longer current;
- the payload is hidden;
- unexpected objects are present.

Payload versions older than the verified one (retry leftovers) are only reported.

Only after a manual review may the drill configuration set `"accept_version_anomalies": true`. Even then, every digest check still applies, and the drill is **never** counted as a passing drill.

These are anomaly checks, **not proof of authenticity**: an artifact forged under a new name with single versions passes them.

**Inputs:**
- the store (a read key);
- the separately held identity;
- one recovery PostgreSQL 16 endpoint. This can be the source cluster with a new target name, or a fresh cluster.

**Steps:** in the probe-tooling image, with the handoff package's source mounted read-only:
```bash
python -m tests.postgres.restore_drill --config /drill/config.json run
python -m tests.postgres.restore_drill --config /drill/config.json cleanup --confirm cleanup
```
The configuration shape is in `tests/postgres/restore_drill.py`. It holds no secrets; credentials are file paths.

**What the drill does:**
- downloads and verifies the payload against its completion record;
- decrypts, checks the plaintext digest, and extracts strictly;
- runs the accepted verifier;
- restores into a **new**, migrator-owned, guarded target, and refuses an existing or source database;
- compares against the artifact's **saved** baseline: the strict verdict and `monotonic_v1` are reported separately, and R2 blocks;
- publishes `drills/<artifact>/<timestamp>.json`.

**Results:**
- Exit 0 means **passed**, which needs restore exit 0, all strict checks equal, strictly equal sequences and the policy accepting.
- Exit 4 means completed with a difference: **not passed**.
- Exit 3 means refused: corrupt payload, wrong or missing key, unsafe target.
- A passed drill is "restore demonstrated" for that copy. It is **not** recovery readiness: those policies remain open, and a strict difference or a failed R2 always prevents it.

## 8. Retention (dry run first; deletion disabled by default)

**Dry run:**
```bash
docker run ... napsedge-backup:<tag> backup-retention
```
It prints the plan: what is kept and why (`daily`, `weekly`, `monthly`, `newest_complete`, `last_known_recoverable`), what would be deleted, incomplete objects, refusals and `plan_sha256`.

**The plan is refused when:**
- the listing is incomplete;
- any artifact is incomplete or inconsistent (manual review);
- a drill record is unreadable;
- **no artifact has a passing drill;**
- (9B-8C) the **version** listing fails, or any artifact folder has a **version-history anomaly**. That includes a hidden artifact the plain listing no longer shows. Anomalies are listed in the plan under `version_anomalies` for manual review.

**Activation, separate from everything else and manual:**
1. Review a dry-run plan.
2. Set `NAPSEDGE_BACKUP_RETENTION_DELETE_ENABLED=true` for that one run.
3. Supply the deletion key.
4. Run `backup-retention --execute --plan-sha256 <the reviewed digest>`.

**Execution rules:**
- It rebuilds the plan and refuses if the digest differs.
- It deletes **every version** of each artifact's completion record **first**, then every version of its payload. Retry leftovers and delete markers are included, and each version is deleted **by version id** (since 9B-8C).
  - A delete by name would only **hide** the objects on B2, leaving them stored and billed.
  - It needs the deletion key (`listFiles`, `readFiles`, `deleteFiles`).
- It stops at the first error and confirms every deletion with a **version** listing that shows nothing left (`versions_remaining` must be empty).
- **Exercised on real B2 (9B-8C deletion test 2):**
  - the upload key was refused (`AccessDenied`, nothing deleted);
  - the deletion key removed all 3 versions of the planned artifact, with no marker left;
  - kept artifacts and drill records were untouched.
- `backup-retention --execute --plan-sha256 <digest>` works only from image `napsedge-backup:9b8c-20261009035256` onwards. Earlier images reject the options as unrecognized arguments (entrypoint bug fixed in 9B-8C).
- Drill records are kept.
- Incomplete objects are never deleted automatically.

## 9. Connection budget (DigitalOcean 1 GiB plan: 22 connections)

| Consumer | Processes / engines | Connections |
|---|---|---|
| API | 1 uvicorn worker, 1 engine | at most 4 (`NAPSEDGE_DB_POOL_SIZE` 2 + `MAX_OVERFLOW` 2) |
| Streamlit | 1 process, 1 memoized engine | at most 4 |
| Discord | 1 process, 1 memoized engine | at most 4 |
| Backup cycle | at most 1 at a time (systemd) | **measured at most 3** concurrent backends (budgeted 4); a refused overlapping attempt plus the holder measured at most 2 |
| Operator / migration session | 1 | 1 |
| **Total** | | **17 of 22** |

**If a capped import process runs at the same time:** it uses its own engine under the same caps (up to 4), giving 12 + 4 + 4 = 20 of 22. Do not raise the pool caps without redoing this table.

**Provider admin sessions:** console sessions as `doadmin` also count.

**Not yet measured:** headroom on the live provider (the deferred probe's check 7).
