# NapsEdge Recovery Runbook -- Restoring Onto an Empty, Independently Initialized PostgreSQL Cluster

**Status: rehearsed against disposable, synthetic resources only (Phase 9B-4, 2026-10-02). This is a procedure, not an installed capability.** No backup schedule, off-host archive storage, production database, real certificate, real credential or secret store exists in this project. Nothing here has been run against a real system, and the timings in section 10 are **not** a production recovery guarantee.

How to read this document: every command in sections 4 to 7 was executed by `tests/postgres/test_postgres_fresh_cluster_recovery.py`, which creates a source and a destination cluster, loses the source, and recovers onto the destination. Where a step is a *future real-system operation* that the rehearsal did not perform, it is marked **[FUTURE]**. Values in `<angle brackets>` are placeholders; no secret value appears anywhere in this document.

**Two kinds of check appear below and must not be confused.** *Enforced gates* (E1 to E4) are conditions the rehearsal's code checks **while it runs**: if one fails, the next action is never executed (the negative tests prove this for each). *Verification checks* (V1 to V11, section 8) are measurements the rehearsal's tests make **after** a recovery has completed; in the rehearsal they stop nothing. On a real recovery they are the operator's pre-cutover procedure, which nothing enforces automatically.

---

## 1. What this recovers, and what it does not

Recovers: the application database `napsedge` (schema at Alembic head `5b02bea0eb85`, all rows, constraints, indexes, sequence state, object ownership, and table/sequence privileges) from a custom-format `pg_dump` archive into a database on a *different* PostgreSQL cluster.

Does **not** carry, and therefore must be re-created explicitly on the destination (all rehearsed): cluster roles and their passwords, the database itself and its owner, database-level privileges (`CONNECT`, the revocation of PUBLIC's defaults), and the TLS server certificate and trust chain.

Out of scope and unrehearsed: point-in-time recovery, other databases, extensions beyond those the archive lists, connection pooling, application cutover, DNS or network changes, and the Discord and Streamlit applications (they only consume the database).

## 2. Required artifacts (gather before touching the destination)

| Artifact | Rehearsal | Real system |
|---|---|---|
| Archive (`pg_dump -Fc`) taken by `napsedge_backup_ro` | copied off the source container to a protected temporary directory, then into the destination | **[FUTURE]** an off-host, encrypted location (none exists) |
| Archive checksum (SHA-256) recorded at dump time | recorded; source-to-host equality is **enforced (E1)** before the source is removed; destination equality is **enforced (E4)** before restore; re-observed after the restore | **[FUTURE]** stored separately from the archive |
| **Pre-loss comparison baseline** (per-table row counts and md5 content hashes, constraint and index definitions with validation state, sequence state, object owners and table/sequence ACLs, Alembic version) | captured by the test from the live source immediately before the dump and **kept only in memory**; it ceases to exist when the test process ends | **[FUTURE -- not implemented]** durable, backup-consistent baseline capture and storage. Capturing a baseline from a live source that is accepting writes is **not validated**: the rehearsal source was idle, and the baseline is read in separate transactions from the dump, so concurrent writes could make them disagree. Do not rely on a baseline you have not proven consistent with the archive |
| Bootstrap definitions (section 6): three roles, one database | built in memory by the test; the SQL in section 6 is the same statements | **[FUTURE]** definitions kept in version control with *no* passwords |
| New passwords for `napsedge_migrator`, `napsedge_runtime_ro`, `napsedge_backup_ro` and the destination superuser | generated per run | **[FUTURE]** drawn from the secret store; never reused from the source |
| Destination server certificate chain and the CA file clients trust | synthetic CA, generated per run | **[FUTURE]** issued for the real host name; SAN must match the name clients use |
| PostgreSQL client tools of the same major version as the archive (16) | `pg_dump`, `pg_restore`, `psql` inside the container | **[FUTURE]** installed on the recovery host |
| The application's Alembic head (`5b02bea0eb85`) from the repository at the matching commit | checked | **[FUTURE]** |

The source is **not** an input to recovery: the rehearsal removes the source before recovery (enforced, E2) and nothing on the destination is derived from it. Do not extract real password hashes, and do not take a globals dump (`pg_dumpall --globals-only`) -- it contains role password hashes.

## 3. Credential handling

- Passwords never appear in a command line, an environment value, a DSN that is logged, or shell history. Each client command reads a **pgpass file created with owner-only permissions** whose path (not content) is exported as `PGPASSFILE`, and uses `--no-password` (`-w` for `psql`) so a missing password fails instead of prompting.
- Create the pgpass file with the content on **stdin** (rehearsed exactly; one line per role, `*:5432:*:<role>:<password>`):

```bash
# the file's content is piped in; only the path ever appears as an argument
sh -c 'umask 077; cat > /tmp/napsedge_pgpass_<random>'
```

- Verify the mode is `600` before use (`stat -c %a <file>`), and `rm -f` the file when finished. (Phase 9B-2's tests assert both; in the 9B-4 rehearsal the files are created the same way and disappear with the destroyed containers.)
- **Role passwords are supplied to the server inside SQL that is built in memory and piped to `psql` on stdin** -- exactly what the rehearsal does (the test builds the statement text in its own process and feeds it to `psql -f -`). The SQL text is never written to a file and never typed into a shell: a heredoc or `echo` containing a password would land in shell history, and `psql -c` or `-v password=...` would put it in the process list. Use a small generator under your control that reads the passwords from the secret store and writes the SQL to the pipe. With `password_encryption = scram-sha-256` the stored verifiers are SCRAM; the rehearsal asserts every role holds a `SCRAM-SHA-256$` verifier. **[FUTURE]** confirm that statement logging (`log_statement`) is off on the real server, or the bootstrap SQL will be logged.

## 4. Verify the destination is empty (enforced gate E3)

Connect as the destination superuser over TCP with `verify-full`; there is no Unix-socket or unencrypted path to fall back to. The connection string used throughout (rehearsed; `hostaddr` pins the address while `host` is verified against the certificate):

```
host=<host-in-certificate> hostaddr=<ip> port=5432 dbname=<bootstrap-db> user=<superuser> sslmode=verify-full connect_timeout=15 sslrootcert=<destination-ca-file>
```

This query contains no secret, so a heredoc is fine here (rehearsed exactly):

```bash
PGPASSFILE=<pgpass> psql -w -X -v ON_ERROR_STOP=1 -d "<connection string>" -tA -F "|" -f - <<'SQL'
SELECT 'napsedge_roles', count(*) FROM pg_roles WHERE rolname LIKE 'napsedge\_%'
UNION ALL
SELECT 'application_databases', count(*) FROM pg_database WHERE NOT datistemplate AND datname NOT IN ('postgres', current_database())
UNION ALL
SELECT 'bootstrap_database_tables', count(*) FROM pg_tables WHERE schemaname NOT IN ('pg_catalog', 'information_schema');
SQL
```

**Enforced:** the rehearsal requires all three counts to be exactly `0` -- and a missing count does not count as zero -- before it executes any bootstrap SQL or restore. The negative test runs this against a real cluster that already holds the three roles and shows the bootstrap, the archive transfer into the restore path and the restore were never executed, the roles were untouched, and the target database was never created. The same query run against the populated source reported three roles and an application database. If it is not empty, stop: do not restore over an existing system.

## 5. Bring the archive in and verify it (enforced gate E4)

The rehearsal copies the archive into the destination and verifies it **before bootstrap and before restore**:

```bash
sha256sum <archive>                       # must equal the digest recorded at dump time
pg_restore --list <archive>               # must exit 0 and list the table-data entry for public.teams (no connection is made)
```

**Enforced:** all three conditions -- digest equality, a successful listing, and the expected `TABLE DATA public teams` entry -- must hold, or neither bootstrap nor restore runs. Negative tests cover a one-byte corruption (digest mismatch), a truncated archive whose own digest is recorded as expected (so only the listing can object), and a listing that succeeds but lacks the entry; in each, the destination was shown still empty afterwards.

**Take the archive (on the source, as the backup role)** -- rehearsed; the source-to-host digest equality is **enforced (E1)** before the source may be removed (a negative test shows a mismatch leaves the source running and records no host archive):

```bash
PGPASSFILE=<pgpass> pg_dump -d "host=<source-host> hostaddr=<ip> port=5432 dbname=<database> user=napsedge_backup_ro sslmode=verify-full connect_timeout=15 sslrootcert=<source-ca-file>" -Fc --no-password -f <archive>
sha256sum <archive>          # record the digest
```

(`pg_dump` creates its output file before it connects, so a refused dump can leave an empty file: judge success by the exit status, never by the file's existence.)

**Confirm the source is gone (enforced gate E2)** before any destination step: the rehearsal removes the source container it owns, then requires both that the container no longer exists **and** that a connection to its address fails. If either condition is false, destination recovery is refused (negative tests cover a removal that does nothing and a "removed" source that still answers). **[FUTURE]** on a real system "the source is lost" is a decision and a fact about the world, not a check this procedure can make for you; do not recover onto a destination while the original may still be accepting writes.

## 6. Bootstrap the destination explicitly

Run as the destination superuser with this destination's **new** passwords. The statements are built in memory and piped to `psql` on stdin (section 3):

```sql
CREATE ROLE napsedge_migrator   LOGIN PASSWORD '<new>' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE napsedge_runtime_ro LOGIN PASSWORD '<new>' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE napsedge_backup_ro  LOGIN PASSWORD '<new>' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE DATABASE "<database>" OWNER napsedge_migrator;
```

```bash
<generator that writes the SQL above to stdout> | PGPASSFILE=<pgpass> psql -w -X -v ON_ERROR_STOP=1 -d "<connection string>" -f -
```

(The rehearsal's generator is the test process itself; no generator script is provided or installed by this project.) The roles must exist **before** the restore: the archive's ownership and ACL statements name them, and a restore into a cluster without them fails atomically (`role "napsedge_backup_ro" does not exist`, proved in Phase 9B-2).

## 7. Restore, then re-apply what the archive does not carry

**Restore (as the non-superuser migrator, atomic, stop at first error)** -- rehearsed:

```bash
PGPASSFILE=<pgpass> pg_restore -d "host=<host-in-certificate> hostaddr=<ip> port=5432 dbname=<database> user=napsedge_migrator sslmode=verify-full connect_timeout=15 sslrootcert=<destination-ca-file>" --no-password --exit-on-error --single-transaction <archive>
```

Expect exit status 0 and empty stderr. Then **re-apply what the archive does not carry**, run by the migrator (the database owner; no superuser needed). The rehearsal observed that, before this step, the restored database still showed PUBLIC's default `TEMPORARY` and `CONNECT`. These statements were executed through the test helper's driver connection (identical SQL), not through `psql`:

```sql
REVOKE ALL ON DATABASE "<database>" FROM PUBLIC;
GRANT  CONNECT ON DATABASE "<database>" TO napsedge_runtime_ro;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT  USAGE ON SCHEMA public TO napsedge_runtime_ro;
GRANT  CONNECT ON DATABASE "<database>" TO napsedge_backup_ro;
GRANT  USAGE   ON SCHEMA public TO napsedge_backup_ro;
GRANT  SELECT  ON ALL TABLES    IN SCHEMA public TO napsedge_backup_ro;
GRANT  SELECT  ON ALL SEQUENCES IN SCHEMA public TO napsedge_backup_ro;
```

(Table and sequence privileges for both roles were already carried by the archive and re-checked below; the last two statements are idempotent.)

## 8. Verification checks before application cutover (observations, not enforced gates)

These were measured on the recovered destination by the rehearsal's tests, and all held. They run after recovery and block nothing in the rehearsal; on a real recovery, performing them and refusing to cut over on any failure is the operator's responsibility.

| Check | What is measured | Rehearsal observation |
|---|---|---|
| V1 Data | per-table row count and an md5 over every row's text equal the pre-loss baseline (section 2) | no differences |
| V2 Schema | every constraint and index present with the same definition and validation state; the only textual difference allowed is PostgreSQL's known re-rendering of `varchar IN (...)` CHECKs, accepted only by an exact rewrite plus byte equality | no genuine difference |
| V3 Sequences | sequence state (`last_value`) equal to the baseline | equal. **Not checked in this rehearsal:** that the next generated id exceeds every existing id (Phase 9B-1 checked that within one cluster; this rehearsal does not repeat it) |
| V4 Ownership and ACLs | object owners and table/sequence ACLs (including both application roles) equal the baseline | equal |
| V5 Migrations | `alembic_version` is `5b02bea0eb85`; `alembic check` reports no new upgrade operations | observed |
| V6 Application read path | `check_readiness` is ready and the accepted read paths succeed under the **runtime** credentials, reading exactly the five granted tables | observed |
| V7 Runtime boundary | 27 write, DDL and escalation statements are each refused with SQLSTATE `42501`; no role memberships; owns nothing | observed |
| V8 Backup boundary | the backup role can read everything it must, holds no write or DDL authority, and 31 write/escalation statements are refused | observed |
| V9 Credentials | the **old** source passwords for all three roles (and the old superuser) are refused by the destination (`password authentication failed`), while the destination's own passwords succeed | observed |
| V10 Trust | the destination certificate verifies against the destination CA only; the old CA is refused (`certificate verify failed`); the connection is TLS as reported by the server (`pg_stat_ssl`) | observed |
| V11 Archive | the archive's SHA-256 is unchanged after the restore | unchanged |

`alembic check` and the application's `NAPSEDGE_DATABASE_URL` accept a percent-encoded URL (fixed and verified in Phase 9B-3). The rehearsal ran `alembic check` through the configuration route; the environment-variable route over TLS was verified in 9B-3's tests.

## 9. Failure handling

- **The restore is atomic, and a failed restore rolls back to the pre-restore state.** `--single-transaction --exit-on-error` makes any error undo the whole restore, leaving the target database exactly as it was before `pg_restore` started. In this procedure that state is the freshly bootstrapped, empty database, so in the rehearsed failure cases (a populated target, a truncated archive, missing roles -- Phase 9B-2) the target held no tables afterwards. That is a consequence of the target having been empty, not a claim that a restore "empties" anything: **never run this restore into a database that holds data you want to keep.**
- **Never delete or alter the archive.** Keep it, and its recorded digest, until recovery is verified and signed off.
- If a verification check in section 8 fails, do **not** cut over. Investigate with the archive intact; a destination that failed a check should be discarded and rebuilt from the empty state, not repaired in place. *(Discarding and rebuilding is guidance; the rehearsal exercised only the success path, the enforced-gate refusals above, and the atomicity checks.)*
- A failed connection to the destination is diagnosed by its message: `password authentication failed` (credential), `certificate verify failed` (trust anchor), `does not match host name` (name versus certificate SAN), `no encryption` (the client did not use TLS), `rejects connection for host "[local]"` (a Unix socket was used). These were each proved for their own reason in Phase 9B-2.
- If an enforced gate refuses to proceed (a populated destination, a digest mismatch, an unreadable archive, an unconfirmed source loss), nothing downstream ran. Fix the cause; do not bypass the check.
- Never copy secrets into tickets or logs; report digests, exit statuses and counts instead.

## 10. Measured synthetic rehearsal duration

These are **measured observations**, not enforced limits: nothing in the rehearsal fails because a step was slow. Measured on a developer workstation with Docker, `postgres:16-alpine`, a **tiny synthetic data set** (3 teams, 2 players, 3 stat snapshots) and local container networking. **These numbers describe the rehearsal only and say nothing about the time a real recovery will take**, which depends on data volume, network and storage.

| Phase | What it covers | Measured (seconds, six executions) |
|---|---|---|
| Two-cluster start-up | both containers, TLS material, readiness over `verify-full` | 6.5 to 8.4 |
| Source build and seed | role/database bootstrap, migrations to head, grants, synthetic records | 3.2 to 4.2 |
| Dump and copy off the source | `pg_dump` as the backup role, checksum, copy out | 0.6 |
| **Destination recovery** | empty measurement, archive transfer, checksum, `pg_restore --list`, bootstrap, restore, post-restore grants, comparison with the baseline | **2.4 to 3.1** |

Six executions: the first five predate the correction that moved archive verification ahead of bootstrap, the sixth runs the current code (6.5 / 3.8 / 0.6 / 2.4). The recovery module as a whole, including the negative tests and every section 8 check, now takes about 52 seconds wall clock because it also starts three extra disposable clusters for the negative paths.

The section 8 checks are separate tests and are not included in the recovery figure.

## 11. Disposable versus future operations

| | Disposable rehearsal (done) | Future real system (not done, needs Anthony's decision) |
|---|---|---|
| Clusters | two `postgres:16-alpine` containers created and removed by the test | a real destination cluster and its provider |
| Trust material | synthetic CA and server certificate, deleted afterwards | certificates for the real host name from a real authority |
| Credentials | generated per run, never printed | issued and rotated through a secret store |
| Archive transport | copied through the Docker API to a temporary directory | an encrypted, off-host, access-controlled store |
| Comparison baseline | captured in memory from an idle source | durable, backup-consistent capture and storage (not implemented) |
| Schedule and retention | none (the 22.5 proposal in the readiness document is **not installed**) | an installed, monitored backup job with a tested retention policy |
| Cutover | none | pointing the application at the destination only after every section 8 check passes |

Overall Phase 9B remains incomplete: production provisioning, secret injection and rotation, installed and scheduled backups with off-host storage and monitoring, the operator import entry point, connection pooling and timeouts, and Streamlit access control (Phase 9C) are all still open.

## 12. Reproducing the rehearsal

```bash
python -m pytest tests/postgres/test_postgres_fresh_cluster_recovery.py -q -m postgres -rP
```

Requires Docker and the host `openssl`; if either is missing the tests are skipped (a skip is not verification). The test removes only the containers, temporary certificate directories and archive directory it creates, **verifies they are gone on success and on failure**, and never uses the shared test container or any named Docker volume.
