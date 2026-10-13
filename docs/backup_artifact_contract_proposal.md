# Specification Proposal -- The NapsEdge Backup Artifact Contract (revision 4: Phase 9B-5B local write side implemented)

**Status (revision 4, 2026-10-02): the READ SIDE (Phase 9B-5A, `src/deployment/backup_artifact.py`) and the LOCAL, FILE-ONLY WRITE SIDE (Phase 9B-5B, `src/deployment/backup_artifact_writer.py`) of the file contract are implemented; everything else here (capture, consistency, the sequence rule, scheduling, storage, retention) remains a specification or proposal. Nothing is installed, scheduled or approved for operation.** No backup job, scheduler, storage integration, database job, account or infrastructure exists or is created by this document. It fixes *what a backup artifact is and what each claim about it means* before any code is written. Overall Phase 9B remains incomplete.

How statements are labelled. **[project fact]** was checked in this repository. **[PostgreSQL behavior]** is documented product behavior that has **not** been proven here; the validation plan in section 7 exists to establish it before any operational use. **[design]** is a choice this specification makes. Nothing marked [PostgreSQL behavior] may be relied on until section 7 has been executed and reviewed.

**Revision 3 (Phase 9B-5A) changes this document in four ways, and records that the read side of the file contract now exists.** (1) The JSON value rule is corrected: objects, arrays, strings, exact integers, booleans and documented nulls are permitted; floats and non-finite values are rejected (the first revision wrongly said "integers and strings only", which contradicted the booleans, arrays and nulls the schemas themselves use). (2) Finalization now states that `RUNNING` is removed **before** `COMPLETE` is written and before publication. (3) The text no longer implies that two endpoint samples of foreign sessions can establish continuous quiescence. (4) The catalog bracket and the concurrent-sequence policy are labelled **proposed and unvalidated**, and passing the selected validation cases is stated not to prove safety against all unmanaged changes. Section 12 records exactly what was implemented, tested and verified; **only the read side** (typed documents, strict JSON, canonical serialization, marker parsing, `verify_artifact_directory`) exists. The writer, finalizer, `FAILED` writer, capture, and the sequence rule do not.

**Revision 4 (Phase 9B-5B) records the local write side and changes the specification in three ways.** (1) The `FAILED` reason classes gain two the writer needs: `destination_exists` (a collision discovered at publication) and `durability_failed` (a required flush failed or is unsupported). (2) Finalization is made exact where the first text was silent: the staging directory is created **exclusively and privately**, the staged contents are validated by the verifier's own content check (called with the **final** name, while the public verifier keeps refusing `.partial-*`) **before** publication, and publication must **refuse** to replace an existing destination, including an empty directory. (3) Durability is split into three separately reported things -- file flushes, directory flushes and atomic visibility -- and the writer never reports durable success for a flush it could not obtain. Section 12.1 records what was implemented, tested and verified, and what is unsupported or deferred. Section 12.2 records the descriptor-closure correction made after the first 9B-5B delivery. Section 12.3 records the first execution of part of the section 7 plan (cases C1, C2 and C8, table data only). Section 12.4 records the sequence cases C3, C4 and C5. Section 12.5 records the catalog and lock cases C6 and C7, including the failed catalog bracket. Section 12.6 records the overlap and quiescence cases C9 and C10. Section 12.7 records the later correction that makes `bulk_import_advisory_lock` preserve a primary failure itself. Section 12.8 records the validation of the fresh catalog-observer candidate. Section 13 is the **proposed** v1 capture and restore operating contract. It is documentation only: not implemented, not verified, and not adopted.

Revision 2 supersedes the first proposal in four ways: it separates four claims that "COMPLETE" must not blur (section 2); it gives exact schemas and a validity predicate (sections 3 to 5); it makes the consistency design precise, including sequences, and states where it is *not* sufficient (section 6); and it narrows the recommended code unit so that file-schema code does not drag database capture helpers into production (section 9).

Inputs: readiness section 22.5 and sections 23 to 27, `docs/recovery_runbook_fresh_cluster.md`, `tests/postgres/pg_restore_compare.py`, and the `src/deployment/` package layout. **Correction to section 22.5:** its "member of `pg_read_all_data`" wording is superseded -- since 9B-2 the backup role (`napsedge_backup_ro`) has explicit grants (`CONNECT`, schema `USAGE`, `SELECT` on tables and sequences), no memberships, fail-closed for new objects. Its nightly schedule, 7/4/6 retention and monitoring rows remain **uninstalled proposals**.

---

## 1. Decisions already established (not reopened)

| Established | Source |
|---|---|
| Dedicated non-superuser backup role with explicit grants, no `pg_read_all_data`, fail-closed for new objects | readiness 23.3 (tested) |
| `sslmode=verify-full`, explicit CA and host, SCRAM; no trust or Unix-socket path | 23.1-23.2 (tested) |
| 0600 pgpass file whose path alone is exported; `--no-password`; no secret in argv, environment values, logs or artifacts | 23.4, runbook 3 |
| `pg_dump -Fc`; SHA-256; atomic restore as the non-superuser migrator; archives name roles but do not create them; no globals dump | 22-25 |
| The comparison baseline's content is `DatabaseBaseline`: counts and md5 content hashes, constraints, indexes, owners and ACLs, sequence state, Alembic version | `tests/postgres/pg_restore_compare.py` |
| The strict recovery comparator (including its one exact CHECK-rendering rewrite) is **not changed** by this work | 25.3, this document |
| Restore verification procedure: enforced gates E1-E4, checks V1-V11 | runbook 5-8 |
| Archives, and anything derived from row contents, are sensitive; storage must be off-host and encrypted | 22.5 (requirement; values remain proposals) |
| No scheduler, storage or infrastructure exists; the runtime role is read-only and there is no application writer role | 22-27 |

## 2. Four claims that must never be conflated

An artifact can be in none, some or all of these states. Each has its own meaning, its own evidence, its own record, and its own words in any tool output.

| # | Claim | Means | Established by | Recorded in | Does **not** imply |
|---|---|---|---|---|---|
| 1 | **Files complete** | the writer reached the end of finalization: all files written, the manifest written, the completion marker written, the directory renamed to its final name | the writer, at finalization | the `COMPLETE` marker and the final directory name | that any byte is still intact, that the files agree with each other, that the data is consistent, or that it restores |
| 2 | **Structurally and checksum verified** | at verification time the directory satisfies the validity predicate of section 5: exact file set, safe paths, well-formed strict JSON, supported versions, sizes and SHA-256 digests match, marker binds the manifest | a verifier, any time, repeatable | a verification result (not stored in the artifact); a verifier may append a separate record | that the archive and baseline describe the same database state, or that `pg_restore` accepts the archive |
| 3 | **Database consistency established** | at capture time the archive and the baseline were read under one shared snapshot (or under proven quiescence) and the catalog-stability and sequence-rule inputs were recorded | the capture procedure of section 6, evidenced by fields in `manifest.json` `consistency` | the manifest (a *claim by the capture procedure*, supported by recorded evidence, not provable from the files alone) | that a restore will succeed, or that the claim is true if the procedure was altered |
| 4 | **Successful restore verified** | a restore of **this** artifact into a disposable, empty destination passed the runbook's enforced gates and all checks, comparing against **this** artifact's baseline | a restore-verification run | a separate, immutable `verification.json` outside the artifact directory, naming the artifact's manifest digest, the runbook revision, the date and every outcome | that the artifact is still intact later (re-run claim 2) |

**`COMPLETE` means claim 1 and nothing else.** A tool that lists, retains, uploads or restores artifacts must use the claim it actually needs: retention and listing need claim 2; trust in comparison needs claim 3; a statement such as "this backup is good" is allowed **only** for claim 4, and must name its date and runbook revision. Claim 2 verification is required immediately before any restore.

## 3. Artifact layout and finalization

A final artifact is exactly one directory, named

`napsedge-<logical_name>-<YYYYMMDDTHHMMSSZ>-<artifact_id>`

(`logical_name` matches `[a-z0-9_]{1,40}`; the timestamp is the manifest's `created_utc`; `artifact_id` is a lower-case UUID4), containing **exactly four files**: `archive.dump`, `baseline.json`, `manifest.json`, `COMPLETE`. Any other file, directory, symlink or reparse point makes it invalid.

**Finalization order [design].** The writer builds in `.partial-<artifact_id>/`, which no consumer ever lists: (1) write `archive.dump` and `baseline.json`, flushing each to stable storage; (2) write `manifest.json`, flush; (3) **remove `RUNNING` and flush the directory**; (4) write `COMPLETE`, flush; (5) flush the directory; (6) rename `.partial-<artifact_id>` to the final name in one step on the same filesystem; (7) flush the parent directory. `RUNNING` exists from the start of the run until step 3 and records the start time and process, so no unfinished-work marker can survive into a published artifact. A `FAILED` file (below) may replace it, and a failed run never reaches step 3. **Publication (step 6) requires that neither `RUNNING` nor `FAILED` exists in the directory**; a verifier treats either beside `COMPLETE` as `CONFLICTING_MARKERS`, and either without `COMPLETE` as `ARTIFACT_RUNNING` or `ARTIFACT_FAILED`. *(Implemented in Phase 9B-5B by `write_artifact()`; see 12.1 for the exact order, the flush semantics, the no-replace publication rule and the platform guarantees. The first step is, precisely, the **exclusive creation of a private** `.partial-<artifact_id>` -- an existing partial or final directory is never reused, adopted, overwritten or deleted -- and the staged directory is validated with the verifier's own content check before step 6.)*

**`COMPLETE` content** is exactly four LF-terminated lines, binding the marker to the manifest:

```
napsedge.backup-complete/1
artifact_id=<uuid>
manifest_sha256=<64 lower-case hex>
manifest_bytes=<decimal>
```

**`FAILED` file** (never inside a valid artifact): strict JSON `{"format":"napsedge.backup-failed/1","artifact_id":"<uuid>","utc":"<timestamp>","reason_class":"<class>"}` where the class is one of `dump_failed`, `snapshot_unavailable`, `baseline_failed`, `checksum_mismatch`, `source_identity_mismatch`, `schema_head_mismatch`, `catalog_changed`, `timeout`, `storage_full`, `interrupted`, `internal_error`, and (added in 9B-5B) `destination_exists`, `durability_failed`. No stack trace, host, DSN or secret; the writer emits only these four fixed keys, with an enumerated class, so no free text can enter the record. A skipped run (another job held the lock) writes no artifact and is logged as `SKIPPED:overlap`.

## 4. Schemas (exact)

**Encoding rules for all JSON files [design].** UTF-8 without BOM; written canonically (keys sorted, separators `,` and `:`, no insignificant whitespace, one trailing LF); **permitted values are objects, arrays, strings, exact integers (within the signed 64-bit range; a boolean is never accepted where an integer is required), booleans, and `null` only where this specification documents it** -- floats (including `1.0` and `1e3`) and non-finite values are rejected; strings must be valid Unicode text (no lone surrogates); nesting is bounded; the verifier also requires that a file's bytes **equal the canonical serialization of what they parse to** (`NOT_CANONICAL` otherwise), so each document has exactly one byte representation. **The only documented nulls** are `baseline.json` `version`, `max_alias_id`, `sequences[].last_value`, and `sequence_details[].last_value`, `.column_max` and `.owned_by` (and, in manifest `/2` only, both `consistency.foreign_write_sessions_*`, meaning "not sampled": section 13.18); a null anywhere else is a type error; **duplicate keys are an error**; **unknown keys are an error** (fail closed; additions need a new format version); timestamps are exactly `YYYY-MM-DDTHH:MM:SS.ffffffZ` (UTC, six fractional digits, literal `Z`, no offsets); digests are 64 lower-case hex characters; sizes and counts are non-negative integers. Maximum sizes: `manifest.json` 1 MiB, `baseline.json` 64 MiB (a verifier rejects larger files before parsing). Values are stored exactly as the capture produced them: **no normalization of any kind happens in the file layer**. List order is the capture's own `ORDER BY` order.

### 4.1 `manifest.json` -- format `napsedge.backup-artifact/1`

All fields required.

| Field | Type | Meaning |
|---|---|---|
| `format` | string | exactly `napsedge.backup-artifact/1` |
| `artifact_id` | string | lower-case UUID4; equals the directory suffix and the marker |
| `created_utc`, `finished_utc` | timestamp | capture start and end; `finished_utc >= created_utc`; `created_utc` equals the directory name's timestamp (to the second) |
| `source.logical_name` | string | equals the directory's `logical_name`; a logical name, **never** a host, address or DSN |
| `source.system_identifier` | string | decimal digits, `pg_control_system().system_identifier` |
| `source.database_oid`, `source.database_name` | int, string | the database dumped |
| `source.server_version_num` | int | `server_version_num` of the source |
| `source.pg_dump_version` | string | the `pg_dump --version` output |
| `source.backup_role` | string | the role that connected (`napsedge_backup_ro`) |
| `schema.alembic_head` | string | `alembic_version` read **in the capture's snapshot** |
| `schema.expected_head` | string | the repository's expected head at capture |
| `schema.heads_match` | bool | `alembic_head == expected_head` |
| `repository.git_revision`, `repository.dirty` | 40-hex string, bool | provenance of the code that produced the artifact |
| `tooling.name`, `tooling.version` | string, string | the producing job |
| `consistency.mode` | string | `exported_snapshot` or `quiescent` |
| `consistency.snapshot_descriptor` | string | `pg_current_snapshot()` text read in the snapshot-owning transaction (`exported_snapshot`), or `""` (`quiescent`) |
| `consistency.snapshot_unchanged_at_end` | bool | the owner re-read `pg_current_snapshot()` after the dump exited and it was identical (`exported_snapshot`); `false` for `quiescent` |
| `consistency.sequence_rule` | string | `monotonic_v1` or `equal_v1` (section 6.5) |
| `consistency.ddl_exclusion` | string | `none` or `locks` (section 6.4) |
| `consistency.catalog_bracket_equal` | bool | the before/after catalog digests were equal (section 6.4); **under the bracket as originally written this is NOT evidence of catalog stability (12.5)** |
| `consistency.foreign_write_sessions_start`, `..._end` | int | sessions other than the backup role holding a write-capable role, sampled **once at capture start and once at the end**: evidence only, not a gate, and **two endpoint samples cannot establish that nothing wrote in between** (section 6.6; observed missing a short-lived writer, 12.6) |
| `archive.format` | string | `custom` |
| `archive.toc_entries`, `archive.toc_sha256` | int, digest | count and digest of `pg_restore --list` output for the archive |
| `baseline.table_count`, `baseline.row_total` | int, int | summary of the baseline |
| `files` | list | exactly two entries, ordered by `path`: `{"path":"archive.dump","bytes":n,"sha256":d}` and `{"path":"baseline.json","bytes":n,"sha256":d}` -- the file inventory; `manifest.json` and `COMPLETE` are bound by the marker, not listed |

### 4.2 `baseline.json` -- format `napsedge.backup-baseline/1`

The baseline is the **existing `DatabaseBaseline`, field for field, under the same names**, plus one block for the sequence rule. No second representation of the comparison data is introduced.

| Field | Type | Equals |
|---|---|---|
| `format`, `artifact_id`, `captured_utc` | string, uuid, timestamp | header; `artifact_id` equals the manifest's; `captured_utc` lies within the manifest's `created_utc`..`finished_utc` |
| `fingerprints` | list of `[table, rows, md5]`, **sorted by table name** | `DatabaseBaseline.fingerprints`, which is a **dictionary** `{table: (rows, md5)}`. The wire form is an explicit, reversible mapping (dictionary <-> sorted list; the `(rows, md5)` tuple is restored on the way back), not an assumed tuple-to-array conversion. `md5` is 32 lower-case hex digits for a table with rows, or the literal `empty` for a table with none (and only then), as real captures produce |
| `constraints` | list of `[table, name, contype, validated, definition]` | `DatabaseBaseline.constraints` |
| `indexes` | list of `[table, indexname, indexdef]` | `DatabaseBaseline.indexes` |
| `acls` | list of `[relname, relkind, owner, acl_text]` | `DatabaseBaseline.acls` (`acl_text` is `""` when null) |
| `sequences` | list of `[name, last_value or null]` | `DatabaseBaseline.sequences` -- the `pg_sequences.last_value` input of the **existing strict comparator** |
| `version` | string or null | `DatabaseBaseline.version` (Alembic version); null is preserved as null |
| `max_alias_id` | int or null | `DatabaseBaseline.max_alias_id`, carried 1:1 for the existing comparator (a test-oriented field; see decision 5) |
| `sequence_details` | list of objects | **new**: one object per sequence, section 6.5: `name`, `owned_by` (`[table, column]` or null), `data_type`, `increment`, `minimum`, `maximum`, `cache`, `cycle`, `last_value` (int or null), `is_called` (bool), `column_max` (int or null) |

The mapping `DatabaseBaseline` <-> document is one-to-one so the existing comparator (`compare_to_baseline`) works unchanged on a deserialized baseline. The md5 fingerprints depend on PostgreSQL's text rendering of rows: **compare against a destination of the same major version** [design; unverified across versions].

## 5. The validity predicate

`verify_artifact_directory(path, expected=None)` returns either **INVALID** with *every* reason found, or **STRUCTURALLY_VALID** (claim 2 only). It never repairs, never adopts a partial directory, never follows symlinks, and fails closed. Checks, in this order (a failed structural precondition stops later checks that depend on it, but the reasons already found are all reported):

| Step | Check | Reason code |
|---|---|---|
| 1 | the path is inspected **component by component with no normalization before validation**; the artifact directory **and every ancestor of it** are real directories, not symlinks or reparse points; the name matches the pattern; not named `.partial-*` | `NOT_A_DIRECTORY`, `SYMLINK`, `NAME_INVALID`, `NOT_FINAL` |
| 2 | exactly the four expected names present, nothing else; no symlinks; names compared case-sensitively and with no path separators, `..`, drive letters, NUL or backslash | `MISSING_FILE:<n>`, `UNLISTED_FILE:<n>`, `UNSAFE_PATH`, `SYMLINK` |
| 3 | no `FAILED` or `RUNNING` file alongside a `COMPLETE` | `CONFLICTING_MARKERS` |
| 4 | `COMPLETE` has exactly the four lines, supported marker version, a UUID and a digest | `MARKER_MALFORMED`, `MARKER_UNSUPPORTED_VERSION` |
| 5 | `manifest.json` size within bound and equals `manifest_bytes`; SHA-256 equals `manifest_sha256` | `FILE_TOO_LARGE`, `MARKER_SIZE_MISMATCH`, `MARKER_DIGEST_MISMATCH` |
| 6 | manifest parses as strict JSON: valid syntax, no duplicate keys, no unknown keys, no floats or non-finite numbers, valid UTF-8 without a BOM, integers within signed 64 bits, bounded depth, valid Unicode text, and bytes equal to the canonical serialization | `INVALID_ENCODING`, `JSON_MALFORMED`, `JSON_DUPLICATE_KEY`, `JSON_NON_FINITE`, `JSON_FLOAT`, `JSON_TOO_DEEP`, `JSON_INVALID_STRING`, `INTEGER_OUT_OF_RANGE`, `NOT_CANONICAL`, `FIELD_UNKNOWN` |
| 7 | `format` is exactly the supported value | `UNSUPPORTED_VERSION` |
| 8 | every required field present with the right type and value shape; timestamps match the fixed pattern and are ordered; digests are 64 hex; `artifact_id`, `logical_name` and the timestamp agree with the directory name and the marker | `FIELD_MISSING:<f>`, `FIELD_TYPE:<f>`, `FIELD_VALUE:<f>`, `TIMESTAMP_INVALID`, `NAME_MISMATCH` |
| 9 | `files` is exactly the two entries; each file exists; its size equals `bytes`; its SHA-256 equals `sha256` | `INVENTORY_INVALID`, `FILE_SIZE_MISMATCH`, `FILE_DIGEST_MISMATCH` |
| 10 | `baseline.json` passes steps 6 to 8 against its own schema; its `artifact_id` equals the manifest's; `baseline.table_count` and `row_total` equal the document | the document-level codes above, `BASELINE_DUPLICATE_KEY`, `BASELINE_ORDER`, `BASELINE_SEQUENCE_MISMATCH`, `BASELINE_ARTIFACT_MISMATCH`, `BASELINE_SUMMARY_MISMATCH`, `BASELINE_TIME_OUTSIDE_CAPTURE` |
| 11 | if `expected` is supplied (source identity, expected head, minimum format): `source.system_identifier`, `database_name` and `schema.alembic_head` match it | `IDENTITY_MISMATCH`, `SCHEMA_HEAD_MISMATCH` |

### 5.1 Reason codes as implemented (Phase 9B-5A)

`src/deployment/backup_artifact.py` defines every code in `REASON_CODES` (one authoritative list; a test requires each to be exercised). The verifier reports **every** structural problem it can find at directory level without trusting unauthenticated content (the manifest is parsed only if the marker binds it, the baseline only if the manifest's inventory authenticates it); **inside one JSON document it reports the first schema violation**, with the field path in the detail. Differences from the table above, all additions: a marker whose `artifact_id` differs from the manifest's is `MARKER_ARTIFACT_MISMATCH`; a required name that is not a regular file is `NOT_A_REGULAR_FILE`; a file that cannot be read is `UNREADABLE_FILE` (reported, never raised); the manifest's `finished_utc >= created_utc` and the baseline's `captured_utc` lying within `created_utc`..`finished_utc` are enforced (`FIELD_VALUE`, `BASELINE_TIME_OUTSIDE_CAPTURE`); inside a baseline, a repeated logical key is `BASELINE_DUPLICATE_KEY`, `fingerprints` not sorted by table is `BASELINE_ORDER`, and `sequence_details` naming different sequences than `sequences` is `BASELINE_SEQUENCE_MISMATCH`; document-level codes (`JSON_*`, `NOT_CANONICAL`, `FIELD_*`, `TIMESTAMP_INVALID`, `UNSUPPORTED_VERSION`) carry the file name in the detail rather than a `BASELINE_` prefix. A size mismatch short-circuits the digest computation for that file. Every file is read **once**, streaming; the hash and the bytes that are parsed come from that same read. Symlinks and Windows reparse points are detected by `lstat` before opening (and `O_NOFOLLOW` where the platform has it); on a platform without `O_NOFOLLOW` a race between the check and the open remains, which is why the verifier is described as integrity checking, not as a defense against an attacker who can write to the directory concurrently.

**Path inspection (corrected in the 9B-5A verifier correction).** The first delivery handed the caller's path string to `lstat`. On POSIX a trailing separator or a trailing `.` component makes the kernel resolve a symbolic link, so a link named like an artifact, spelled `link/` or `link/.`, was reported as a real directory and **verified as `STRUCTURALLY_VALID`**; and links in the *ancestors* of the path were never examined at all (on both Linux and Windows). `_inspect_artifact_path` now validates every component on its own, never `lstat`-ing a path with a trailing separator and never resolving anything before it is validated: empty and `.` components are dropped; `..` pops the previously validated component (sound only because every pushed component was already confirmed to be a real directory), so a link is never collapsed away, and a link that a later `..` would have popped is still rejected; a relative path is anchored at the current directory and that directory's components are validated too; **any** link or reparse point, the artifact directory or any ancestor, is `SYMLINK`; a missing component or a non-directory ancestor is `NOT_A_DIRECTORY`. The path actually used for every later directory listing and file read is rebuilt from the validated components, and a rejected path reaches **no** listing and **no** artifact-content read (asserted with spies). **Stated limits:** the walk is a check, not a lock -- a component replaced by a link after its `lstat` and before the later reads is not prevented, and `O_NOFOLLOW` (absent on Windows) protects only the final component of each file opened; an artifact store that legitimately sits behind a link (a symlinked temporary directory, a junctioned folder) is rejected, and accepting such a store needs an explicit, separately decided trusted-root mechanism rather than silent resolution; case-insensitive name aliasing, 8.3 short names and NTFS alternate data streams are not specially handled; the Windows tests use real NTFS junctions, and real Windows symbolic links could not be created by this user.

**Schema provenance across documents (added in the same correction).** A finalized artifact's `baseline.version` must equal the manifest's `schema.alembic_head`; a null or different version is `BASELINE_SCHEMA_VERSION_MISMATCH`. The first delivery accepted a baseline with version `ffffffffffff` beside a manifest naming `5b02bea0eb85` when the digests and marker were recomputed, because each document was valid on its own. Standalone baseline parsing still allows a null version (a baseline is not always attached to a manifest), so the adapter's null round trip is unchanged. `schema.expected_head` is deliberately **not** compared to the actual head: a head that differs from the repository's expectation is a fact the manifest records, not a structural defect, and this check establishes only that two documents agree about which schema they describe -- not that the database was consistent or that a restore works.

**What a `STRUCTURALLY_VALID` verdict does not mean.** The verdict itself carries `establishes = ("structural_integrity",)` and `does_not_establish = ("database_consistency", "restore_verified", "authenticity")`. Matching hashes, `consistency` flags in the manifest, and `COMPLETE` are never treated as evidence of consistency or of a successful restore; a garbage archive whose digests are correct, and a manifest whose flags claim a failed catalog bracket, both verify structurally. A rewritten manifest with a recomputed marker also verifies: the marker binds against accident, not against someone who can rewrite every file.

**Behavior on the named hazards.** *Malformed JSON, duplicate keys, unknown fields:* rejected with the reason above, never "best effort". *Unsupported versions:* any `format` other than the exact supported string is `UNSUPPORTED_VERSION`; a verifier of version N never guesses at version N+1. *Missing files:* `MISSING_FILE`. *Mismatched digests or sizes:* `FILE_DIGEST_MISMATCH`, `FILE_SIZE_MISMATCH` (compared before any content is trusted). *Unsafe paths:* the inventory may name only `archive.dump` and `baseline.json`; any other value, any separator or `..`, any symlink is `UNSAFE_PATH`/`SYMLINK`, and no path from a file is ever opened. *Interrupted finalization* (section 3's steps): a crash before step 4 leaves `.partial-<id>` with no marker (with or without `RUNNING`) -> `NOT_FINAL`; a crash after step 4 but before step 6 leaves `.partial-<id>` **with** a marker -> still `NOT_FINAL` and never adopted (the next run records it as `FAILED:interrupted` and may verify it only to diagnose); a crash after step 6 leaves a final directory that verifies normally. A final directory that lacks a marker is `MISSING_FILE:COMPLETE`. Consumers (retention, restore, upload) must treat `INVALID` as absent. Retention counts **only** claim-2-valid artifacts toward its quota and never deletes the newest one.

## 6. Consistency (claim 3), precisely

### 6.1 The problem
The baseline must describe exactly the state the archive holds, even while writers run, or every later comparison is meaningless. The 9B-4 rehearsal avoided the issue only because its source was idle.

### 6.2 Snapshot ownership and lifetime [design, with PostgreSQL behavior to validate]
- A **snapshot owner**: one connection, as `napsedge_backup_ro`, opened by the job. It runs `BEGIN ISOLATION LEVEL REPEATABLE READ, READ ONLY` and then `SELECT pg_export_snapshot()`. The owner **owns the snapshot for its whole life** and is the only connection that reads the baseline.
- **Same snapshot for both reads.** All baseline reads of table data (counts, md5 fingerprints) and of catalog *rows* run on the owner. `pg_dump` is started afterwards as `pg_dump --snapshot=<id> -Fc` (single process, **no** `-j`, so only one importer) as the same role.
- **Lifetime.** The owner stays open until `pg_dump` has exited, then re-reads `pg_current_snapshot()`, and only then ends its read-only transaction. The *minimum* requirement [PostgreSQL behavior] is that the exporter stays open until the importer has attached; holding it to the end additionally lets the job record `snapshot_unchanged_at_end`. If the owner disconnects before `pg_dump` attaches, `pg_dump` fails and the run is `FAILED:snapshot_unavailable`.
- **Order of non-snapshot reads.** Everything that is *not* governed by a snapshot (section 6.3, sequences) is read by the owner **before** `pg_dump` starts, so a later archive reading cannot be older than the baseline reading.
- Idle-transaction and statement timeouts for the owner are set longer than the dump deadline (section 8).

### 6.3 What the snapshot does not cover
**[PostgreSQL behavior; to validate]** A snapshot makes *row data and catalog rows* read through it consistent. It does **not** make these consistent: (a) `pg_get_constraintdef`, `pg_get_indexdef` and similar functions, which read the *current* catalog state; (b) role-name resolution (`relowner::regrole`), which uses the current catalog; (c) sequence values, which are live (section 6.5); (d) DDL that commits between the snapshot and `pg_dump`'s own table locks. The existing baseline queries use (a) and (b) for constraints, indexes, owners and ACL text, so they need additional protection. **Measured (Phase 9B-5C5, section 12.8; only the operations tried, this version):** a new index row was not seen by a long-lived REPEATABLE READ observer on its next read, while the text rendered after a role rename and after a column rename was; no wider claim is made.

### 6.4 Additional protection for schema, ownership and ACL observations [design; layer 1 AS WRITTEN FAILED validation, see 12.5]
**Status: PROPOSED; layer 1 AS WRITTEN FAILED its validation (Phase 9B-5C3, section 12.5), and layer 2 is only partial.** Layer 1 was implemented in the test layer exactly as written and **missed every committed catalog change** (an index, an ACL and an owner change): the owner's repeated reads inside its snapshot transaction are snapshot-bound. Layer 2 (explicit `ACCESS SHARE` locks before the export) blocked the `ACCESS EXCLUSIVE` owner change but not index or ACL changes, and a queued exclusive request stalls ordinary reads for up to its lock timeout. Two test-layer alternatives detect the changes and are unreviewed; **the fresh-endpoint candidate was then validated against constraint changes, changes during a real `pg_dump` and role changes (Phase 9B-5C5, section 12.8): it detected every committed difference tried and misses a change and its reversal, its index read queues behind a queued exclusive request (it fails closed), and it does not establish archive consistency; it is still not adopted.** It remains a mitigation, not a guarantee, and no passing case in section 7 would make it a guarantee (see the closing statement of section 7).
Two layers, recorded in `consistency`:
1. **Catalog bracket -- FAILED AS WRITTEN (section 12.5). The original proposal is retained below as HISTORY; it is not a working mechanism and is not an active claim.** *Original proposal text (history):* "The owner computes a digest over the baseline's `constraints`, `indexes` and `acls` reads before the snapshot export and again after `pg_dump` exits. If the digests differ, the run is `FAILED:catalog_changed`. This detects a concurrent migration, grant, revoke or owner change that overlapped the capture; it cannot prove a change and its exact reversal did not occur. `catalog_bracket_equal` is recorded." **Validated result:** implemented exactly so, it detected **none** of the committed changes tried (an index, an ACL and an owner change, each proven committed and visible to a fresh observer), because the owner's repeated catalog reads inside its snapshot transaction are snapshot-bound; it therefore did not refuse finalization. **A recorded `catalog_bracket_equal = true` from this procedure is NOT evidence that the catalog was stable.** Two test-layer alternatives (fresh committed transactions at each endpoint, or the owner observing after its transaction ended) detect these changes and are unreviewed; neither can detect a change and its reversal.
2. **DDL exclusion by locks (optional).** Before exporting the snapshot the owner takes `ACCESS SHARE` locks on every application table (the backup role's `SELECT` suffices), so DDL needing `ACCESS EXCLUSIVE` was intended not to commit during capture. **Validated (12.5):** the locks block `ACCESS EXCLUSIVE` operations only while they are held (ordinary baseline reads take the same locks only after the reads; an owner change was blocked under the explicit variant), and do **not** stop index or ACL changes. **Risk, now observed:** a *queued* `ACCESS EXCLUSIVE` request blocks later readers, so a migration waiting behind the backup could stall runtime reads; the migrator would need a `lock_timeout`. Whether to accept this availability trade-off is a decision (section 11). `ddl_exclusion` records the choice; `none` relies on layer 1 alone.
Neither layer is claimed to be sufficient: section 7's catalog and lock cases (C6, C7) have now run (section 12.5), and layer 1 as written is **not** sufficient.

### 6.5 Sequences: the exact acceptance rule
**Status: the NapsEdge sequence facts below are verified. Cases C3, C4 and C5 of section 7 were executed in Phase 9B-5C2 (section 12.4) and showed the behavior this section predicts for those configurations; the `monotonic_v1` rule remains PROPOSED and is not implemented in production. C6 and C7 were executed in Phase 9B-5C3 (section 12.5) and C9 and C10 in Phase 9B-5C4 (section 12.6): they concern catalog observations, locking, overlap and quiescence, not the sequence rule, and did not change this section's conclusions; every case of section 7 has now been executed (C6 failed as written; C10's endpoint evidence cannot establish quiescence).** The rule is not implemented in 9B-5A, and the strict recovery comparator is unchanged.
**[project fact, from offline migration SQL]** NapsEdge has 24 tables and **13 sequences**, all created implicitly by `BIGSERIAL id` columns: `team_aliases`, `player_aliases`, `source_artifacts`, `data_quality_issues`, `player_stat_snapshots`, `roster_memberships`, `team_stat_snapshots`, `team_game_log_observations`, `team_game_stat_lines`, `espn_benchmark_snapshots`, `espn_source_observations`, `espn_benchmark_values`, `player_game_log_observations` (each `<table>_id_seq`). No identity columns, no `CYCLE`, no `CACHE` clause, no explicit sequence DDL; PostgreSQL's defaults then apply (bigint, start 1, increment 1, cache 1, no cycle, owned by the column). Every other table uses natural or caller-supplied keys, not sequences. A static search found **no** `setval`, `ALTER SEQUENCE`, `RESTART IDENTITY` or `nextval` call in `src/` or `alembic/`.

**Vocabulary [PostgreSQL behavior].** A sequence holds `(last_value, is_called)`. The **next value** it will hand out is `last_value + increment` if `is_called`, else `last_value`. `pg_dump` emits `setval(seq, last_value, is_called)`, so a restored sequence's next value equals the dumped sequence's next value at the moment `pg_dump` read it. `pg_sequences.last_value` (what the existing comparator's `sequences` field holds) is `NULL` when `is_called` is false, so it **loses** the value of a not-yet-called sequence; `sequence_details` therefore reads `SELECT last_value, is_called FROM public.<seq>` directly (the backup role has `SELECT` on sequences, and `pg_dump` needs the same). `nextval` is **not transactional**: it is never rolled back, so rolled-back inserts leave gaps, and no snapshot freezes a sequence.

**Why equality fails under concurrency.** A writer calling `nextval` between the owner's sequence read and `pg_dump`'s read makes the archive's next value larger than the baseline's. That is correct behavior, not an error. Under *quiescent* capture it cannot happen and equality is required.

**The rule (`monotonic_v1`).** Let `N_b` be a sequence's baseline next value and `N_r` its next value on the restored database. For every sequence, **all** of these must hold:
- **R0 (applicability, checked at capture and recorded):** `increment > 0`; `cycle` is false; `owned_by` is non-null. If any fails, the capture must use `equal_v1` (quiescent) or fail.
- **R1 (no regression):** `N_r >= N_b`. Holds because the baseline read precedes the archive read and sequences do not decrease.
- **R2 (next-ID safety, the actual safety condition):** `N_r > column_max_restored`, where `column_max_restored` is the maximum of the owning column **in the restored table** (an empty table passes). This is computed on the restored database; it is *not* derivable from the baseline.
- **R3 (headroom, informational):** `N_r <= maximum` -- otherwise the next insert fails rather than reuses an id.

**What this does and does not prove.** R1 alone **does not** prove next-ID safety: it compares only against the baseline. A manually supplied id above the sequence, a `setval` to a lower value, a restart, or a `TRUNCATE ... RESTART IDENTITY` (none of which NapsEdge code does, but which a human or a future change could) leaves R1 true while a collision is still possible. R2 is the check that matters, and it assumes ids are unique per column (the primary key enforces that in the restored data). Cache values above 1 would only make `last_value` run *ahead* of handed-out ids (safe direction) and are not used. Ids that rolled-back or in-flight transactions consumed are gaps, not hazards. After a server crash a sequence may skip forward but not backward. Ids the source issued **after** the snapshot are not in the archive; the restored system will reuse them, which is an ordinary consequence of the recovery point (the readers of this database, Discord and Streamlit, never record ids externally).

**The strict comparator is untouched.** `compare_to_baseline` keeps comparing `sequences` for equality, which is correct for quiescent capture and for the 9B-4 rehearsal. `monotonic_v1` is evaluated by *new* verification logic over `sequence_details`; it never loosens the existing comparison.

### 6.6 Quiescent capture, for contrast
`quiescent` mode drops the shared snapshot's role as the guarantee and instead **requires** that nothing writes during the capture. Requirements: (1) the capture holds a non-blocking advisory lock that writers respect; the existing controlled import respects `bulk_import_advisory_lock`, **but Alembic migrations and ad hoc operator sessions do not take any lock**, so exclusion of DDL and of unmanaged sessions is **operational discipline, not enforcement**; (2) foreign sessions holding a write-capable role are counted at start and at end and recorded (`foreign_write_sessions_*`) -- **these two endpoint samples cannot establish continuous quiescence**: a session can connect, write and disconnect between the samples, a connected session need not have written anything, and the count says nothing about unmanaged DDL, so the numbers are evidence for an operator to weigh and never proof. Continuous exclusion would require either every writer cooperating with a lock the capture holds, or a database-level mechanism that prevents writes for the whole window; neither exists and no such mechanism is designed or validated here; (3) sequences compared by equality (`equal_v1`); (4) imports and migrations cannot run during the window. It is simpler to reason about and much weaker to enforce. It is also acceptable as a **fallback**: the snapshot mode can still be used while quiescent, in which case the recorded counts are evidence.

**Observed (Phase 9B-5C4, section 12.6).** Endpoint counts observed by the backup role missed a short-lived non-cooperating writer (counts 0 and 0 while a committed, sequence-advancing insert reached the dump) and counted a connected write-capable session that wrote nothing; the backup role sees another role's session only through `pid`, `datname`, `usename` and `application_name` (`backend_type` and `state` are NULL, `query` is `<insufficient privilege>`), and `pg_stat_activity` is cached per transaction, so samples must come from fresh transactions. The advisory lock did not stop a writer that does not take it. The endpoint count is therefore characterization evidence only and **cannot establish continuous quiescence**, as stated above.

## 7. Bounded disposable validation plan (a plan; all ten cases executed: C1, C2, C8 in 9B-5C1, 12.3; C3-C5 in 9B-5C2, 12.4; C6, C7 in 9B-5C3, 12.5; C9, C10 in 9B-5C4, 12.6; the catalog-observer candidate was then validated in 9B-5C5, 12.8)

Purpose: establish concurrent-capture behavior [PostgreSQL behavior] before operational use. Scope and bounds: **one** disposable `postgres:16-alpine` cluster reusing the 9B-4 `launched_cluster`, ledger and guards; synthetic data only; a restore target in the same cluster (a separate fresh database owned by the migrator) because independence of clusters is not what is being tested; at most **2 writer threads**, each capped at **5,000 inserts**; a **60-second** hard deadline per case; the whole module budgeted at about **3 minutes**; no real credentials. **No case relies on timing for correctness:** writers and the capture are coordinated with barriers (events), so each case is deterministic.

| Case | Setup | Must be observed |
|---|---|---|
| C1 snapshot sharing under concurrent writers | owner exports a snapshot and reads the baseline; a barrier then releases writers to commit rows; `pg_dump --snapshot` runs; restore the archive | restored fingerprints **equal** the baseline although rows were committed after the export |
| C2 negative control | read the baseline in a **separate** transaction, release writers, then dump | fingerprints **differ** (proves C1 can fail) |
| C3 sequence behavior | barrier: writers call `nextval` between the owner's sequence read and `pg_dump` | restored `N_r > N_b` strictly; R1, R2, R3 hold; **equality is not expected** (shows why `equal_v1` cannot apply) |
| C4 manual-id hazard | before capture, insert a row whose id is above the sequence's next value | R1 passes but **R2 fails** (shows R1 alone is insufficient) |
| C5 rule applicability | alter a copy of a sequence to `CYCLE` or a negative increment | R0 fails and capture refuses `monotonic_v1` |
| C6 catalog bracket | DDL (an index, a `GRANT`, an owner change) committed between the first digest and the last | `FAILED:catalog_changed`; a no-op control run passes |
| C7 lock mode | with DDL exclusion on, a migrator `ALTER TABLE` under a `lock_timeout` | the DDL waits then times out; and record whether a queued `ACCESS EXCLUSIVE` stalls a concurrent reader (the risk in 6.4) |
| C8 owner loss | kill the owner connection before `pg_dump` attaches | `pg_dump` fails; `FAILED:snapshot_unavailable`; no artifact finalizes |
| C9 overlap | a second job while the first holds the advisory lock | `SKIPPED:overlap`; confirm the backup role may call `pg_try_advisory_lock` |
| C10 quiescent mode | lock held, no foreign writers; then a non-cooperating writer connects, writes and **disconnects before the end sample**; a second writer that stays connected until the end sample | sequence equality is **not** guaranteed; the first writer is **not detected** by the endpoint counts (the case exists to demonstrate that limitation); the second is counted, which shows only that it existed, not that it wrote |

**Execution status: C1, C2 and C8 were executed and passed for table-data behavior only (Phase 9B-5C1, section 12.3); C3, C4 and C5 were executed and passed for sequence behavior only (Phase 9B-5C2, section 12.4); C6 and C7 were executed in Phase 9B-5C3 (section 12.5): C7's observations were recorded and **C6 showed the catalog bracket as written does not meet its purpose** (it missed every committed change; the requirement is unmet). C9 and C10 were executed in Phase 9B-5C4 (section 12.6): **C9 passed** (the existing lock refuses a second session with `SKIPPED:overlap` and is released on every tested path) and **C10's safety requirement is unmet** (endpoint session counts missed a short-lived writer and a positive count does not imply a write). All ten cases have now been executed; none establishes consistency for a real artifact.**

Cleanup is verified by the ownership ledger, including on failure. A case that cannot be made deterministic is dropped and recorded as unvalidated rather than weakened. **Claim 3 may not be asserted for any real artifact: all ten cases have now been executed, but C6 failed as written and C10's endpoint evidence cannot establish quiescence, so the design questions they raise (section 11, items 6 to 8) are unresolved.** **Passing C1 to C10 would show behavior for those cases only; it is not proof that capture is safe against all unmanaged changes** -- changes to catalog objects the digest does not cover, a change made and reverted inside the bracket, a session that bypasses every cooperating lock, writers visible to neither endpoint sample, or PostgreSQL versions and settings the cases do not exercise. The catalog bracket and the concurrent-sequence policy therefore remain **PROPOSED and UNVALIDATED**, and even after the cases pass they remain mitigations whose residual risks the manifest records rather than hides.

## 8. Credentials, bounded execution, overlapping jobs

- **Credentials.** Backup role only. A private temporary directory (mode 0700) holds a 0600 pgpass file built from the secret source on stdin; only `PGPASSFILE` is exported; `--no-password`; removed on every exit path. The secret store is not decided. Artifacts and logs carry digests, counts, exit statuses and reason classes only.
- **Bounded execution.** An overall deadline; a timeout on every child process, killed with its process group; `connect_timeout` on every connection (15 s in the tested runbook); `statement_timeout` for baseline queries; an idle-in-transaction timeout for the snapshot owner longer than the dump deadline (a trade-off to settle in validation). A timeout produces `FAILED:timeout`, never a hang.
- **Overlap.** A local lock file with stale-process detection **and** a non-blocking database advisory lock for the whole run (`pg_try_advisory_lock`); failure is `SKIPPED:overlap`.
- **Source identity.** Before dumping, confirm the connected cluster's `system_identifier` and database name match the job's configured designation (the same idea as `verify_database_target`), so a mis-pointed job cannot label one database as another.

## 9. Reuse, and the smallest code unit after this review

| Component | Where | Status for this contract |
|---|---|---|
| `DatabaseBaseline`, `capture_baseline`, `compare_to_baseline` | `tests/postgres/pg_restore_compare.py` | **stay in the test layer.** The file-schema code must not import or promote them. They remain the single definition of what a baseline contains and how it is compared |
| `provision_backup_access`, `assert_backup_boundary`, pgpass, `run_dump`, stage guards, ownership ledger | TLS and fresh-cluster test modules | test-layer patterns; reused by the validation plan |
| `verify_database_target`, `compute_advisory_lock_key`, `bulk_import_advisory_lock` | `src/pipeline/bulk_import_safety.py` | pattern for source-identity and overlap checks in a later unit |
| process-specific fail-fast validation | `src/deployment/entrypoints.py`, `env_validation.py` | pattern for a later job entrypoint |

> **Status (Phase 9B-5A):** the **read side** of this list is implemented -- typed documents, strict JSON and canonical serialization, marker parsing and `verify_artifact_directory()` -- with its tests and a test-layer adapter (section 12). The **write side** named below (the marker writer, the `.partial` build and atomic-rename finalizer, and the `FAILED` record writer) was implemented in **Phase 9B-5B** as a sibling module, `src/deployment/backup_artifact_writer.py` (section 12.1); the read-side module stayed separate and still performs no writes.

**Recommended smallest code unit (Phase 9B-5 implementation): the file contract only.**
- **Where:** a new module `src/deployment/backup_artifact.py` in the existing `src/deployment/` package (operations code, no database dependency), with tests in `tests/regression/test_deployment_backup_artifact.py` following that package's test naming, in the default suite.
- **Dependencies:** the standard library only (`json`, `hashlib`, `os`, `pathlib`, `re`, `uuid`, `dataclasses`, `datetime`). No new dependency; `pydantic` is available but not needed and would add a second schema authority.
- **Contents:** typed records for the manifest and baseline documents; strict canonical JSON read/write (duplicate-key, unknown-key, float and size rules of section 4); SHA-256 and size helpers; the `COMPLETE` marker writer and parser; the `.partial` build and atomic-rename finalizer with the flush order of section 3; the `FAILED` record; and `verify_artifact_directory()` implementing section 5's predicate with its reason codes. The baseline document is validated for shape and types; **it contains no capture or comparison code and no SQL**, and `sequence_details` is validated structurally only (the monotonic rule is **not** implemented in this unit).
- **Drift guard, in the test layer:** one test asserts that the schema's baseline field names equal `dataclasses.fields(DatabaseBaseline)` plus the documented `sequence_details`, and a test-layer adapter maps `DatabaseBaseline` <-> document (tuples <-> arrays), proving a round trip through the production schema reproduces an identical `DatabaseBaseline`. This keeps a single baseline representation and a single set of normalization rules without moving database helpers into production.
- **Tests:** a valid artifact; one case for **each reason code** in section 5; interrupted finalization at every step; duplicate keys, unknown keys, floats, `NaN`, oversize and non-UTF-8 input; unsupported manifest and marker versions; path traversal and symlink attempts (skipped, with the reason stated, where the platform cannot create a symlink); a `FAILED` and a conflicting-markers directory; digest and size tampering of each file; and the round-trip drift test. No database, `pg_dump`, Docker, scheduler or storage.
- **Explicitly out of scope:** any capture, `pg_dump` call, connection, advisory lock, snapshot, sequence rule evaluation, scheduler, storage, retention or monitoring. Those are separate, separately authorized units after section 7's validation: snapshot-consistent capture; then the bounded job wrapper; then restore verification producing `verification.json`; then storage, retention and monitoring.

## 10. Later requirements (not designed here)

| Area | What the contract must continue to support |
|---|---|
| Storage | off-host, encrypted, access-controlled, versioned or write-once; `COMPLETE` uploaded last; separate write and restore credentials |
| Retention | counts claim-2-valid artifacts only; never deletes the newest valid one; monthly artifacts exempt from daily pruning; the numbers (22.5: 7 daily, 4 weekly, 6 monthly) need approval |
| Monitoring | alert on no claim-2-valid artifact newer than the recovery-point objective; a `FAILED` or consecutive `SKIPPED` streak; a size deviation from the predecessor; an invalid artifact in the store |
| Restore verification | claim 4 via the runbook against the artifact's own baseline; `verification.json` binds the manifest digest; never edits the artifact |

## 11. Decisions

**Already established (section 1):** the backup role and its grants; TLS and credential handling; `pg_dump -Fc` and SHA-256; the baseline's content; the unchanged strict comparator; the runbook gates; sensitivity and off-host requirements.

**Specified here as design choices that stand unless Anthony objects:** the four-claim vocabulary; the file layout, schemas, strict-JSON rules and validity predicate; the finalization order; stdlib-only code in `src/deployment/`; the baseline kept field-for-field identical to `DatabaseBaseline`; the monotonic sequence rule `R0`-`R3` as a *verification* rule that does not touch the strict comparator.

**Material decisions that require Anthony:**
1. **Consistency policy.** Shared-snapshot capture (recommended) with the catalog bracket and `monotonic_v1`; or quiescent capture only (imports and migrations blocked during the window, operational discipline); or both, with the snapshot as the guarantee and quiescence as evidence.
2. **DDL exclusion during capture.** Whether to accept the optional lock layer (6.4), which can delay migrations and, if one queues, stall reads -- or to rely on the catalog bracket alone and retry on `FAILED:catalog_changed`.
3. **Authorization for production code.** Approval to add `src/deployment/backup_artifact.py` (stdlib only) and its tests, scoped to the file contract above.
4. **The test-oriented `max_alias_id` field.** Carry it 1:1 in baseline v1 (recommended, no change to the comparator), or replace it with the general `column_max` in `sequence_details` and adapt the comparator in a later, separately reviewed change.
5. **Recovery-point objective, schedule and retention values; storage destination, key custody and any cost.** Not needed for the file contract and not begun; required before any job is installed.
6. **Catalog-observation design (open; raised by C6, section 12.5).** The bracket as written does not detect committed changes. Choose between fresh committed transactions at each endpoint, the owner observing after its transaction ends, or accepting the residual risk and recording it in the manifest; **Update (Phase 9B-5C5, section 12.8):** the fresh-committed-endpoint candidate was validated for constraint changes, changes during the dump itself, and role rename and drop; it detected every committed difference tried, it does not detect a change and its reversal, its observation fails closed while an exclusive request is queued, and it does not establish that the archive is consistent. The other two options and the residual-risk option were not evaluated. Not adopted.
7. **Lock policy (open; raised by C7 and C9, sections 12.5 and 12.6).** Whether to use the explicit lock layer (it blocks `ACCESS EXCLUSIVE` operations only while held and stalls readers for up to the migrator's lock timeout, which is not chosen), and whether the advisory key is backup-only or shared with controlled imports (today it is shared), (the release-masking defect in `bulk_import_advisory_lock` was corrected in the 9B-5C4 correction, section 12.7). Not chosen.
8. **Quiescence evidence (open; raised by C10, section 12.6).** Whether `foreign_write_sessions_*` stays evidence-only (it cannot establish quiescence), whether to give the backup role `pg_read_all_stats` to see more of other sessions (a privilege expansion, not made), or to drop quiescent mode.

**Resolved by Anthony on 2026-10-05 (the three 13.8 decisions; recorded here, nothing implemented).**
- **Decision 1:** the exported snapshot with `monotonic_v1`, as the basis of the section 13 operating contract.
- **Decision 2:** no explicit table-lock layer (`ddl_exclusion = none`).
- **Ownership:** v1 refuses unsupported ownership, as specified in 13.2.
- **Decision 3 (production code):** extended to the read-only capture preflight of 13.7 only.

Decisions 6, 7 and 8 remain open.

**Resolved by Anthony on 2026-10-09 (the Phase 9B-8C completion-checklist recommendations, `docs/phase9b_completion_checklist.md` section 3; approved as a whole):**
- **Decision 8:** `foreign_write_sessions_*` stay null, meaning "not sampled", in `exported_snapshot` mode. Quiescent mode is retired for v1, and `pg_read_all_stats` is not granted. Consistency rests on the exported snapshot. There is **no** evidence that writers were absent, and session sampling could never show that.
- **Decision 7:** the advisory key stays shared with imports (imports are refused while a backup runs). With no table-lock layer, the migrator `lock_timeout` needs no backup-specific value.
- **Strict sequence differences:** fail closed. Any strict `sequences` difference withholds a passing drill and claim 4, even when `monotonic_v1` explains it.
- **Recovery ready, per artifact (claim 4):** a drill with `passed: true`. That requires claim 2 immediately before the restore, restore exit 0, strict all equal including sequences, R2 passing, a clean version history bound to the exact payload version, and a written-once record naming the manifest and runbook digests.
- **Recovery ready, system level:** all of these must hold:
  1. the newest remotely verified artifact is within the RPO (24 h);
  2. a passing claim-4 drill on the provider's PostgreSQL 16 exists within 31 days;
  3. its measured restore time is within the RTO (4 h);
  4. `backup-status` is ok, with no version anomalies;
  5. both offline identity copies were verified within 12 months.
- **Refusal-retry:** no automatic retry of a capture refusal; only the bounded overlap retry. The next scheduled cycle is the retry, and one manual cycle is allowed after reading the reason. Retrieval and drill refusals are never retried automatically.
- **Object Lock / signed records:** optional enhancements. Versioning plus detection is accepted for v1, with the residual risks recorded (deletion-key holder, a forged artifact under a new name, written-once by detection rather than prevention). Reconsider when production data has value.
- **Not decided here:** the schedule and retention values (open decision 6 / bundle 2), PITR, secrets decision 9, and any spending (Anthony is still testing; DigitalOcean remains deferred).

---


## 12. Implementation status (Phase 9B-5A, 2026-10-02)

**Implemented (read side only).** `src/deployment/backup_artifact.py` (standard library only): typed `ArtifactManifest` and `BaselineDocument` with their component records; `parse_strict_json` and `canonical_json_bytes`; `parse_manifest`, `parse_baseline`, `parse_completion_marker` and `parse_artifact_directory_name`; `verify_artifact_directory()` returning a `Verdict` with a status, every reason found and the `establishes` / `does_not_establish` statement; and the single authoritative `REASON_CODES`. It writes nothing, repairs nothing, performs no capture, SQL, subprocess, connection, scheduling or storage, evaluates no sequence rule, imports nothing from the test tree, and performs no filesystem, network or process work at import time (each asserted by a test). **Test layer:** `tests/postgres/baseline_document.py`, the adapter between `DatabaseBaseline` and the production document, kept outside production so that `DatabaseBaseline`, `capture_baseline` and `compare_to_baseline` remain the single definition of baseline content and comparison. `max_alias_id` is carried 1:1 and the comparator is unchanged.

**Tested.** `tests/regression/test_deployment_backup_artifact.py`: 475 tests (469 passed, 6 skipped, 3.5 s), synthetic directories with genuinely computed digests, an independent canonical-JSON oracle, explicit expected codes for every documented reason code (a static check requires full coverage), tampering, all four interrupted-finalization stages, strict-type failures including booleans in integer fields and floats, size limits applied before JSON is loaded, streaming (a 16 MiB archive verified with a bounded memory peak), read-only behavior (directory snapshots unchanged, verification with every file read-only), and baseline round trips. `tests/postgres/test_postgres_baseline_document.py` (1 test) captures a genuine `DatabaseBaseline` from a disposable, migrated PostgreSQL, pushes it through the production schema as bytes, and requires exact equality plus a clean result from the **unchanged** comparator on the deserialized baseline. **Mutation check:** of eight deliberate defects in the verifier (a boolean accepted as an integer, duplicate keys allowed, the canonical check removed, an archive digest not compared, marker binding ignored, the size limit not applied, a `.partial` directory accepted, floats allowed) seven were caught; the eighth (removing the float-parsing hook) is an **equivalent mutant** because a second layer rejects floats identically.

**Verified.** In a clean worktree of `373b35452bb47f71d5f85cbca1e0624a9e4139f3` plus only the intended uncommitted files: the default-suite selection (the new module, the comparator unit tests, the Alembic DSN regression and the four existing deployment test files) **631 passed, 6 skipped, 0 failed** (JUnit: 637 tests); the live adapter check **1 passed**. The existing deployment checks also ran in the working tree: 114 passed. **Platform-specific skipped coverage, reported exactly:** the 6 skips are the real-symlink tests (four required files, the directory itself, and an extra entry); this user cannot create symbolic links here (`OSError`), so symlink coverage was **not exercised**. The Windows reparse-point path **was** exercised by a real directory junction, and the link/reparse predicate is covered by logic-only tests on synthetic stat results, which are not a substitute for real symlinks. No other test was skipped.

**Specification corrections made in this revision:** the JSON value rule (floats and non-finite rejected; objects, arrays, strings, exact integers, booleans and documented nulls permitted); `RUNNING` removed before `COMPLETE` and before publication; endpoint session counts cannot establish continuous quiescence (and C10 no longer claims detection); the catalog bracket and concurrent-sequence policy stated as proposed and unvalidated, with passing selected cases not proof against all unmanaged changes; the `fingerprints` dictionary mapping made explicit; the verifier's additional codes and limits documented (5.1).

**Correction after the first delivery (two defects independently reproduced against that module).** (1) *Artifact-root link bypass:* on Linux a symlink named like an artifact verified as `STRUCTURALLY_VALID` when spelled with a trailing separator or `/.`, and an artifact beneath a linked parent verified as valid on **both Linux and Windows**; (2) *schema provenance mismatch:* a baseline with version `ffffffffffff`, a manifest naming head `5b02bea0eb85`, and recomputed digests and marker verified as valid. Reproduced here before any change: on Linux with real symlinks (link itself rejected; `link/`, `link//`, `link/.`, `link/./` and similar variants **accepted**; linked parent **accepted**); on Windows with NTFS junctions (link itself and **all** trailing-separator and dot variants already rejected, so the root-variant bypass is POSIX-specific, but the linked parent was **accepted**); both defects on both platforms for the version check. **Corrections:** component-wise path inspection (5.1) and the `BASELINE_SCHEMA_VERSION_MISMATCH` cross-check (5.1). The first delivery's statements that the verifier "never follows a symbolic link or reparse point" and that symlinks were rejected were **incomplete** and are superseded by 5.1.

**Verified after the correction.** Real-filesystem tests cover the plain path, trailing-separator, `/.`, doubled-separator, dot-component and `..` variants of a link named like an artifact, an artifact beneath a linked parent and a linked grandparent (absolute, relative, forward-slash spellings), `..` not collapsed through a link, ordinary spellings of a real path staying valid, missing/non-directory/NUL/drive-relative paths, and spies proving a rejected path reaches no directory listing and no content read (with a control showing the spies do observe reads); the schema-version tests change the baseline and then **recompute the inventory digests and the marker**, and independently assert they are correct before expecting the cross-document code. **Results as observed:** Windows (real junctions) module **505 passed, 7 skipped** (512 collected); Linux (real symlinks, in a disposable `--network none` container built from the project's own local image, repository mounted read-only, `pytest` and its pure-Python dependencies copied from this machine's site-packages into a read-only scratch mount; nothing pulled, installed or changed) module **507 passed, 1 skipped** (508 collected); clean worktree of `373b354` plus intended files: Windows default-suite selection **667 passed, 7 skipped** (JUnit 674), Linux **555 passed, 1 skipped** (JUnit 556: module, comparator unit tests, DSN regression), live adapter check **1 passed**. **Skips, exactly:** Windows -- the six file-level symlink tests (this user cannot create symlinks) and one POSIX-only test; Linux -- the one Windows-only junction test. The Windows and Linux module counts differ (512 against 508) only because the trailing-variant parametrization de-duplicates on POSIX. The new tests run against the **unmodified** module: 33 fail on Windows and 29 on Linux; on Windows the failures in the root-variant tests are only the new detail wording (the old module did reject those spellings there), while the linked-parent, `..`, relative and version tests fail for real. **Mutation check of the new code:** ignoring ancestors (caught on both), normalizing the path before validating (caught on both), removing the version check (caught on both), tolerating a null version (caught on both), and handing the caller's string to `lstat` (caught on Linux, **not** caught on Windows, where that approach is not exploitable). The earlier counts (475 tests / 469 passed / 6 skipped; clean worktree 631 / 6) remain the results of the first delivery's tree. Manifests: unchanged (the new helpers are private; the 50 public symbols are the same; no collision).

**Decisions resolved.** Decision 3 (production code) was given for the read side only. Decision 4 (`max_alias_id`): carried 1:1 and the comparator unchanged.

**Deferred and still unvalidated:** the marker writer, finalizer and `FAILED` writer; any capture, `pg_dump` call, connection, snapshot or lock; the sequence rule and `sequence_details` capture; every case in section 7 (none was run); storage, retention, monitoring, scheduling and restore-verification records. **Manifest handling:** the manifests are keyed by real notebook function name, and a new public symbol with no notebook origin gets no row; a collision check of all 50 public top-level symbols of the new module (7 functions, 22 classes, 21 constants) against both manifests found **no collision**, so no row was added or changed and both remain at 1,156 data rows (1,157 physical lines including the header), identical key sets.

---

### 12.1 Phase 9B-5B: the local write side (2026-10-02)

**Implemented.** `src/deployment/backup_artifact_writer.py` (standard library plus the contract module; 709 lines): `write_artifact(parent_directory, archive_path, metadata, baseline, *, durability=DurabilityMode.REQUIRED, ops=None) -> WrittenArtifact`, `write_failed_record(owned, reason_class, *, ops=None)`, the caller-supplied `ArtifactMetadata`, the `DurabilityMode` choice (`REQUIRED`, `BEST_EFFORT_REPORTED`), the failure taxonomy (`PreconditionError`: nothing was created; `WriteFailed`: an owned partial exists and was marked `FAILED`; `PostPublicationError`: the rename may have happened), and one injectable `FileSystemOps` seam used only to inject faults in tests. The read-side module changed only by a **behavior-preserving split**: `verify_artifact_directory` now inspects the path, refuses `.partial-*` (`NOT_FINAL`) and delegates to `_verify_contents(root, name, expected)`, and the manifest/baseline relation checks moved unchanged into one pure helper, `_manifest_baseline_relations`, so the verifier and the writer share a single definition. The public verifier's refusal of `.partial` directories is unchanged and asserted. The AST test that the read-side module contains no write operation still passes.

**What the writer takes and what it invents.** The caller supplies the archive path, the baseline document and the manifest metadata (source identity, schema head, tooling, repository state, consistency claim, `created_utc`/`finished_utc`, the archive's `pg_restore --list` fields). The writer derives only what follows from bytes it handled itself: file sizes, SHA-256 digests, the baseline summary counts, and the completion marker. It does **not** check the table-of-contents fields (that needs `pg_restore`), invents no source identity, schema head, capture evidence or restore status, and performs no capture, SQL, `pg_dump`, snapshot, sequence evaluation, scheduling, retention, subprocess or external-storage work.

**Finalization order as implemented.** (0) every precondition is checked before anything is created (metadata and baseline validated through the contract; archive is an openable regular file and not a link; the store is a real directory with no linked ancestor, via the verifier's own path inspection; the destination and the partial do not exist; under `REQUIRED`, directory flushes are obtainable); (1) `.partial-<artifact_id>` is created **exclusively** and privately; (2) `RUNNING` is created (strict JSON: format, artifact id, pid, UTC time) and flushed; (3) `archive.dump` is **streamed** in bounded chunks, hashed as written, and flushed, and the source's size and mtime are compared afterwards (a source that changed while being copied is not published) and the source is closed -- one attempt, before anything is published (12.2); (4) `baseline.json`, then (5) `manifest.json`, each written and flushed, the manifest bound to the digests and sizes of the bytes actually written; (6) **`RUNNING` is removed and the staging directory flushed**; (7) `COMPLETE` is written last, binding the manifest's actual digest and byte count, flushed, and the staging directory flushed; (8) the staged directory is **validated by the verifier's own content check with the final name**, and the manifest it parsed must equal the one the writer built; (9) publication; (10) the parent directory is flushed. The input archive is opened read-only and is never altered.

**Publication.** One `rename` of a directory within a single filesystem. Windows refuses a rename onto an existing destination of any kind. POSIX `rename` would silently replace an **empty** destination directory, so on POSIX the destination is first **reserved with an exclusive `mkdir`** (which fails if anything, even an empty directory, exists) and the rename then replaces only that reservation. A rename that fails with the staging directory still present is treated as **not published**: only the writer's own, still-empty reservation is removed, and the finished partial is kept. There is **no** cross-filesystem copy-and-delete fallback, and no replacing rename anywhere (asserted by an AST test). If the process dies between the reservation and the rename, an empty directory with the final name remains; it verifies as invalid, is never adopted or deleted by the writer, and blocks a retry of the same artifact id until an operator removes it.

**Concurrency assumptions (stated, enforced where possible).** The store directory is written only by cooperating writers that use this module. Exclusive creation of `.partial-<artifact_id>` guarantees that two attempts for the same artifact id cannot both proceed; different ids never conflict. A process that tampers with the store during a write (removing the reservation, swapping directories) is outside the guarantee. The writer takes no lock and uses no advisory lock; the path walk, as in 5.1, is a check and not a lock.

**Durability, per operation.** *File flush* (`flush` then `fsync` on every file): supported on Linux and Windows; a failed file flush is an error in **every** mode and never reported as success. *Directory flush* (`fsync` of a directory descriptor): supported on Linux; **unsupported through the Python standard library on Windows**. Hence the default mode `REQUIRED` **refuses before creating anything** where directory flushes are unsupported, and the explicit mode `BEST_EFFORT_REPORTED` proceeds and lists every skipped directory flush as a **gap** in the result (`durable` is `False`); a directory flush that *fails* for a real reason (for example an I/O error) is an error in both modes. *Atomic visibility* is reported separately (`atomic_rename`). **Fault injection proves how the writer handles a reported failure; it is not proof of survival of an actual power loss.** Privacy: staging directory and files are created `0700`/`0600` and, on POSIX, the directory mode is verified; on Windows the mode is not applied, access follows the parent directory's ACL, and the result says so.

**Failure handling.** A failure before the rename marks the owned partial `FAILED` and raises `WriteFailed` carrying the stage, the reason class, `published="no"`, and whether the record was written. `FAILED` supersedes `RUNNING` (a best-effort removal; both may remain, and the directory stays invalid). The record is the fixed four-key document of section 3 with an enumerated class; credentials, DSNs, exception text and stack traces cannot enter it. If recording `FAILED` itself fails, the **original** failure is still raised and the recording failure is attached as a note. An interrupt (`KeyboardInterrupt`) is recorded as `interrupted` and propagates unwrapped. A failed partial stays invalid (`NOT_FINAL` to the public verifier, and invalid even if renamed by hand), is **never adopted, resumed, repaired or deleted** by the writer, and blocks a retry of the same artifact id. **After the rename** the artifact may already be published: the writer then raises `PostPublicationError` (`published` is `"yes"`, or `"unknown"` if the outcome cannot be classified), never claims the final directory is absent, and **deletes nothing**; the caller must verify the directory with the public verifier before use. A failure only of the final parent-directory flush is reported the same way, with `published="yes"`.

**What a `WrittenArtifact` establishes.** Claim 1 (the files are complete) plus a structural and checksum check by the contract's own rules immediately before publication. It does **not** establish database consistency (claim 3), restore success (claim 4), authenticity, or that the data survived anything beyond the flushes it reports.

**Tested.** `tests/regression/test_deployment_backup_artifact_writer.py`: 89 tests on synthetic archives and temporary directories with genuinely computed digests. Success passes the **unchanged public verifier** with exactly the four files (no `RUNNING`, no `FAILED`); the input archive and an unrelated bystander directory are byte- and name-identical afterwards; supplied metadata is recorded exactly and nothing is invented; sizes and digests are those of the bytes written; the documented order is asserted from the operation log; the archive is streamed (bounded memory); invalid metadata, baselines, archives, stores and linked ancestors are refused before anything is created; an existing destination (empty directory, non-empty directory, regular file, link, a previously published artifact) is never replaced, and neither is an empty or non-empty directory that appears during the write; an existing partial (empty, with files, failed earlier) is never reused, adopted or deleted; **18 injected faults** at write, flush, marker, removal and rename boundaries (including a no-space error on a later chunk, a failing `RUNNING` removal and an `EXDEV` rename, which must be attempted exactly once with no fallback), each of which cannot report success and leaves exactly the owned, `FAILED`, unadopted partial; staged corruption, an extra staged file and bytes corrupted on their way to disk are caught by validation before publication; a post-rename failure, an unclassifiable rename and a parent-flush failure are reported as possibly or certainly published with nothing deleted; repeated attempts after success and after failure have explicit refusals; the `FAILED` record is fixed-shape, bounded and free of a planted credential and stack trace, and a failure to record it never hides the original; durability modes behave as documented, the real platform behavior matches the documented support, and the default operations do not swallow a real `fsync` error; module constraints (standard library plus contract only, no copy or move fallback, no replacing rename, no recursive delete, no restated schema or normalization rules, no work at import) are asserted by AST tests.

**Verified.** Exact command `python -m pytest tests/regression/test_deployment_backup_artifact_writer.py tests/regression/test_deployment_backup_artifact.py -q`: Windows (Python 3.13.3, pytest 9.1.1) **590 passed, 11 skipped** (601 collected); Linux (Python 3.13.16, real symlinks and real directory flushes, in a disposable `--network none` container built from the project's own local image, repository mounted read-only, `pytest` and its pure-Python dependencies copied from this machine's site-packages into a read-only scratch mount; nothing pulled, installed or changed) **595 passed, 2 skipped** (597 collected). The writer module alone: Windows 85 passed, 4 skipped; Linux 88 passed, 1 skipped. Clean worktree of `373b35452bb47f71d5f85cbca1e0624a9e4139f3` plus only the intended uncommitted files: Windows default-suite selection (writer, artifact module, comparator unit tests, Alembic DSN regression and the four existing deployment test files) **752 passed, 11 skipped, 0 failed** (JUnit 763); Linux (writer, artifact module, comparator, DSN regression) **643 passed, 2 skipped** (JUnit 645); live adapter check **1 passed**. **Skips, exactly:** Windows -- one POSIX permission-mode test, one POSIX-only non-private-staging test, seven file-level symlink tests (this user cannot create symlinks), one POSIX-only publication-order test, one POSIX-only `lstat` trailing-separator test; Linux -- one Windows-only statement test and one NTFS-junction test. Container runs execute as root, so a permission-based read-only check there is not evidence of enforcement. The read-side suite was **505 passed, 7 skipped** on Windows and 507 / 1 on Linux both before and after the behavior-preserving split; those earlier counts are historical to their trees. **Mutation check of the writer** (11 deliberate defects: `RUNNING` not removed, `os.replace` instead of `os.rename`, no exclusive reservation, staged validation skipped, the manifest-as-written check removed, file-flush errors swallowed, no `FAILED` record, no parent flush, `COMPLETE` written before the `RUNNING` removal, a post-rename failure treated as unpublished, the source-changed check removed): **11 of 11 caught on Linux; 10 of 11 on Windows**, the eleventh (no exclusive reservation) being a POSIX-only code path that does not execute on Windows. A first run left three mutants alive (the reservation on Windows, the manifest-as-written check, and swallowed real `fsync` errors, because fault injection replaced the operation being mutated); three tests were added for those, and a refinement was needed so the file-flush test could not be satisfied by an unrelated directory-flush failure on Linux. **One observation, reported as found:** a single early run showed a directory-entry modification-time mismatch in a whole-tree snapshot on NTFS and never recurred in about ten later runs; the snapshot helper now compares names and contents only, and collision tests additionally assert that no mutating operation was attempted. The cause was a test-helper fragility, not a defect found in the writer, but it was not reproduced, so it is not proven.

**Unsupported (stated, not claimed away).** Directory flushes on Windows (the default refuses; the explicit mode reports gaps). POSIX privacy modes on Windows. Any filesystem where directory flush or atomic directory rename is not provided (network and removable filesystems were not tested and are not claimed). Survival of power loss or storage failure (unproven). A cross-filesystem store/archive arrangement (refused, no fallback). Writers that do not cooperate with this module. Adoption, resumption, repair or automatic cleanup of any partial, and of the empty reservation a crash can leave. Checking the archive's table-of-contents fields. Authenticity (no signature or MAC). **Deferred, unchanged:** database capture, `pg_dump`, snapshots, the section 7 validation plan (**not executed, and required before any capture code**), the sequence rule, scheduling, retention, external storage, restore-verification records and the five decisions for Anthony (section 11). **Manifests:** reviewed by the established convention; the writer's 22 public top-level symbols have no bare-name collision with either manifest, so no entry changed (1,156 data rows; 1,157 physical lines including the header).

### 12.2 Phase 9B-5B correction: descriptor closure (2026-10-02)

**Two defects were reproduced against the first 9B-5B delivery (synthetic data, injected errors, temporary directories).** (1) `write_artifact()` closed the source descriptor in an unguarded outer `finally`. An `EIO` on that close after otherwise successful processing raised a plain `OSError`: no stage, no `published` attribute, **although the final artifact existed and the public verifier accepted it** (`STRUCTURALLY_VALID`). Publication status was lost, and the raw exception message escaped unsanitized. (2) `_create_file()` closed its descriptor in an unguarded `finally`. An archive `fsync` failure followed by a close failure replaced the flush failure: the cause chain no longer reached the flush error and `WriteFailed` and the `FAILED` record carried the **close** error's classification (`internal_error`, or `storage_full` when the close errno was `ENOSPC`, as in the reproduction) instead of `durability_failed`. **Audit finding:** `FileSystemOps.fsync_directory()` had the same pattern. A close failure replaced a `DurabilityUnsupported` (turning "unsupported" into a hard error, or hiding it) and replaced a real flush failure (reproduced on Linux with real directory descriptors).

**Corrected behavior.** (a) Every descriptor the writer opens -- the source archive, each staged file (including `RUNNING` and `FAILED`), each directory opened for a flush -- receives **exactly one close attempt**, and an uncertain close is **never retried**. (b) The source is closed as soon as its copy has been verified, **before** anything is published, so after the rename no descriptor except the parent directory's flush descriptor is open and publication status can no longer be lost to a source close. (c) A close that fails **with no earlier failure** is a failure, never success: a written file's close is a `durability_failed` `WriteFailed` in that file's own stage (`staging` for `RUNNING`, `archive`, `baseline`, `manifest`, `marker`); the source's close is an `internal_error` `WriteFailed` in the `archive` stage; a directory close after a *successful* flush is an error in both durability modes (a `PostPublicationError` with `published="yes"` after the rename, a `WriteFailed` before it). (d) A close that fails **while another failure is propagating never replaces it**: the primary keeps its type, stage, reason class and causal chain, and the secondary is attached as a sanitized note and in the new `cleanup_failures` tuple on the writer error. The text is fixed-format -- what was being closed, the exception type and its errno, **never an exception message, path, credential or source content**. (e) `KeyboardInterrupt` and `SystemExit` stay the propagating exception, are recorded as `interrupted`, and carry the note; an interrupt raised by a close itself is not swallowed. (f) A post-publication failure keeps `PostPublicationError` with the correct `published` status, carries the directory-close note, and the published directory is not touched. (g) The directory flush distinguishes unsupported from failed as before, and a close note on an *unsupported* flush is reported with the gap in `BEST_EFFORT_REPORTED` (REQUIRED still refuses as unsupported). (h) `FAILED` remains best effort: a failing close of the `FAILED` record is reported as `failed_record="failed: ..."` and cannot replace the primary failure (the record may nevertheless exist, so this report is conservative). The seam was extended, not replaced: `FileSystemOps` gained `open_directory` and `fsync_directory_descriptor` (the production flush-and-close for a descriptor, which tests drive on every platform); no cleanup framework was added.

**Tested.** 31 tests were added to `tests/regression/test_deployment_backup_artifact_writer.py` (120 collected in the module). The test seam now tracks descriptor ownership: every opened descriptor must be closed exactly once, a close of a descriptor that is not open (a retry or a double close) is recorded, and an injected close error is raised only **after** the descriptor is really released, as after a failing `close(2)`. Cases: source close alone; close alone on each of the five staged files; primary write or flush failure plus the file's close failure (four combinations, asserting stage, reason class, **identity of the cause**, the exact sanitized note, the `FAILED` record and the counts of close attempts); both the file's and the `FAILED` record's closes failing; source close failing after an archive write failure, a source read failure, and a source changed during the copy; a `PreconditionError` raised after the source was opened; `KeyboardInterrupt` and `SystemExit` each with a file close and with a source close failing; an interrupt raised by a close itself; post-publication failures with the parent directory's flush failing or succeeding and its close failing (final contents compared byte for byte with a control run and re-checked with the public verifier); an unsupported flush with a failing close in `BEST_EFFORT_REPORTED` and in `REQUIRED`; five flush-and-close combinations at the production descriptor method on every platform; and, on POSIX only, the real `fsync_directory` over real descriptors with `os.close` failing. Planted credential-like text in the injected close errors must appear in no error, note, cleanup record, gap or `FAILED` record. **Run against the unmodified writer** the module fails 28 tests on Windows (86 passed, 6 skipped) and 30 on Linux (89 passed, 1 skipped); the Linux run includes the two real-descriptor directory tests, which confirm the audit finding. The failures at the file and source level are the defects themselves (a plain `OSError`, a wrong class, a lost cause); the directory-descriptor tests on Windows fail only because the new seam method does not exist there.

**Verified.** Exact command `python -m pytest tests/regression/test_deployment_backup_artifact_writer.py tests/regression/test_deployment_backup_artifact.py -q -ra`: Windows (Python 3.13.3, pytest 9.1.1) **619 passed, 13 skipped** (632 collected, JUnit 632, 0 failures); Linux (Python 3.13.16, real symlinks and real directory flushes, disposable `--network none` container from the project's own local image, repository mounted read-only, `pytest` copied from this machine's site-packages into a read-only scratch mount) **626 passed, 2 skipped** (628 collected). The same selection in a clean worktree of `373b35452bb47f71d5f85cbca1e0624a9e4139f3` containing only the six files it needs (`src/deployment/backup_artifact.py` and `backup_artifact_writer.py`, `tests/postgres/baseline_document.py` and `pg_restore_compare.py`, and the two regression modules): Windows 619 passed, 13 skipped; Linux 626 passed, 2 skipped. **Skips, exactly:** Windows -- 1 POSIX permission-mode test, 1 POSIX-only non-private-staging test, 7 file-level symlink tests (this user cannot create symlinks), 1 POSIX-only publication-order test, 2 POSIX-only real-directory-descriptor tests, 1 POSIX-only `lstat` test; Linux -- 1 Windows-only statement test and 1 NTFS-junction test. The Linux container runs as root, so a permission-based check there is not evidence of enforcement. **Mutation check of the new code** (12 deliberate defects: a written file's close failure swallowed; a failing close allowed to replace the primary; the source closed again on the failure path; the source never closed on the failure path; the source not closed on the success path; its success-path close failure swallowed; a directory close failure after a successful flush swallowed; a directory descriptor not closed after a failed flush; cleanup notes dropped from `PostPublicationError`; an interrupt raised by a close swallowed; the note carrying the exception message; the gap dropping the close note): **12 of 12 caught on Windows and on Linux**, with the file restored byte for byte after each. PostgreSQL batteries, the comparator and Alembic tests, the live adapter and the unresolved identity test were deliberately **not** re-run. The earlier counts (590 / 11 Windows, 595 / 2 Linux, and every earlier figure in this document) are historical to their trees.

**Limitations (stated, not claimed away).** A second interrupt raised by a close during cleanup propagates and, because it escapes the handler, the `FAILED` record may not be written; the partial then keeps its `RUNNING` marker, stays invalid and is never adopted. Injected close failures prove how the writer handles a reported failure; they do not prove how any particular filesystem behaves when `close(2)` fails (on Linux the descriptor is released regardless, which is why no retry is made). On Windows directory flushes remain unsupported, so the directory-close paths there are exercised only through the descriptor seam over a regular file. The writer still establishes claim 1 and a structural check only. **Unchanged and deferred:** capture consistency remains **unvalidated** (the section 7 plan is not executed and is required before any capture code), the sequence rule, scheduling, retention, storage and the decisions in section 11. Overall Phase 9B remains incomplete. **Manifests:** reviewed by the established convention; the writer's 22 public top-level symbols are unchanged (the new helpers are private; the new `FileSystemOps` members are methods, not top-level symbols), so no entry changed (1,156 data rows; 1,157 physical lines including the header).

### 12.3 Phase 9B-5C1: the first execution of the section 7 plan -- cases C1, C2 and C8 only (2026-10-02)

**Scope.** Test-layer validation against a disposable, synthetic PostgreSQL 16 (`postgres:16-alpine`, one cluster, verified TLS and SCRAM, private pgpass files inside the container, the unchanged 9B-2 backup-role grants). **No production capture code was written** and the artifact schemas were not touched. Only table-data behavior was examined. **Cases C3, C4, C5, C6, C7, C9 and C10 were not executed and remain unvalidated.** Claim 3 may still not be asserted for any real artifact: C1, C2 and C8 are three of the ten cases the plan requires, and passing them is not proof of capture safety.

**What was found about the existing helper (before any case ran).** `capture_baseline(Engine)` does **not** preserve one transaction: its helpers `scalar()` and `rows()` called `engine.connect()` for every read, so a baseline read through an Engine is dozens of separate connections and transactions that share no snapshot. The shared helpers in `tests/postgres/pg_restore_compare.py` therefore now accept an open `Connection` as well. A `Connection` is used as is (never opened, committed, rolled back or closed by the helpers; its transaction belongs to the caller); an `Engine` behaves exactly as before, so every existing caller and the output shape are unchanged, and the fingerprint SQL and comparison semantics were not duplicated or altered. Two default-suite unit tests pin the ownership rule (an Engine uses one new connection per read and cannot see another connection's temporary table; a Connection is used as is, is never closed, and keeps its caller's transaction). In the cases, the owner's engine is asserted to open **no further connection** during the capture, and the owner's re-read after the commit equals the baseline while a fresh transaction differs.

**What each case did and what it proves.**
- **C1 (shared snapshot) -- passed.** The owner (backup role, `BEGIN ISOLATION LEVEL REPEATABLE READ, READ ONLY`, verified from the server's own settings) exported its snapshot and read the baseline through its own connection. A separate writer (the migrator, released by a threading event with a timeout) then committed a known change -- one `INSERT` into `teams` and one `UPDATE` of `conferences` -- before `pg_dump` started; a fresh transaction saw exactly that change (teams 3 to 4 rows; conferences unchanged in count, changed in content). A real `pg_dump -Fc --snapshot=<exported id>` ran as the backup role over verified TLS (one single-process session, empty stderr, exit 0) while the owner stayed `idle in transaction`; the owner's `pg_current_snapshot()` descriptor was identical at export, after the commit and after the dump. The archive was validated by the existing archive-ready guard, restored into a fresh empty database, and the restored table fingerprints equalled the baseline exactly for every table, with the post-snapshot change absent. **Proves, for table data in this configuration:** a dump that imports the owner's exported snapshot reproduces the owner's baseline even though another session committed rows after the export and before the dump.
- **C2 (negative control) -- passed, with the expected mismatch.** Same data shape, same baseline/change/commit ordering, same command line except that `--snapshot` is absent (asserted). The restored data differed from the baseline in exactly `teams` (3 to 4 rows) and `conferences` (same count, different content), contained the committed change, and equalled the fresh committed state exactly. The case fails if the writer's change never committed (a precondition, and a second guard through the fresh read). **Proves:** C1's equality is sensitive to a dump using the wrong snapshot; it is not an artifact of the data or the comparison.
- **C8 (owner loss before import) -- passed.** The owner exported its snapshot and read the baseline, was then ended from the server side and **confirmed absent** from `pg_stat_activity` before `pg_dump` started. `pg_dump` exited 1 with PostgreSQL's `invalid snapshot identifier` error on `SET TRANSACTION SNAPSHOT`; the output contains none of the authentication, connectivity or permission fragments, the server logged the session as SCRAM-authenticated TLS under the backup role (so the failure came after authentication), and a one-variable control (same command, role and flags with a snapshot whose owner was alive) exited 0. The harness recorded **`snapshot_unavailable`** (one of the specification's enumerated classes) and nothing downstream ran: no destination database was created, no archive validation, no restore, no finalization, and the would-be artifact directory stayed empty (no `COMPLETE`). `pg_dump` left an incomplete file behind; the same archive gate that guards restores refused it. **Proves:** an unavailable snapshot is a loud, classifiable failure and the downstream steps are gated on it.

**Discrepancies investigated.** (1) The first full run had two failures, both in cross-case assertions the plan did not require: the command lines legitimately differ in the case database name, and the md5 fingerprints of two separately seeded databases differ because the seed stores server-generated `created_at` timestamps that the fingerprint covers. Each case is deterministic **within** its own database, which is all C1 and C2 compare; the assertions were corrected (database name normalized; row counts compared across databases) and **no expected result of C1, C2 or C8 was changed**. (2) The harness mutation check found one vacuous assertion in the first version of the C8 test (it read a field that C8 never sets, and the mutant that skipped the server-side termination still ended the owner through a client-side disconnect); the test now records the owner's server-side state before and after the loss and requires both.

**Verified.** Command `python -m pytest tests/postgres/test_postgres_backup_snapshot_consistency.py -q -m postgres -ra` (Windows host, Python 3.13.3, pytest 9.1.1, Docker 29.7.2 with the local `postgres:16-alpine` image, nothing pulled): **42 passed, 0 skipped**, about 23 s, exit code 0, in the working tree and in a clean worktree of `373b35452bb47f71d5f85cbca1e0624a9e4139f3` plus only the files it needs. The shared helper changed, so `tests/regression/test_pg_restore_compare_unit.py` was also run (**30 passed**; 28 before, plus two ownership tests) and, as the callers that exercise the unchanged Engine path, the live adapter check (**1 passed**), `tests/postgres/test_postgres_roles_and_restore.py` (**33 passed**) and the 9B-4 rehearsal's baseline-versus-restored comparison test (**1 passed**). The full PostgreSQL battery, the 9B-2 TLS module, the rest of the 9B-4 module and the unresolved identity test were deliberately not re-run. **Mutation check of the harness** (eight deliberate defects: `pg_dump` never imports the snapshot; the visibility descriptor passed instead of the exported id; a writer that never really commits; an owner never ended in C8; capture through the Engine instead of the owner's connection; the owner ending before the dump; a classifier that ignores authentication and permission text; cleanup that does not remove container files): **8 of 8 caught**, with the file restored byte for byte after each. Cleanup was verified on every case (databases dropped, no session in them, no leftover `pg_dump`/`pg_restore`/owner session, container files removed, writer thread joined, directories removed), on a deliberate mid-case failure, and a deliberately leaking cleanup is reported without replacing the original failure. The cluster container was removed and verified; the existing Docker volume `bonnieswebscraping_napsedge-local-db-data` was not touched. No credential appears in any recorded command line or output (checked in the tests and by a scan of the retained evidence).

**Limits (stated, not claimed away).** Table data only: no sequence, catalog, ownership/ACL, DDL, lock, overlap or quiescent-mode behavior was examined, and no claim 3 or claim 4 statement follows. One writer committing a small known change was used, not the plan's two writers with thousands of inserts, so behavior under sustained concurrent write load is **not** shown. One PostgreSQL version and configuration (16, default settings), one single-process dump (no `-j`), the owner and the importer being the same role, and a restore target in the same cluster. Passing these cases says nothing about unmanaged changes, a change made and reverted inside a window, or other versions and settings. Production capture is **not implemented**; the capture procedure of section 6 remains a design.

### 12.4 Phase 9B-5C2: sequence validation -- cases C3, C4 and C5 only (2026-10-04)

**Scope.** Test-layer validation against a disposable, synthetic PostgreSQL 16 (one cluster, verified TLS and SCRAM, the unchanged 9B-2 backup-role grants, the C1 harness reused by import). **No production capture code and no production sequence verifier were written**; the rule functions (`next_value`, R0 to R3) are test-layer verification code, and the artifact schemas, reader, writer and the strict comparator `compare_to_baseline` are unchanged. Only the behavior of the 13 documented sequences in these cases was examined. **C6, C7, C9 and C10 were not executed and remain unvalidated** (section 12.3's statement that C3 to C5 were unexecuted is superseded for those three cases only). The `monotonic_v1` rule is still a **proposal**: these cases show its behavior in the configurations below and nothing more. Claim 3 may still not be asserted for any real artifact.

**Capture contract as exercised.** The owner (backup role, `REPEATABLE READ, READ ONLY`) reads, in this order and all through its own connection (the engine opened no further connection), the baseline table data (`capture_baseline(Connection)`), then the sequences: the inventory and each sequence's owning table and column from the catalog (`pg_class`, `pg_sequence`, `pg_depend`), its parameters, its state **directly** (`SELECT last_value, is_called FROM public.<seq>`) and the owning column's maximum, held as the production contract's `SequenceDetail` (the one representation; the baseline block round-trips through `parse_baseline`). `pg_sequences.last_value`, which is NULL for a not-yet-called sequence, is kept separately in the strict baseline exactly as before and is never replaced by zero or used to infer `is_called`. All of this runs **before** `pg_dump` starts. The next value is computed without drawing from any sequence: `last_value` when `is_called` is false, else `last_value + increment`, only inside the tested domain (R0). The owning column's maximum for R2 is measured on the **restored** database. A static test fails if any function other than the explicit writer and setup actions (`advance_action`, the two `ALTER SEQUENCE` setups) names `nextval`, `setval`, `ALTER SEQUENCE` or `RESTART IDENTITY`, and a second read of the restored sequences after all verification equals the first: verification moved nothing.

**What each case did and what it proves** (values from the retained run).
- **C3 (advancement between the baseline and the dump) -- passed.** After the baseline sequence read and before `pg_dump`, a bounded writer (a threading event with a timeout; the migrator) drew 5 values from `team_aliases_id_seq` (baseline `last_value=3, is_called=true`, N_b=4) and 1 from `data_quality_issues_id_seq` (baseline `last_value=1, is_called=false`, N_b=1, so the first draw returned 1). Advancement was **required and confirmed** from a fresh transaction before the dump may start (a writer that draws nothing cannot satisfy the case: tested by mutation). The owner's own re-read showed the advanced sequences (sequences are not snapshot-bound) while its table fingerprints still equalled the baseline (data is). `pg_dump --snapshot=<exported id>` ran with the owner open; the restored table-data fingerprints equalled the baseline **exactly** for every table; the restored sequences were `last_value=8, is_called=true` (N_r=9, **N_r > N_b**) and `last_value=1, is_called=true` (N_r=2); every other sequence was unchanged. R0, R1, R2 and R3 held for all 13 sequences from observed values. The **unchanged strict comparator** reported the sequence difference exactly as expected and only for the two advanced sequences (`team_aliases_id_seq` 3 to 8; `data_quality_issues_id_seq` NULL to 1), and that difference was neither suppressed nor reinterpreted. A **no-advancement control** (same path, no writer) gave equal next values, no strict difference and equal data; it also exercises the nine live uncalled sequences (`is_called=false`, next value 1, before and after) against the four the seed has drawn from. A **bounded rollback example** inserted a row and rolled it back: the table was unchanged (3 rows) but `team_aliases_id_seq` advanced 3 to 4 and stayed advanced; after dump and restore the next value was 5 with the column maximum 3, a gap, and R1, R2 and R3 all held (a gap is not corruption). **Proves:** a sequence advanced after the baseline read is carried into the archive (N_r > N_b) while table data stays at the snapshot, so `equal_v1` cannot apply under concurrent capture, and the proposed rule R0 to R3 evaluates as designed from observed values.
- **C4 (manual-id hazard) -- passed.** Before capture a valid `team_aliases` row with explicit id 1,000,000 was inserted; the sequence did not move (`last_value=3, is_called=true` before and after). Capture, dump and restore ran through the same guarded path: table data was in exact parity, **R1 held** (N_b = N_r = 4), and **R2 failed** on the restored column maximum (1,000,000 against N_r = 4) for that one sequence and no other. The unsafe condition is reported and **nothing was reset or repaired** (the restored sequence is exactly as dumped; the offending row is still there). **Proves:** R1 alone is insufficient and R2 detects the condition from the restored database. It does **not** claim the immediately next insert must collide: the restored generator is not safely beyond the existing ids, which is all R2 asserts.
- **C5 (applicability) -- passed.** In separate disposable databases `team_aliases_id_seq` was altered to `CYCLE` and to `INCREMENT BY -1` before capture. In the actual harness path the applicability guard read the owner's baseline and **refused** each (`cycles`; `increment_not_positive`, only that sequence), recorded `monotonic_not_applicable`, and nothing downstream ran: no `pg_dump`, no destination database, no restore, no command at all after the setup, an empty would-be artifact directory; no next value or rule outcome was computed for the rejected configuration (`next_value` raises). The ordinary positive, non-cycling, owned control was accepted and its dump proceeded. R3 (headroom) is **reported separately and informationally**: no aggregate verdict is computed and no harness step reacts to it, so no new policy was introduced. The `equal_v1`/quiescent alternative the rule names for a rejected configuration was not exercised (that belongs to C10).

**Discrepancies investigated.** The first full run had 67 passes and one failure: my own expectation that at least ten sequences are uncalled in a freshly seeded database. The observed value is nine, which is exactly 13 minus the four sequences the deterministic seed draws from (`team_aliases`, `player_aliases`, `source_artifacts`, `player_stat_snapshots`); the assertion now states the exact called and uncalled sets. No expected result of C3, C4 or C5 was changed. In the sensitivity run one mutant finished in 1.9 s with setup errors (a transient immediately after the previous mutant's cluster was torn down); it was rerun alone and was killed for the right reason.

**Verified.** Command `python -m pytest tests/postgres/test_postgres_backup_sequence_consistency.py -q -m postgres -ra` (Windows host, Python 3.13.3, pytest 9.1.1, Docker 29.7.2 with the local `postgres:16-alpine` image, nothing pulled; Docker Desktop had to be started by the user at the start of this unit): **68 passed, 0 skipped, 0 warnings**, about 38 s, exit code 0, in the working tree and in a clean worktree of `373b35452bb47f71d5f85cbca1e0624a9e4139f3` plus only the files it needs. The shared C1 harness changed (a pluggable writer action, and the workspace-preparation and restore blocks extracted as functions that `run_case` now calls, behavior-preserving), so `tests/postgres/test_postgres_backup_snapshot_consistency.py` was rerun: **42 passed, 0 skipped**, in both. `pg_restore_compare.py` did not change in this unit, so its unit tests and the older integration checks were not re-run. **Sensitivity check** (nine deliberate defects, file restored byte for byte after each): a writer that draws nothing; `is_called` ignored; R2 omitted; CYCLE ignored by R0; a negative increment ignored by R0; the applicability guard never called; R1 inverted; R2 measured on the baseline column maximum; verification that draws from a sequence -- **9 of 9 caught**. Cleanup (databases dropped, no session in them, no leftover `pg_dump`, `pg_restore` or owner session, container files removed, writer thread joined, directories removed) was verified for every scenario; the cluster container was removed; the existing Docker volume `bonnieswebscraping_napsedge-local-db-data` was not touched; no credential appears in the retained evidence.

**Limits (stated, not claimed away).** Default sequence parameters only (`bigint`, increment 1, cache 1; a cache above 1, other types and non-default increments were not exercised except the rejected ones). One bounded writer drawing six values: behavior under sustained concurrent draws is **not** shown. One PostgreSQL version and configuration, a single-process dump, the same role as owner and importer, a restore target in the same cluster. No server crash, `setval`, restart or `TRUNCATE ... RESTART IDENTITY` behavior was examined (the static guard forbids them in verification). Nothing here validates catalog consistency, DDL exclusion, locks, overlap handling or quiescent capture (C6, C7, C9, C10), restore verification (claim 4), or any production code. The `monotonic_v1` rule and the concurrent-sequence policy therefore remain **proposed**.

### 12.5 Phase 9B-5C3: catalog observations and locking -- cases C6 and C7 only (2026-10-04)

**Result in one line.** The catalog bracket of section 6.4 **as written is NOT met**: implemented in the test layer exactly as proposed, it **missed every committed catalog change** (an index change, an ACL change and an owner change were each proven committed and visible to a fresh observer). The blind spot is retained as a characterization test and the safety requirement as seven strict-xfail tests. Two test-layer alternatives detect the same changes, are labeled **alternatives that need later design review**, and are not adopted. The lock results (C7) are recorded without selecting a production lock policy. **C9 and C10 remain unvalidated**, no production capture or production lock policy exists, and Claim 3 may still not be asserted for any real artifact.

**Scope and method.** Test layer only, one disposable synthetic PostgreSQL 16 cluster (verified TLS and SCRAM, the unchanged 9B-2 backup-role grants, the C1 harness reused by import, no shared harness code changed). The observation is the existing `constraint_snapshot`, `index_snapshot` and `acl_snapshot` read through a connection (no catalog normalization was duplicated, the strict comparator was not altered), digested **separately** per category (constraints, indexes, ACL/ownership) and combined. Each scenario runs the real flow (owner export, baseline data reads **on the owner connection**, `pg_dump --snapshot`, restore, strict comparison, a finalization stand-in that writes nothing); the server's own `pg_locks` shows the owner backend holding the baseline reads' locks, so the data reads demonstrably stayed on the owner. Changes are made by a separate session with bounded lock and statement timeouts, each step in its own transaction, then **proven committed and visible from a fresh committed observer**; a lock timeout (SQLSTATE 55P03) is reported as **blocked** and never counts as detection.

**The proposal as written, and what it does.** The owner observes the three categories before the snapshot export (its own session, outside any data transaction) and again after `pg_dump` exits while it is still inside its open `REPEATABLE READ` transaction (the order of section 6.2). Observed: for **every committed change, in both windows, the digests were identical at the two endpoints** (all three categories), so no `catalog_changed` was raised and the flow proceeded to restore and the finalization stand-in. The cause is snapshot blindness, not absence of the change: the catalog scans the owner repeats are bound to its snapshot. The same owner session, **after its transaction has ended**, detected all of them, as did a fresh committed transaction. Equality at two endpoints also cannot show that nothing happened in between: a change and its exact reversal (an index created and dropped, both committed and each seen by a fresh observer) was undetected by **every** variant.

| Change | Window | Result | Visible to a fresh observer | As written (owner, in its transaction) | Owner after its transaction ended | Candidate (fresh committed transactions) |
|---|---|---|---|---|---|---|
| `CREATE INDEX` on `teams` | after export, before baseline reads | committed | indexes | **missed** | detected | detected |
| `GRANT INSERT` on `conferences` | after export | committed | acls | **missed** | detected | detected |
| `ALTER TABLE ... OWNER TO` (superuser; a disposable NOLOGIN role) | after export | committed (the owner holds no table lock yet) | acls | **missed** | detected | detected |
| `CREATE INDEX` | after the baseline reads | committed (SHARE conflicts only with writers) | indexes | **missed** | detected | detected |
| `GRANT INSERT` | after the baseline reads | committed | acls | **missed** | detected | detected |
| `ALTER TABLE ... OWNER TO` | after the baseline reads | **blocked** (55P03 after the 5 s test timeout, behind the owner's ACCESS SHARE) | none | not applicable | not applicable | not applicable |
| `ALTER TABLE ... OWNER TO` with explicit ACCESS SHARE locks taken before the export | after export | **blocked** (55P03) | none | not applicable | not applicable | not applicable |
| `CREATE INDEX` / `GRANT INSERT` with explicit locks taken before the export | after export | committed (ACCESS SHARE does not prevent them) | indexes / acls | **missed** | detected | detected |
| index created then dropped | after export | both steps committed; the fresh observer saw it appear then disappear | none at the end | undetected | undetected | undetected |
| no change (control) | -- | -- | none | equal | equal | equal |

For the committed changes the strict comparator found the baseline and the restored database **equal** (fingerprints, indexes, constraints, ACLs, owners): both were snapshot-bound, so these particular missed changes produced no baseline-versus-archive difference. That is an observation, **not** a safety claim; the bracket's stated purpose, to detect a concurrent migration, grant, revoke or owner change that overlapped the capture, is unmet.

**The candidates (alternatives requiring later design review; neither is adopted).** (1) *Fresh endpoints:* each endpoint is observed through a fresh committed transaction (a separate backup-role connection) while the data-snapshot owner stays open and unchanged (still `idle in transaction`, its visibility descriptor unchanged, its reads untouched). It detected every committed change. Its `catalog_changed` refusal was exercised **in the real harness flow**: the flow stopped before archive validation, restore and the finalization stand-in, no destination database was created and the would-be artifact directory stayed empty; its no-change control proceeded to completion. (2) *Owner after its transaction ended:* it also detected every change, but it ends the owner's transaction before the second observation, which changes the lifetime rule of section 6.2 and so needs the same review. Neither candidate detects a change and its reversal.

**C7 -- locks and availability** (values from the retained run; no policy is selected).
- The backup role takes `ACCESS SHARE` on all 24 application tables with its **existing** `SELECT` grant; `ACCESS EXCLUSIVE` and `ROW EXCLUSIVE` are denied (42501); its table privileges (`SELECT` only) are identical before and after. Nothing was granted to it.
- Ordinary baseline reads hold `AccessShareLock` on all 24 tables, but **only after the reads**: right after the export the owner holds none. The explicit variant holds the same 24 locks **from before the export**. It changes *when* the locks are held, not *which*. That timing is why an owner change committed in the window after the export under ordinary reads and blocked under the explicit variant. ACCESS SHARE does **not** protect against every catalog or ACL modification: index and ACL changes committed under both.
- A migrator `ALTER TABLE public.teams ADD COLUMN` (a genuine `ACCESS EXCLUSIVE` request, `lock_timeout` 10 s, a test value) was confirmed **queued** from the server's own views: an ungranted `AccessExclusiveLock` on `teams`, `pg_blocking_pids` naming the owner backend, `pg_stat_activity` showing `wait_event_type = Lock`, `wait_event = relation`. It timed out with 55P03 after 10.01 s and **did not commit** (the column is absent).
- After the request was confirmed queued, a fresh ordinary reader (the runtime role, `SELECT count(*) FROM teams`) was observed **waiting behind the queued request**: an ungranted `AccessShareLock`, blocked by the DDL backend and **not** by the owner (whose lock is compatible), `wait_event_type = Lock`. It completed (3 rows) after the DDL timed out and rolled back, **while the owner stayed open** (still `idle in transaction`, descriptor unchanged). It had waited **9.83 s**; the control with no queued request finished in **0.0 s** with no ungranted lock seen at any poll. **Availability cost:** a queued exclusive request can stall ordinary reads for up to the migrator's `lock_timeout`; this specification does not choose that timeout or any lock policy (decision 11 remains open).

**Verified.** Command `python -m pytest tests/postgres/test_postgres_backup_catalog_consistency.py -q -m postgres -ra` (Windows host, Python 3.13.3, pytest 9.1.1, Docker 29.7.2, the local `postgres:16-alpine` image, nothing pulled): **120 passed, 7 xfailed, 0 skipped, 0 warnings**, about 2 minutes, exit code 0, in the working tree and in a clean worktree of `373b35452bb47f71d5f85cbca1e0624a9e4139f3` plus only the files it needs. The 7 xfailed are the **strict** safety-requirement tests (one per committed change the bracket missed): they are expected failures that would turn into failures if the bracket began to detect. No shared harness file changed (the snapshot and sequence modules and `pg_restore_compare.py` are byte-identical to their previous state), so those modules were not rerun. **Sensitivity check** (ten deliberate defects, file restored byte for byte after each): the as-written endpoint read through a fresh transaction; the ACL/ownership digest ignored; a lock timeout not recognized as blocked; a blocked change counted as committed; `catalog_changed` never firing; the flow continuing after a refusal; the baseline read through another connection; explicit locks never taken; the reversal step not reversing; a lock timeout far shorter than the observation window -- **10 of 10 caught**. Cleanup (databases dropped, no session in them, no leftover `pg_dump`, `pg_restore` or owner session, container files and directories removed, threads joined, the probe role dropped) was verified for every scenario and the cluster container removed; the existing Docker volume `bonnieswebscraping_napsedge-local-db-data` was not touched; no credential appears in the retained evidence.

**Limits (stated, not claimed away).** One table per change (`teams` for the index, `conferences` for the ACL and owner); one PostgreSQL version and default configuration; one catalog change at a time, applied before `pg_dump` starts (changes during the dump itself were not exercised); no constraint-change scenario (the constraints digests stayed equal throughout, but that proves nothing about constraint changes, which take `ACCESS EXCLUSIVE`); no role rename or drop (the name-resolution case of section 6.3 (b)); the lock timeouts are test values; the queued-request observation was made on one table. The two candidates and their ordering rules are unreviewed. The bracket proposal as written is therefore **failed for its stated purpose**, section 6.4 remains a **proposal**, and a redesign needs Anthony's review before any capture code. Overall Phase 9B remains incomplete.

### 12.6 Phase 9B-5C4: overlap and quiescence -- cases C9 and C10 only (2026-10-05)

**Result in one line.** C9 passed: the existing advisory lock refuses a second backup-role session immediately (`SKIPPED:overlap`), is released on every tested path, and also excludes controlled imports (recorded, not chosen). C10 characterized quiescence and **its safety requirement is not met**: endpoint session counts missed a short-lived non-cooperating writer entirely and a positive count proved presence, not a write, so the counts can never establish continuous quiescence. Production capture remains blocked by the unresolved C6 design; this unit selects no production policy and establishes no database consistency for any artifact.

**Scope and method.** Test layer only, one disposable synthetic PostgreSQL 16 cluster (verified TLS and SCRAM, the unchanged 9B-2 backup-role grants, the C1 harness reused by import, no shared file changed). The EXISTING `verify_database_target`, `compute_advisory_lock_key` and `bulk_import_advisory_lock` were reused unchanged; no key logic, lock namespace, grant or production code was added or changed (a static test enforces this).

**C9 -- what was observed** (values from the retained run).
- *Two physical sessions.* Two backup-role sessions with separate Engines and distinct backend PIDs (97 and 98 in the retained run). The key derived from the verified database name decodes in `pg_locks` as `classid` = high 32 bits and `objid` = low 32 bits of the bigint (`objsubid` 1) and is identical from both sessions.
- *Refusal.* While the first session held the lock the second was refused in **0.005 s** with the recorded job outcome **`SKIPPED:overlap`** (a harness outcome, not a pytest skip) and never ran its body; `pg_locks` showed the lock held by the first session alone. Through the **real capture flow** the refused attempt sent exactly three statements (backend pid, `SELECT current_database(), version()`, the non-blocking lock attempt), opened one connection, issued no command and created no database, and reached no snapshot export, baseline capture, `pg_dump`, restore or finalization. After the holder released, **the same flow** acquired and completed with strict data and sequence equality.
- *Release paths.* Normal release: a fresh session acquires. Exception path: the lock is released and the primary failure propagates unchanged. Physical session loss (`pg_terminate_backend`): the lock disappears with the session (verified from `pg_locks`), a fresh session acquires, and calling the release on the lost session raises `OperationalError`.
- *Primary failures (history; CORRECTED in section 12.7).* **Originally observed:** when the session is lost inside the `with` block and the release then fails, the production context manager let the release error replace the primary failure (an `OperationalError` propagated; the primary was reachable only through the exception chain), and the test harness compensated. **Corrected afterwards:** `bulk_import_advisory_lock` itself now preserves the original failure and records a bounded note (section 12.7); the harness no longer carries a repair policy. The original defect evidence is retained in section 12.7.
- *Pools and rollback.* A session-level lock **survives `rollback()`**. `Connection.close()` returns a **live backend to the pool with the lock still held** (a second session was refused while it sat in the pool). `Engine.dispose()` then ends it. `dispose()` **alone does not close a connection that is still checked out**: the backend and its lock survived until the connection was closed and the pool disposed. Physical-session cleanup was verified from PostgreSQL (all 18 sessions of the lifecycle case gone, no lock rows left), not assumed.
- *Imports.* The reused key **also excludes controlled imports in both directions** (an import-role session was refused while a backup held it, and a backup was refused while the import role held it). That is import exclusion by the same key, distinct from backup-overlap protection; a backup-only versus import-shared policy is **not selected here**.
- *Grants.* The existing grants suffice: the backup role holds `SELECT` only on all 24 tables, can execute the advisory-lock functions (granted to PUBLIC by default), and is a member of none of `pg_monitor`, `pg_read_all_stats`, `pg_write_all_data`. Nothing was granted.

**C10 -- the procedure and what was observed.** The quiescent procedure held the advisory lock across the whole window and used **no exported snapshot anywhere** (plain baseline reads, a plain `pg_dump`, so a difference could not be hidden). The count is defined exactly: sessions of any `backend_type` connected to the CASE database, other than the observer, whose role is one of the declared write-capable roles (the migrator and the cluster superuser), observed AS THE BACKUP ROLE in a **fresh transaction per sample**; it does not count the backup role, the read-only runtime role, any other role or database, or anything the backup role cannot see. Administrative instrumentation never connected to the case database, so it could not distort the counts.
- *A, no writer (control).* Counts 0 and 0; strict data and sequence equality (all 13 sequences, `last_value` and `is_called`); the lock held at every stage.
- *B, short-lived non-cooperating writer.* A migrator session, which never took the lock, committed a sequence-generated insert into `team_aliases` **while the lock was held** (id 4, sequence 3 to 4, proven from NEW backup-role transactions, independent of the writer), then physically disconnected (confirmed from `pg_stat_activity`). A sample taken while it was connected counted it (so the count works), but **the start and end counts were both zero: the write was missed**. The dump contained it: the unchanged strict comparator reported exactly `team_aliases` (3 to 4 rows) and `team_aliases_id_seq` (3 to 4).
- *C, writer retained through the end sample.* Counted at the middle and end samples (the administrator saw the same PID, `idle`); its committed change was separately proven; the same two strict differences.
- *D, connected write-capable session that writes nothing.* Counted at start and end (the administrator saw it `idle`, no transaction id, no open transaction); data and sequences equal: **a positive count established presence, not a write**.
- *What the backup role can see.* For another role's session it sees `pid`, `datname`, `usename` and `application_name`, but `backend_type` and `state` are NULL and `query` is `<insufficient privilege>`. **A first version of the count filtered on `backend_type`, which silently counted zero for every foreign session**; it was corrected to identity columns only and the original defect is retained as a regression (mutation check). Restricted fields are never evidence of inactivity.
- *Fresh observations are required.* `pg_stat_activity` is cached for the life of a transaction: in one open transaction (as a snapshot owner's would be) the count stayed at 1 after a second session connected; after `pg_stat_clear_snapshot()`, or in a fresh transaction, it was 2.
- *Safety requirements, not met (strict xfail).* "Zero endpoint counts imply nothing wrote" fails for B; "a positive count implies a write" fails for D. The no-writer control and the short-lived writer have **identical endpoint evidence (0, 0) and different data**. Characterization success is kept separate from the safety requirement, which is unmet.

**Discrepancies investigated.** (1) The first count returned zero for every foreign session because of the `backend_type` filter (above). (2) The first physical-cleanup check failed because `Engine.dispose()` does not close a checked-out connection (above); sessions are now ended by closing the connection and then disposing the pool, and the behavior is itself recorded as a finding.

**Verified.** Command `python -m pytest tests/postgres/test_postgres_backup_overlap_quiescence.py -q -m postgres -ra` (Windows host, Python 3.13.3, pytest 9.1.1, Docker 29.7.2, local `postgres:16-alpine`, nothing pulled): **53 passed, 2 xfailed, 0 skipped, 0 warnings**, about 42 s, exit code 0, in the working tree and in a clean worktree of `373b35452bb47f71d5f85cbca1e0624a9e4139f3` plus only the files it needs. The 2 xfailed are the strict safety-requirement tests above. No shared file changed (the snapshot, sequence and catalog modules, `pg_restore_compare.py` and `bulk_import_safety.py` are byte-identical), so nothing else was rerun. **Sensitivity check** (seven deliberate defects, file restored byte for byte after each; a mutant counts only when a named test fails with an assertion, never for an infrastructure failure): the guard never taking the lock; the body running although the lock was refused; the second session being the first session; every sample read through one held transaction; a writer that never commits; a session ended by `dispose()` alone; the original `backend_type` count bug -- **7 of 7 caught**. Cleanup (databases, sessions, container files, directories, threads, advisory locks) was verified for every scenario and on a deliberate mid-case failure (an abandoned held lock and a connected writer); the cluster container was removed; the existing Docker volume `bonnieswebscraping_napsedge-local-db-data` was not touched; no credential appears in the retained evidence.

**Limits (stated, not claimed away).** One table and one writer; default PostgreSQL 16 settings; the count covers only the two declared roles (a write-capable session under another role is invisible to it by construction); no DDL, no server crash or network partition, no `pg_read_all_stats` variant (the backup role was not given it); the lock key is per database name and cross-database collisions were not examined; pooling hazards were shown for SQLAlchemy's default pool only. None of this validates a production job, a lock policy or consistency for any artifact.

### 12.7 Phase 9B-5C4 correction: `bulk_import_advisory_lock` preserves the primary failure itself (2026-10-05)

**Why.** Section 12.6 recorded that the production lock manager let a failed release replace a primary failure, and compensated in the test harness. That left the documented contract of `bulk_import_advisory_lock` (the original is never masked by the release) unmet by the production function. This correction restores it **in the function itself**, with a narrowly scoped production-code change, targeted tests and documentation. No production capture, lock namespace, grant, schema or dependency changed.

**The defect, reproduced against the unchanged function before the change** (history, retained).
- *Real backend loss.* The owning backend was terminated inside the protected block and a primary `RuntimeError` raised: the production manager propagated **`sqlalchemy.exc.OperationalError`** from the `pg_advisory_unlock` instead (the SQLAlchemy message even embeds the SQL parameters, including the lock key).
- *Realistic variant.* A SQL error in the protected body leaves the transaction aborted, so the unlock statement cannot run: a body `DataError` (`SELECT 1/0`) was **replaced by `InternalError`**.
- *False unlock result with a primary failure.* The primary survived but nothing recorded that the release had not been confirmed.
- *Regression selection before the change.* Of the 16 new fake-connection tests, 6 failed against the unchanged function (a raised release error replacing the primary, its cause and notes, the bounded note, the false result, no retry, and a primary interrupt replaced by the release error); 3 of the 3 new live tests failed. The existing 33 tests passed throughout.

**The correction.** After a successful acquisition the release is attempted **exactly once** through a single call site. If the protected block succeeded, an unsuccessful release stays a visible failure (a raised error propagates unchanged; a `false` result still raises `AdvisoryLockUnavailableError`). If an exception is already propagating, the **original exception object** propagates with its type, explicit cause, existing notes and traceback; an ordinary exception from the unlock, or a `false` result, is recorded as **one bounded, sanitized note** (fixed text plus the exception TYPE name only: never an exception message, SQL parameter, URL or secret) and the original is re-raised with a bare `raise` (no `from None`, so an existing cause is never erased). A primary `KeyboardInterrupt` or `SystemExit` is preserved the same way; only `Exception` is absorbed during the release, so a **new** interrupt raised during cleanup is never swallowed. A refused acquisition never runs the body and never unlocks. The manager still never commits, rolls back, closes, invalidates or disposes anything; signatures, exports, verification, key derivation, non-blocking refusal and nested acquisition are unchanged. The harness guard `run_guarded` no longer carries any repair policy: it only classifies the refusal.

**Preserving the primary failure is not releasing the lock.** When the release raised or reported `false`, the release is **unconfirmed** and the lock may remain held until its session ends or the caller releases it. Observed live: after a body SQL error aborted the transaction, the primary `DataError` propagated with its note, the lock was **still held** (verified in `pg_locks`), the caller's `rollback()` did **not** free it (a session-level lock survives rollback), a second session was refused, and an explicit `pg_advisory_unlock` by the caller on the same session released it. After a real backend loss the lock was gone with the session and a fresh session acquired; the physical session was ended and verified by the caller. A behavioral consequence for callers: a protected block's own database error now surfaces as itself rather than as the unlock's follow-on error.

**Implemented / tested / verified, separately.** *Implemented:* the change in `src/pipeline/bulk_import_safety.py` (one helper for the single unlock, one for the failure path, two note constants), the simplified `run_guarded`, and the updated C9 experiment and test, which now require the contract **directly from the production manager**. *Tested:* 16 new fake-connection tests (normal body and release; primary plus successful release; primary plus raised release error with cause, notes and traceback; primary plus false result; release-only failures; refusal without body or unlock; no retry; caller ownership; nested balance; unchanged signature, exports and call sites; primary and new interrupts), 3 live tests against a real backend (backend loss through the production manager; an aborted transaction; a real false unlock), and one static test that the harness guard holds no repair policy. *Verified:* `python -m pytest tests/regression/test_bulk_import_safety.py tests/regression/test_bulk_import_coordinator.py -q`: **74 passed** (58 before), and `python -m pytest tests/postgres/test_postgres_bulk_import_safety.py tests/postgres/test_postgres_bulk_import_coordinator.py tests/postgres/test_postgres_backup_overlap_quiescence.py -q -m postgres -ra`: **81 passed, 2 xfailed** (77 passed, 2 xfailed before), in the working tree and in a clean worktree of `373b35452bb47f71d5f85cbca1e0624a9e4139f3` plus only the files it needs; the 2 xfailed are C10's two unchanged expected failures. The four runner regression modules that consume the lock also pass (160 passed). A sensitivity check of the correction (nine deliberate defects: the release error replacing the primary again, the cause erased, a double unlock, a release-only failure swallowed, a new interrupt swallowed, an unlock after a refusal, the exception message recorded, no note for a false result, a rollback of the caller's transaction) was **9 of 9 caught** by expected assertion failures, with the file restored byte for byte. Cleanup, stated to match the actual assertions: **physical-session absence is asserted explicitly only in the backend-loss test** (the owning backend is shown absent from `pg_stat_activity` after the termination and again after the caller disposed its engine, alongside an empty `pg_locks` holder list and a successful fresh acquisition). The aborted-transaction and false-unlock live tests assert lock state from `pg_locks` and dispose their engines in a `finally` block; they do **not** themselves assert that their backends ended, and no separate retained evidence establishes more. The disposable container was removed (0 containers before and after) and the existing Docker volume was not touched. (This corrects an earlier statement that cleanup was verified on every live path; the lock-fix review attachment carried the same overstatement, is kept unchanged as history, and a corrected attachment supersedes it.)

**Limits (stated, not claimed away).** The note records that a release failed; it does not report what PostgreSQL holds. The caller must verify the lock and its session. A release that fails because the transaction is aborted leaves the lock held until the caller releases it or ends the session, and this function deliberately does not roll back. The four PostgreSQL runner modules that also use the lock were not rerun. C6 remains failed as written, C10's endpoint-count limitations and its two expected failures are unchanged, and production capture and Phase 9B remain incomplete; the catalog-observation redesign is the subsequent unit.

### 12.8 Phase 9B-5C5: validation of the fresh catalog-observer candidate (2026-10-05)

**Scope.** Disposable experiments (one PostgreSQL 16 cluster, synthetic data, the existing local image) and documentation. The candidate named in 12.5 -- each endpoint observed by a **separate, fresh observer transaction**, the data-snapshot owner untouched -- was validated against what C6 did not exercise: **constraint changes, committed changes during a real `pg_dump`, and role changes**. **This is not the adoption of a production capture design, does not authorize production capture, and does not establish consistency for any artifact.** No production code, grant, schema, migration, dependency, formula, identity rule, notebook, export or the corrected `bulk_import_advisory_lock` changed; C6 stays failed as written (its seven expected failures are retained and were not rerun); C10's two endpoint-count expected failures are unchanged.

**The candidate and its observers (documented, recorded per endpoint).** The observation is the **existing** `observe_catalog` (the baseline's own constraint, index and ACL/ownership reads, digested per category) and the **existing** `require_catalog_unchanged` guard, imported from the C6 module; the baseline is the existing `capture_baseline` read on the snapshot owner's `Connection`. No second catalog definition or baseline representation exists (a static test enforces it). Each endpoint is a **new transaction** on one pooled backup-role connection (pool of one): **READ COMMITTED** (the server default, as in C6), beginning at its first statement (`SET LOCAL lock_timeout` and `statement_timeout`, then the three reads) and ending with a rollback when the block exits. The pool reuses one backend between endpoints, so the **transactions are separate but the session is not**: `now()` (the transaction start) was equal before and after the three reads of an endpoint and different between endpoints, and the observer's backend differed from the owner's. **An Engine does not supply one transaction across several reads**: calling `observe_catalog` through an Engine used **3 checkouts**, through one Connection **1** (measured). The owner stayed open and idle in its transaction through every `pg_dump` (state read from the server at exit; its visibility descriptor unchanged; no connection opened by the baseline reads; its backend held ACCESS SHARE on all 24 tables).

**Barrier for changes during the dump (test orchestration, NOT a production lock policy).** After the owner's baseline reads, a migrator session requested `ACCESS EXCLUSIVE` on `conferences` and was **confirmed queued** behind the owner (`pg_locks`, `pg_blocking_pids`). `pg_dump` was then started; the server showed it **inside its snapshot transaction and parked**: `xact_start` set, one active statement `LOCK TABLE public.alembic_version, public.conferences, ...` (**one batched statement**, so it parks partway through its table locks), `wait_event_type` `Lock`, an ungranted `AccessShareLock` on `conferences`, `pg_blocking_pids` = the barrier, and **`backend_xmin` equal to the owner's exported snapshot's xmin** (it had imported the snapshot). Only then was the operation applied and proven committed; the dump was still parked afterwards; the barrier was **cancelled** (`pg_cancel_backend`; SQLSTATE 57014, nothing committed, 0.33 s) and gone; the dump exited 0. The ordering is a sequence of recorded events, never elapsed time.

**Operations observed (every row is one real flow; PIDs, the ordered events, transaction starts, digests and raw differences are retained per scenario in the JUnit evidence).**

| Scenario | When | Operation | Commit status | Fresh observer | Candidate | Flow outcome |
|---|---|---|---|---|---|---|
| S1 control | -- | none | -- | -- | equal | guard passed; restore; strict equality on 24 tables; finalization stand-in permitted |
| S2a | after export, before the owner's baseline table locks | `ADD CONSTRAINT ... CHECK` (`ACCESS EXCLUSIVE`) | **committed**, no error | `constraints` | **detected** | refused |
| S2b | after the baseline reads (locks held) | the same `ADD CONSTRAINT` | **not committed: 55P03** after 5.04 s | nothing | nothing to detect | permitted; strict equality (a blocked change is **never** counted as detection) |
| S2c | after the baseline reads | additive `ADD FOREIGN KEY` (`SHARE ROW EXCLUSIVE`, compatible with ACCESS SHARE) | **committed** | `constraints` | **detected** | refused |
| S3a-c | during the dump (parked) | `GRANT INSERT`; `CREATE INDEX`; additive `ADD FOREIGN KEY` | **committed** each | `acls`; `indexes`; `constraints` | **detected** each | refused |
| S4a | during the dump | `ALTER ROLE ... RENAME` of a disposable role holding `SELECT` on `teams` | **committed** | `acls` | **detected** | refused |
| S4b control | -- | a disposable role owns `seasons`; no rename | -- | -- | equal | guard passed; **restore failed** (below) |
| S4b | after export | rename of that owning role | **committed** | `acls` | **detected** | refused |
| S4c | -- | `DROP ROLE` of the table owner; of an unreferenced role | **rejected, 2BP01**, role still exists; **committed**, role gone | -- | observation **unchanged** in both | -- |
| S5 | during the dump | `GRANT` then `REVOKE` (two committed transactions) | **both committed** | `acls`, then nothing | **equal endpoints, not detected** | **guard passed; flow permitted** (limitation retained) |

Constraints were never dropped or weakened and the backup role's grants were not expanded (an `ADD` of constraints, grants to the application role in the disposable database only, a disposable test-owned role; the application, backup and migrator roles were never renamed or dropped). Role names in the observation are the names at observation time: the old name before, the new name after.

**What each refusal did.** Every refusal ended the flow before the normal restore and finalization path: no `destination_verified_empty`, `restored` or `finalization_permitted` action, no accepted-artifact stand-in, an empty artifact directory. Each was followed by a **separately labeled DIAGNOSTIC restore** (action names `diagnostic:*`; never a capture; the refusal stood). **Observed (these operations, this version, small synthetic schema; not a general claim):** for S2a, S2c and S3a-c the committed change was **absent from the archive** and the diagnostic restore equals the owner's baseline on every established output, so the candidate's refusal there was **conservative**: it reports a live catalog difference between its endpoints, which is not the same thing as an archive that disagrees with its baseline. The **role renames were different**: with S4a the archive's `GRANT` named the role as it was when the dump read the ACL (the old name) and the restore **failed loudly** (`role ... does not exist`); with S4b the archive carried **both** the old name (`OWNER TO`) and the new name (`SET SESSION AUTHORIZATION`) and the restore failed the same way. Which dump read produced which name was not investigated. **A separate finding about the unchanged restore recipe** (S4b control, no rename): a table owned by a role other than the migrator cannot be restored (`must be able to SET ROLE`), because the recipe restores as the migrator without `--no-owner`.

**Measured: the candidate's own reads versus a queued exclusive request.** With an `ACCESS EXCLUSIVE` request queued on a table (and the owner holding ACCESS SHARE), the existing **constraint and ACL reads completed in 0.05 s and 0.01 s**; the existing **index read waited**: it queues for `ACCESS SHARE` on the table (the view renders each index with `pg_get_indexdef`) behind the queued request, not behind the owner, and was refused by its bounded lock timeout (55P03 after 4.01 s). The **full observation therefore failed closed after 10.01 s** (it raised and returned no observation for the guard), and recovered (0.02 s) as soon as the request was cancelled. Visibility proof in the barrier scenarios therefore used lock-free reads (the existing constraint and ACL reads and a list of index names); that probe never feeds the guard. **This requirement is retained as unmet** (strict xfail): the observation does not complete while an exclusive request is queued.

**Measured: multi-query behavior.** A long-lived observer (it read once, a change committed, it read again in the same transaction), against a fresh observer as the reference: a **new index** was seen by READ COMMITTED and **not** by REPEATABLE READ; a **role rename** and a **column rename** (which changes the rendered text of the dependent constraint and index) were seen by **both**. That is all that was measured: in this version, for these operations, rows follow the transaction's snapshot while role-name and definition text did not. No wider claim is made about which catalog functions are snapshot-bound or current, and one REPEATABLE READ observer is shown to be a **mixture**, not a single point in time; a READ COMMITTED endpoint takes a new snapshot for each of its three reads, so a change landing between them can tear it.

**Requirements retained as unmet (strict xfail).** (1) A change and its reversal during the dump is detected and refused: **not met** (S5: a fresh observer saw the grant between its two steps; the endpoints were equal; the guard passed). Here the archive and restore equalled the baseline, which is an observation, not a safety claim. (2) The candidate's observation completes while an exclusive request is queued: **not met** (it fails closed).

**Implemented, tested, verified, failed, proposed, deferred -- separately.** *Implemented (test layer only):* `tests/postgres/test_postgres_backup_catalog_candidate.py`. *Tested:* the candidate's detection and refusal in 11 full flows (7 refused and 4 permitted), the barrier ordering, the blocked and compatible constraint changes, role rename and drop, the change-and-reversal limitation, the observer semantics and lock behavior probes, cleanup and credential absence. *Verified:* `python -m pytest tests/postgres/test_postgres_backup_catalog_candidate.py -q -m postgres -ra`: **143 passed, 2 xfailed, 0 skipped, 0 warnings (93.3 s, exit 0)** in the working tree and **143 passed, 2 xfailed, 0 skipped, 0 warnings (93.7 s, exit 0)** in a clean worktree of `373b354` plus only the files it needs (exit codes 0; output, exit codes and JUnit retained outside the repository); sensitivity check **15 of 15 caught (plus one control correctly not counted)**; cleanup verified per scenario including the disposable roles; the existing Docker volume untouched; no credential in the evidence. *Failed:* the two requirements above (and, unchanged, the catalog bracket as written, C6, and C10's endpoint evidence). *Proposed, unadopted:* the fresh-endpoint candidate itself. *Deferred / not implemented:* production capture, any lock or timeout policy, restore verification records, any DDL audit or freeze mechanism, categories the observation does not read.

**What the candidate detects, what it misses, and what it would need.** *Detects (in these runs):* a committed difference in constraints, indexes, or ACL/ownership text between its two endpoints, including changes that commit while `pg_dump` is inside its transaction and role renames; every difference tried was detected and none that was blocked was counted. *Misses or does not establish:* a change and its reversal; anything outside the three categories (columns, triggers, functions, views, extensions, sequence parameters, comments, policies, default privileges, role attributes and memberships, database-level settings -- none was tested); whether the **archive** is consistent (here it matched its baseline although the candidate refused, and was unrestorable although role renames are detected); a torn endpoint. *Additional operational controls it would require:* administrative discipline or enforcement that migrations, grants and role administration do not run during the window (the advisory lock does not stop them); a bounded observation `lock_timeout` with a defined refusal or retry on timeout, and a migrator `lock_timeout` so a queued exclusive request does not stall observation (values not chosen); stable role names between capture and restore and a restore recipe that can restore ownership (decision pending); restore verification (claim 4) as the independent check, since the candidate does not establish archive consistency; and, if a change and its reversal must be excluded, a server-side DDL audit or a true freeze, **neither of which was designed or validated here**. *The production decision that remains open:* decision 6 below -- whether to adopt this observation as the catalog protection (accepting the residual risks above and recording them in the manifest), to add enforcement, or to rely on quiescence -- together with the lock and timeout policy (decision 7).

**Limits.** One table per operation; one PostgreSQL version and configuration; small synthetic data; test lock timeouts; the observer is READ COMMITTED only (REPEATABLE READ was measured for re-reads, not used as the candidate); role renames only (no role attribute or membership changes); categories the observation does not read are untested. Passing this unit does not establish artifact consistency, authorize capture implementation or complete Phase 9B.

## 13. Proposed v1 capture and restore operating contract (Phase 9B-6 design, 2026-10-05) -- PROPOSED, NOT IMPLEMENTED, NOT VERIFIED

**Status.** Documentation only. This section turns C1-C10 and the 12.8 candidate into one design for review. Nothing in it is implemented, tested or adopted, and writing it takes none of the section 11 decisions. Claim 3 still may not be asserted for any artifact. Production capture and Phase 9B remain incomplete. No historical result changes: C6's original bracket remains **failed** (seven strict xfail); 12.8's two unmet requirements (a change and its reversal; an observation while an exclusive request is queued) remain unmet; C10's endpoint counts remain **evidence only** (two strict xfail). Labels: **[reuse]** existing code used unchanged; **[missing]** not implemented; **[validate]** needs disposable validation before anyone relies on it; **[operational]** an operator responsibility that nothing enforces; **[gap]** the existing contract has no place for it. Gaps are named here, not filled with invented fields or reason codes.

### 13.1 Recommended design

**One design.** Capture uses the **exported snapshot** (decision 1, snapshot option) inside a **controlled capture window**:
- The backup job holds the existing advisory lock across every database-touching step, so software excludes controlled imports.
- Migrations, grants, role administration and ad hoc writes are excluded by **operational discipline only**.
- A preflight ownership check and the 12.8 fresh-endpoint observations are **bounded refusal checks**, never proof of stability.
- Sequences are recorded under `monotonic_v1`.
- There is no explicit table-lock layer (`ddl_exclusion = none`).
- Unsupported ownership is refused, never repaired.
- Only restore verification against the artifact's own baseline can produce a recovery-ready statement (claim 4).

| Choice | Why | Cost accepted |
|---|---|---|
| Snapshot, not quiescent | C1/C2: table data matches the baseline whether or not writers stop; quiescence cannot be proven (C10) | sequences are not frozen (C3), so the strict `sequences` comparison can differ |
| No explicit table locks | avoids C7's reader stall behind a queued exclusive request, and the undecided migrator `lock_timeout` | DDL that commits after the export is only **detected** (12.8 S2a), not prevented; ordinary baseline reads still hold ACCESS SHARE after they run (S2b was blocked) |
| Refuse unsupported ownership | restore stays as the non-superuser migrator, with ownership and ACLs carried and compared (V4) | an object owned by another role blocks every backup until an operator corrects it: loud, not silent |
| Shared advisory key (today's behaviour, C9) | excluding imports is the purpose of the window; no new key or namespace | imports are **refused**, not queued, while a backup runs (decision 7 records the choice) |

### 13.2 Supported source and restore target

| Item | v1 requirement | Basis | Status |
|---|---|---|---|
| PostgreSQL | major 16 on both source and destination; the only validated configuration is `postgres:16-alpine` with default settings | C1-C10, 12.8; md5 fingerprints compare only within one major (4.2) | other versions and settings unvalidated; refuse when the source's `server_version_num` major is not 16 [missing] |
| Client tools | `pg_dump`/`pg_restore` major 16; `-Fc`; single process (no `-j`); `--snapshot` | C1, C3, 12.8 | [reuse] test-layer pattern |
| Object set | schema `public` only: the 24 tables, the 13 sequences implied by `BIGSERIAL`, and their indexes and constraints. No views, functions, types, triggers or extensions (none exists in the six migrations; static inspection) | 6.5; `alembic/versions` | the baseline does not cover anything else |
| Sequence configuration | default parameters only: bigint, increment 1, cache 1, no cycle, owned by its column | 6.5; C3-C5 | R0 refuses anything else [missing in production; test-layer rule in 12.4]; cache above 1 and other types untested |
| Object ownership | database owned by `napsedge_migrator`; every `public` relation of kind `r` or `S` (the baseline's `acls` scope) owned by `napsedge_migrator` | runbook 6; 12.8 S4b control (a table owned by another role cannot be restored by the unchanged recipe) | **refuse otherwise** [missing] |
| Referenced roles | relation ACL grantees drawn only from `napsedge_migrator`, `napsedge_runtime_ro`, `napsedge_backup_ro`; no relation privilege granted to PUBLIC | 9B-2 boundary tests; runbook 6-7 | **refuse otherwise** [missing] |
| Stable role names | those three names identical at capture and at restore; no role referenced by an owner or ACL is renamed or dropped during the window, or between capture and restore | 12.8 S4a/S4b (after a rename the archive names the old role and the restore fails) | [operational]; the end observation detects a committed rename inside the window, not one made later |

**Unsupported ownership: refuse.** The check runs before the snapshot is exported. Restore must not use `--no-owner` or `--no-acl`, because they silently discard what V4 compares. It must not run as a superuser either, because that expands restore privileges.

**Manifest identity and provenance inputs: what the backup role can obtain today.**

| Field | Source | Backup role today |
|---|---|---|
| `source.database_name` | `current_database()` | yes: C9 ran `verify_database_target` as the backup role |
| `source.database_oid` | `pg_database` | not exercised as the backup role [validate] |
| `source.server_version_num` | `current_setting('server_version_num')` | not exercised as the backup role [validate]; `version()` was (C9) |
| `source.system_identifier` | `pg_control_system()` | **unknown.** Only a superuser has read it (9B-4). Execute permission for the backup role is unverified [validate]; check with `has_function_privilege` so a denial cannot abort the transaction. If denied, the field cannot be filled without a privilege decision, which has not been made [gap]. **Measured in 13.9: readable by the backup role on PostgreSQL 16 with default settings and the 9B-2 grants. No privilege was added.** |
| `source.backup_role` | `current_user` | yes |
| `schema.alembic_head` | `alembic_version`, read in the snapshot (the baseline's `version`) | yes (SELECT on all 24 tables; C1) |
| `schema.expected_head` | repository Alembic heads (pattern: `src/services/health.py`) | not a database privilege |
| `source.pg_dump_version`, `tooling.*`, `repository.git_revision`/`dirty` | job host | no production helper found in `src/` [missing] |
| `archive.toc_entries`/`toc_sha256` | `pg_restore --list` on the job host | the writer does not check these fields (12.1) [missing] |
| `consistency.foreign_write_sessions_*` | `pg_stat_activity`, identity columns only | yes (C10); **evidence only, never exclusion or quiescence** |

### 13.3 Exclusion and lock ownership

**Which activities take the existing lock.** The lock is `bulk_import_advisory_lock`: one key per verified database name, session level, non-blocking.

| Activity | Takes it? | Acquired / released |
|---|---|---|
| `run_controlled_bulk_import` | yes | right after `verify_database_target` and before manifest validation; released when its `with` block exits |
| `run_allstats_committed_import`, `run_team_game_log_committed_import`, `run_player_game_log_committed_import`, `run_espn_committed_import` | yes | same placement; when called by the coordinator they re-enter its lock on the same connection |
| Writes through an import's `session_factory` | **covered by the run's lock, not by their own.** The lock is held on `connection`, and binding `session_factory` to the same database is a documented caller obligation that is not enforced | for the run's duration |
| Backup capture (proposed) | yes, same key (C9: refusal in both directions) | 13.4 steps 1 to 7 |
| Alembic upgrade or downgrade | **no** | none |
| GRANT, REVOKE, ALTER DEFAULT PRIVILEGES, ALTER ... OWNER | **no** | none |
| CREATE/ALTER/DROP ROLE | **no.** These are **cluster-wide** (shared catalogs), so no database-local lock can cover them, and a lock on another database uses another key | none |
| Ad hoc `psql` or driver sessions | **no** | none |
| Runtime readers | no; read-only role (V7) | not a write path |

**Software enforcement versus operational discipline.** Software enforces only this: a controlled import and a backup on the same database cannot overlap (C9), and the snapshot keeps table data consistent regardless of writers (C1). Everything else in the window is **[operational]**: no migration, grant, ownership change or role administration, and no ad hoc writes. An operator's declaration that the window was respected is recorded as a declaration. **It does not guarantee that unmanaged activity stopped.** The fresh-endpoint observation may refuse a run when such activity committed a difference in constraints, indexes or ACL/ownership. It cannot see a change that was reversed, categories it does not read, or data writes. Endpoint session counts are neither exclusion nor quiescence proof (C10).

**Lock lifetime for a capture.** The lock is acquired first, before any other database work. It is held through the preflight, the snapshot export, the baseline and sequence reads, `pg_dump`, the owner's end, and the end observation. It is released before file finalization, which touches no database. The lock connection L runs no other statement.

*(Steps 1-4 below are implemented by `backup_capture_lock` in Phase 9B-6B, section 13.10, with one refinement: the session is discarded on every exceptional exit rather than by inspecting notes.)*

**Caller-owned recovery when release is unconfirmed.** "Unconfirmed" means the release raised, reported `false`, or left one of the notes `RELEASE_FAILED_NOTE`/`RELEASE_NOT_HELD_NOTE` on the primary exception (12.7). Preserving the primary failure does not release the lock.
1. L is the only connection of a dedicated Engine created with `NullPool`, so closing L ends its backend [validate: C9 showed only that the default pool returns a live backend that still holds the lock, and that `dispose()` alone does not end a checked-out connection].
2. On an unconfirmed release the caller calls `L.invalidate()`, so the DBAPI connection is discarded and never returned to a pool. Then it calls `L.close()` and disposes the Engine. It never rolls back and reuses L: a session lock survives rollback (12.7). It never retries the unlock (the manager's contract).
3. Confirmation comes from a **new** backup-role session: `pg_try_advisory_lock(key)` must succeed and is then released, the C9 pattern. If the lock is still held, the run reports the lock state as unconfirmed and stops. The backup role cannot terminate other backends and must not try. There is no outcome name for this case yet [gap].
4. After a confirmed release, L is still closed and its Engine disposed.

### 13.4 Capture and publication

**Connections.** L holds the lock. O is the snapshot owner. F is one fresh-observer connection that opens a new READ COMMITTED transaction for each endpoint, with `SET LOCAL lock_timeout` and `statement_timeout` (values not chosen). `pg_dump` is a child process. All connect as `napsedge_backup_ro` over verify-full TLS (section 8).

1. **L:** `verify_database_target`, then `bulk_import_advisory_lock`. A refusal is `SKIPPED:overlap` and writes no artifact [reuse].
2. **F, endpoint 1:** the preflight (13.2 checks), the existing `observe_catalog` start digest, and `foreign_write_sessions_start` (evidence). Then the transaction ends. A refusal writes no artifact.
3. **O:** `BEGIN ISOLATION LEVEL REPEATABLE READ, READ ONLY`; `pg_export_snapshot()`; the `pg_current_snapshot()` descriptor. Then **all baseline reads on O's own Connection** (`capture_baseline(Connection)`; an Engine is never passed, 12.3 and 12.8). Then `sequence_details` on O (direct `last_value, is_called`, parameters, column maximum; R0 recorded), all before `pg_dump` starts [reuse calculations; production location missing].
4. **`pg_dump --snapshot=<id> -Fc`**, single process, under a deadline, while O stays idle in its transaction (C1, C8).
5. **F, endpoint 2,** after `pg_dump` exits and while O is still open (the order validated in 12.8): the end digest and `foreign_write_sessions_end`. `require_catalog_unchanged` refuses on any difference (`catalog_changed`). Then **O** re-reads `pg_current_snapshot()` (`snapshot_unchanged_at_end`) and ends its transaction.
6. **L:** release, then confirm or recover (13.3).
7. **Job host:** `pg_restore --list` for the `archive.toc_*` fields. Then `write_artifact(...)` [reuse], which establishes claim 1 plus the contract's structural check.

**Authoritative schemas and calculations are preserved.** The baseline is `DatabaseBaseline` field for field, plus `sequence_details` (4.2). The fingerprint, catalog and sequence SQL are the existing definitions, not restated. No file schema, reason code or comparator changes.

**Sequence policy: `monotonic_v1`; `equal_v1` is not used.** With the snapshot, sequences can still advance after the baseline read (C3), and quiescence cannot be shown. R0 is checked at capture and refuses inapplicable sequences (C5). R1, R2 and R3 are evaluated at restore verification. **Equality of the strict `sequences` field does not establish next-ID safety:** in C4, R1 held with equal values while R2 failed. **Corrected in 9B-6B (13.10):** R2 is the next-ID safety condition (6.5), and **a failed R2 blocks recovery readiness** (13.5). It is evaluated at restore verification, never at capture, and the restored sequence is never reset or repaired to make it pass (C4). R3 (headroom) stays informational. Neither becomes a capture gate. *(The earlier wording here, which gave R2 informational status, is superseded.)*

**Fresh observations are bounded diagnostics and refusal checks.** Unequal endpoints refuse the run. A timed-out observation also refuses: the index read waits behind a queued exclusive request and fails closed (12.8). The refusal is not retried within the run. **Equal endpoints are recorded as "no committed difference observed at the two endpoints", never as stability:** S5's grant-then-revoke passed with equal endpoints, and a READ COMMITTED endpoint can tear across its three reads.

**Recording gaps in the manifest.** These block the capture unit, not the first unit in 13.7.
- `consistency.catalog_bracket_equal` is documented as the result of the **failed** bracket. Writing the fresh-endpoint result into it would silently change its meaning [gap; needs a format revision or a decision].
- No field records that the lock was held for the window, or the operator's declaration [gap].
- `ddl_exclusion = none`, `mode = exported_snapshot` and `sequence_rule = monotonic_v1` fit the existing schema.

**Timeouts, partial failure and cleanup, using the existing contracts.**

| Event | Existing outcome | Gap |
|---|---|---|
| lock refused | `SKIPPED:overlap`, no artifact | none |
| identity or head refusal | `FAILED` classes `source_identity_mismatch`, `schema_head_mismatch` | **[gap]** a `FAILED` record exists only inside an owned `.partial` (`write_failed_record(owned, ...)`), and the implemented writer creates the partial only when it writes the finished archive. No capture-phase failure therefore has a recording path |
| ownership or R0 refusal | none | **[gap]** no class; the same recording gap applies |
| owner lost before attach | `snapshot_unavailable` (C8) | recording gap |
| `pg_dump` fails | `dump_failed` | recording gap; the incomplete dump file (C8) stays in job scratch and is never passed to the writer |
| catalog difference or observation timeout | `catalog_changed` / `timeout` | recording gap; timeout values not chosen; a child process is killed with its process group (section 8) |
| writer failure | `PreconditionError`, `WriteFailed`, `PostPublicationError` (12.1, 12.2) | none; the writer never adopts or deletes a partial |
| release unconfirmed | none | **[gap]**; recovery in 13.3 |

On every path the job removes its own scratch (the dump file, the private pgpass directory), closes O, F and L, and disposes their Engines.

**`COMPLETE` keeps its meaning:** files finalized (claim 1), nothing more. A manifest written under this contract carries claim-3 *evidence*: a claim by the procedure, bounded by the residual risks. Those risks are a change and its reversal, categories the observation does not read, unmanaged writers, torn endpoints, and untested versions and settings. Claim 3 does not mean a restore will succeed.

### 13.5 Restore and recovery readiness

**Prerequisites for the supported ownership model.**
- An empty PostgreSQL 16 destination (E3).
- A bootstrap with the exact three role names and attributes, and the database owned by `napsedge_migrator` (runbook 6) [reuse; keeping the bootstrap definitions in version control is still FUTURE].
- `verify_artifact_directory(path, expected=...)` with the source `system_identifier`, `database_name` and head, immediately before restore [reuse].
- Archive checksum equality (E4).
- `pg_restore` as the migrator with `--exit-on-error --single-transaction`, and without `--no-owner` or `--no-acl`.
- The database and schema grants from runbook 7 re-applied.

**Independent verification against the durable captured baseline.**
- Parse `baseline.json` from the artifact, never a re-read of the source.
- Run the unchanged `compare_to_baseline` for data (V1), schema (V2), ownership and ACLs (V4) and version. A strict `sequences` difference is **reported, not suppressed**.
- Evaluate `monotonic_v1` over `sequence_details`: R1 against the baseline, R2 against the **restored** column maxima, R3 informational.
- Run the remaining runbook checks, V5 to V11.

The production path from the document to `DatabaseBaseline`, and the R1 to R3 functions, exist only in the test layer [missing in production].

**Recovery-ready (claim 4) is produced only when** claim 2 held immediately before the restore, the restore exited 0, and every check passed.
- **A failed restore, a failed check or any comparison difference never produces recovery-ready.**
- It is undecided whether a strict `sequences` difference that `monotonic_v1` explains may coexist with claim 4. Until that is decided, any strict difference withholds recovery-ready (fail closed).
- **A failed R2 (next-ID safety) withholds recovery-ready**, like any other failed check (corrected in 9B-6B; the earlier "informational" wording for R2 is superseded). R3 is reported informationally and never decides readiness on its own. Every R1-R3 outcome appears in the record.
- The outcome goes only into a separate `verification.json` outside the artifact [missing]. The artifact is never edited.

**Restore verification stays separate from structural validity:** `STRUCTURALLY_VALID` establishes `structural_integrity` only (section 5).

### 13.6 Requirement map

| Requirement | Reusable today | Missing implementation | Validation still required | Operational |
|---|---|---|---|---|
| target identity, lock, overlap | `verify_database_target`, `bulk_import_advisory_lock` | NullPool lock connection; unconfirmed-release recovery | NullPool close ends the backend and its lock | -- |
| preflight: version, head, ownership, grantees, R0 | Alembic heads pattern; the R0 rule (test layer) | **the 13.7 module** | backup-role access to `pg_control_system()`, `pg_database`, `server_version_num`; completion while an exclusive request is queued | correcting refused ownership |
| snapshot owner and baseline reads | `capture_baseline(Connection)`, `sequence_details` reads, adapter (test layer) | a production location for the reads that keeps one definition | -- (C1, C3, C8) | -- |
| fresh-endpoint catalog check | `observe_catalog`, `require_catalog_unchanged` (test layer) | production location; timeout values | -- (12.8); its unmet limits stay | no DDL, grants or role administration in the window |
| manifest recording | `ArtifactMetadata`, `write_artifact` | the catalog-observation field; capture-phase failure recording; lock and declaration evidence | -- | the window declaration |
| restore and claim 4 | runbook E1-E4 and V1-V11, comparator, verifier | production comparison path; R1-R3; `verification.json` | -- | stable role names; same-major destination |

### 13.7 Smallest first production-code unit: a read-only capture preflight

**Purpose.** Implement the 13.2 refusal checks as facts a later capture can act on. Taking no lock, snapshot or dump, it can be reviewed and tested independently of decisions 6 and 7.

**Location.** `src/deployment/backup_capture_preflight.py`, with tests `tests/regression/test_deployment_backup_capture_preflight.py` and `tests/postgres/test_postgres_backup_capture_preflight.py`. It would be the first module in `src/deployment/` that reads a database. Like `bulk_import_safety.py`, it takes only a caller-owned `sqlalchemy` `Connection`. Standard library plus SQLAlchemy and `src.pipeline.bulk_import_safety`; no new dependency.

**Interfaces (proposed).**
- `CaptureSourcePolicy` (frozen): `designation: DatabaseTargetDesignation`, `expected_alembic_head: str`, `supported_server_major: int` (16), `object_owner_role: str` (`napsedge_migrator`), `permitted_grantee_roles: frozenset[str]` (the three roles).
- `SequenceApplicability` (frozen): `name`, `owned_by: tuple[str, str] | None`, `data_type`, `increment`, `cycle`, plus a pure `applicable` property (R0).
- `OwnershipFinding` (frozen): `relname`, `relkind`, `owner`, and the grantee that falls outside the policy (or `PUBLIC`).
- `CapturePreflight` (frozen): `verified_target`, `database_oid`, `server_version_num`, `alembic_head`, `system_identifier: str | None` together with `system_identifier_readable: bool`, `unsupported_ownership: tuple[OwnershipFinding, ...]`, `sequences: tuple[SequenceApplicability, ...]`, and a pure `accepted` property.
- `observe_capture_preflight(connection, policy) -> CapturePreflight`.

The result holds **facts and one boolean**. It defines no reason codes, no `FAILED` classes and no manifest fields.

**Exclusions.** No lock, snapshot, `pg_dump`, baseline capture, sequence-state read, catalog digest, file write, `FAILED` record, retry, scheduling, storage or entrypoint. No commit, rollback, close or dispose: the caller owns the transaction. No statement that waits on a relation lock: the preflight reads catalogs only, never `pg_get_indexdef` or a sequence relation. *(Corrected in 13.9: the Alembic head is necessarily read from the table `public.alembic_version`, which takes ACCESS SHARE on that one table. No application table is read.)* No import from `tests/`.

**Acceptance criteria.**
- Against a disposable PostgreSQL 16, migrated, with the 9B-2 grants and run **as the backup role**, a conforming database is `accepted`.
- Each of these is reported precisely and not accepted: a table owned by a disposable role (the S4b setup); a grant to a non-permitted role; a PUBLIC grant; a `CYCLE` sequence and a negative increment (the C5 setups); a head mismatch; a non-16 major (unit level, with a fake result).
- A database-name mismatch raises the existing `DatabaseTargetVerificationError`.
- `system_identifier_readable` reports the server's actual answer, whichever it is, and a denial does not abort the transaction.
- The function completes while an `ACCESS EXCLUSIVE` request is queued on an application table.
- The caller's transaction state and the connection are unchanged afterwards.
- Static tests: no write statement, no import from `tests/`, no work at import time.
- The existing modules are byte-identical.
- Manifests are reviewed by the established convention.

**Targeted validation commands (proposed; not run).**
```
python -m pytest tests/regression/test_deployment_backup_capture_preflight.py -q
python -m pytest tests/postgres/test_postgres_backup_capture_preflight.py -q -m postgres -ra
python -m pytest tests/regression/test_bulk_import_safety.py tests/regression/test_deployment_backup_artifact.py tests/regression/test_deployment_backup_artifact_writer.py -q
```

**Later units, in order (each separately authorized):**
1. The lock connection and unconfirmed-release recovery (13.3).
2. A production location for the snapshot-owner reads and the catalog observation.
3. The manifest-recording gaps (13.4), then capture.
4. The restore comparison path with R1-R3 and `verification.json`.

Scheduling, storage, retention and monitoring stay out of scope.

### 13.8 Decisions that block the 13.7 unit

**All three were approved by Anthony on 2026-10-05.** The 13.7 unit is therefore unblocked as a design. It is **not implemented**, and no test or validation has been run. The approval covers only the read-only preflight. It does not adopt the rest of section 13 as implemented or verified behavior, and it does not authorize capture, a lock wrapper, storage or scheduling. The decisions as originally posed:

1. **Operating direction.** Adopt 13.1 as the basis: the snapshot with `monotonic_v1` and no explicit table locks (decisions 1 and 2). The preflight's R0 refusal only makes sense under `monotonic_v1`.
2. **Ownership policy.** Refuse unsupported ownership (recommended). Fix the permitted set as exactly the migrator as owner, the three roles as grantees, and no PUBLIC relation grants.
3. **Production code.** Approve the read-only module in `src/deployment/`. It would be the first database-reading module there.

Not blocking this unit: adopting the fresh observer and its timeouts (decision 6), the lock key and migrator `lock_timeout` (decision 7), quiescence evidence (decision 8), the `system_identifier` privilege if it is denied, the manifest and failure-recording gaps, and the claim-4 treatment of strict sequence differences.

### 13.9 Phase 9B-6A: the read-only capture preflight, implemented (2026-10-05)

**Scope.** This is the 13.7 unit only, built on the three decisions approved in 13.8. It adds one production module, `src/deployment/backup_capture_preflight.py`, and two test modules. **Production capture is not implemented**, and the rest of section 13 is unchanged as a proposal. No lock wrapper, snapshot, `pg_dump`, manifest field, reason code, `FAILED` class, grant, role, migration, dependency, schema, formula, identity rule, notebook or export was added or changed. The existing modules are untouched: `bulk_import_safety.py`, the artifact reader and writer, and the test harnesses.

**Implemented.** `observe_capture_preflight(connection, policy) -> CapturePreflight` takes one caller-owned `Connection`. It never commits, rolls back, closes or disposes it and sets no timeout. `v1_policy(designation, expected_head)` holds the approved values: major 16, `napsedge_migrator` as owner, the three roles as the only grantees, and PUBLIC never permitted. The result holds facts plus pure properties, and `accepted` is true only when every one of these holds:
- the server major is supported;
- the Alembic heads are exactly `(expected,)`;
- the database owner is the migrator;
- there is no ownership finding;
- there is no unsupported object;
- every sequence passes R0 **and** is in the validated configuration.

`system_identifier` is reported through `system_identifier_readable` and `system_identifier`. It is asked through `has_function_privilege` first, so a denial cannot abort the caller's transaction. Its readability is **not** part of `accepted`.

**Beyond the 13.7 text, stated rather than hidden:**
- The database owner is checked.
- Column-level ACL grantees are checked as well as relation ACLs.
- These are reported as unsupported objects:
  - relation kinds other than `r`, `S` and `i`;
  - functions, non-internal triggers and extensions in `public`;
  - enum, domain, range and multirange types.
- `alembic_heads` is a tuple, so a database with several heads is refused rather than truncated.
- The sequence inventory SQL is character-for-character identical to the test layer's validated `_INVENTORY_SQL`, and a test enforces that equality. Only parameters are read, never sequence state.

**Correction to 13.7.** The Alembic head can only be read from `public.alembic_version`, so the preflight takes ACCESS SHARE on that one table. Every other read is a catalog or function read.

**Measured.** As the backup role, with the unchanged 9B-2 grants on PostgreSQL 16 with default settings, `has_function_privilege('pg_catalog.pg_control_system()', 'EXECUTE')` was true, and the identifier returned equalled the one the superuser read. **The 13.2 `system_identifier` gap is closed for this configuration only.** No privilege was added.

**Tested.**

*`tests/regression/test_deployment_backup_capture_preflight.py`:* 35 tests with a fake connection that answers the module's own statements:
- policy validation;
- acceptance;
- each refusal: major 17; three head shapes; a missing `alembic_version`; the database owner; a relation owner; a non-permitted and a PUBLIC relation grantee; a column grantee; unsupported relation kinds and objects; six sequence configurations;
- an unreadable `system_identifier` that is never queried;
- no transaction-control call and SELECT statements only;
- static checks: ten single-SELECT statements with no write, lock, snapshot, advisory, `pg_get_indexdef` or sequence-state token; only `alembic_version` read as a table; the exact set of imports; no connection control; no work at import; the exact public symbols.

*`tests/postgres/test_postgres_backup_capture_preflight.py`:* 12 tests on one disposable TLS cluster (the existing 9B harness), every observation as the backup role:
- the inventory drift check;
- a conforming migrated source is accepted with its real oid, version, head, 13 sequences and readable `system_identifier`. The caller's transaction keeps its start time, its `SET LOCAL` value and an `idle in transaction` backend, and the engine recorded no begin, commit or rollback;
- a name mismatch raises the existing `DatabaseTargetVerificationError`;
- a head mismatch is refused;
- a table given to a disposable role (the S4b setup) is refused. `ALTER TABLE ... OWNER TO` also moved the table's indexes and gave the new owner the old owner's ACL entries, and exactly those findings are reported;
- a relation grant to a disposable role, a PUBLIC grant and a column grant are each refused;
- a view is refused;
- `CYCLE` and `INCREMENT BY -1` (the C5 setups) are refused for that one sequence only;
- **queued exclusive request:** a migrator held ACCESS SHARE on `teams` and a second migrator's `ALTER TABLE` was confirmed queued from `pg_locks`. A backup-role control read of `teams` then failed with 55P03 under a 1 s `lock_timeout`, while the preflight, under 3 s, completed and accepted. The request was still queued afterwards, was then cancelled (57014), and never committed.

**Verified.**
- `python -m pytest tests/regression/test_deployment_backup_capture_preflight.py -q`: **35 passed** (working tree and clean worktree).
- `python -m pytest tests/postgres/test_postgres_backup_capture_preflight.py -q -m postgres -ra`: **12 passed** (working tree, about 27 s; clean worktree, 27.1 s).
- The clean worktree was `373b354` plus only the 14 files the selection needs.
- The adjacent regression selection (the new module, `test_bulk_import_safety.py`, the artifact and writer modules, and the four existing deployment test modules): **817 passed, 13 skipped**. The skips belong to the existing artifact and writer modules (platform-only and symlink tests). The counts were not re-derived from those modules separately.
- **Sensitivity check, 11 of 11 caught** by expected assertion failures, with the module restored byte for byte after each:
  - ownership findings ignored by `accepted`;
  - PUBLIC tolerated;
  - column grants skipped;
  - R0 ignoring `CYCLE`;
  - `system_identifier` queried without the privilege check;
  - unsupported relation kinds ignored;
  - the database owner unchecked;
  - the head read touching an application table (caught by the queued-request test);
  - the caller's transaction committed;
  - the inventory drifting from the validated query;
  - relation owner changes tolerated.
- Disposable roles were dropped and confirmed absent; no container remained; the existing Docker volume was not touched; the clean worktree was removed.

**Discrepancies investigated.** The first PostgreSQL run had two failures, both in the tests and not in the module:
1. The ownership test expected findings for `seasons` only. The owner change legitimately also moves its indexes and rewrites its ACL, so the assertion was made **exact** rather than looser.
2. The queued-request thread called `begin()` after SQLAlchemy had already autobegun a transaction. The thread now ends that transaction first.

No expected safety outcome was changed.

**Limits.** One PostgreSQL version and configuration; one table per deviation. Schema- and database-level ACLs, default privileges, policies, role attributes and memberships, and objects outside `public` are not checked. The preflight is a point-in-time observation in the caller's transaction: it is not exclusion and not evidence of stability. A refusal has no recording path yet (the 13.4 gap). **Not implemented:** the lock connection and unconfirmed-release recovery, the production snapshot-owner reads, the fresh-endpoint observation in production, the manifest-recording gaps, capture, and restore verification. Decisions 6, 7 and 8 remain open. Claim 3 may not be asserted for any artifact, and Phase 9B remains incomplete.

**Manifests.** Reviewed by the established convention. None of the module's 12 public top-level symbols collides with either manifest, so no entry changed (1,156 data rows each; both files are byte-identical to their preflight hashes).

### 13.10 Phase 9B-6B: the dedicated backup lock session (2026-10-05)

**Reconciled in 9B-6C.** 9B-6B is **verified within its tested scope**: the scenarios, configuration and injected faults listed below. It does not claim that every possible failure path was validated. The F outcomes fail at three different stages: target verification after a successful connection, authentication during the connection attempt, and URL validation before any connection. The JUnit-warning account below has been corrected.

**Boundaries of 9B-6A, clarified first (no result changes):**
1. **`accepted` is narrow.** It means the implemented support checks of 13.9 passed. It does **not** establish catalog stability, complete provenance, a successful capture or recovery readiness.
2. **`system_identifier` access is shown for one setup only.** It was demonstrated in the tested setup (PostgreSQL 16 defaults, `postgres:16-alpine`, the 9B-2 grants) and is **not** generalized to every deployment. If a deployment cannot supply that provenance, the later capture unit must block on it.
3. **Only one preflight read was tested against a queue.** The preflight's head read (not `verify_database_target`, which runs `SELECT current_database(), version()`) reads `public.alembic_version`, so it can wait on that table's lock, bounded by the caller's `lock_timeout`. The queued-request test placed the request on an application table (`teams`); it does **not** show that every preflight read is non-blocking.

**R2 wording corrected.** A failed R2 (next-ID safety) **blocks recovery readiness**; R3 stays informational (13.4, 13.5). The earlier active wording that gave R2 informational status is superseded. C4's result is unchanged: R2 failed and nothing was repaired.

**Scope.** This unit adds one production module, `src/deployment/backup_capture_lock.py`, and two test modules. **No shared helper changed:** `bulk_import_safety.py` (namespace, key, signatures, single release) and every deployment module are byte-identical to their pre-change hashes, so the coordinator and runner suites were not rerun. Nothing in this unit exports a snapshot, runs `pg_dump`, captures a baseline, restores, writes an artifact, adds a grant, migration or dependency, or touches a formula, identity rule, notebook or export. **Production capture is not implemented, and Phase 9B remains incomplete.**

**Interface and resource-ownership contract (implemented).** `backup_capture_lock(database_url, designation)` is a context manager. It yields a `BackupLockSession` holding `verified_target`, `lock_key`, `backend_pid` and `backend_start`, plus `cleanup` once the block exits. **It never yields a connection.**

*What it creates and owns, and the only things it ever cleans up:*
- one `NullPool` lock engine (application name `napsedge_backup_lock`). Its single physical connection is the lock session L. A second physical connect on that engine is refused (`BackupLockSessionLost`) before it reaches the network, so L can never be replaced silently by a new backend;
- after exit, one `NullPool` verifier engine and connection (`napsedge_backup_lock_verify`).

*Order:*
1. Input checks before anything is created: a `DatabaseTargetDesignation`, and a URL with `postgresql+psycopg`, `sslmode=verify-full`, an explicit `sslrootcert`, a host and a database. **No production TLS validator existed**, so the module enforces the section 8 rule itself. Its messages never echo the URL.
2. Connect. A failure becomes `BackupLockSetupError("... (TypeName)")` with no chained driver message.
3. Read L's PID and `backend_start`.
4. `verify_database_target`, then roll back L's own autobegun transaction.
5. The **unchanged** `bulk_import_advisory_lock` acquires the lock. A refusal is its `AdvisoryLockUnavailableError`, and the body never runs.
6. Roll back L's transaction again. This ends the transaction only and **does not release the session-level lock**; the server showed the session `idle`, not `idle in transaction`, during the body.
7. Run the body.
8. The shared manager makes its single unlock attempt. This module adds no unlock of its own, no retry and no `pg_advisory_unlock_all`.
9. If any exception left the block, `Connection.invalidate()` discards L.
10. `close()` (a physical close under `NullPool`).
11. `dispose()`.
12. A fresh, independently validated verifier session polls, with a deadline, for L's (PID, `backend_start`) in `pg_stat_activity` and lists the key's holders in `pg_locks`, each poll in a new transaction.

Every cleanup step is attempted even if an earlier one raised. `LockCleanupReport.verification` is `closed_and_released`, `original_session_present`, `not_verified` or `no_session_created`. Another holder of the key is reported as `other_holder_present` and never mistaken for L. Cleanup policy never reads exception notes.

*Errors:*
- A primary exception propagates as the same object: its type, cause, notes and traceback are kept, and the shared manager's notes are unchanged.
- Unconfirmed cleanup adds one bounded note (`step:TypeName` entries only).
- Without a primary exception, unconfirmed cleanup raises `BackupLockCleanupError`, which carries the report.
- A new `KeyboardInterrupt` or `SystemExit` raised during cleanup propagates after the remaining steps; a primary exception stays reachable as its context.
- The shared manager's own exceptions pass through unchanged.
- No error or note written by this module contains a URL, credential, host or driver message.

**Distinct from the snapshot owner.** L runs only identity, verification, lock and rollback statements *(and, since 9B-6C, its own identity facts and `confirm_held()`; see 13.11)*. It is not yielded, and it is never the snapshot-owning session of 6.2, which will be a separate connection.

**Server-observed behavior (one disposable PostgreSQL 16 cluster, synthetic data, the 9B-2 grants; administrator evidence from `pg_locks` and `pg_stat_activity`; exported from JUnit).** PIDs are from the retained working-tree run.

| Scenario | Acquired | Body | Release | During | After (server) | Cleanup report |
|---|---|---|---|---|---|---|
| A normal | yes | ran | confirmed | holders `[(85, granted)]`; session `idle` | backend gone; no holders; a **distinct** probe session (another PID) acquired and released, then was confirmed gone | closed_and_released, not discarded |
| B body failure | yes | raised | confirmed | holders `[(97)]` | gone; none | same object, caller's note only; discarded; closed_and_released |
| C aborted transaction (test seam) | yes | `1/0`, rollback, `1/0` | **unconfirmed** (unlock raised `InternalError`) | **after rollback: still `[(108)]`** (real PostgreSQL: the session lock survives rollback); **at discard, after the failed unlock: still `[(108)]`** | gone; none | primary `DataError` (22012) kept with the manager's note; discarded; closed_and_released |
| D backend loss, with a primary | yes | test admin terminated L | unconfirmed (`OperationalError`) | none after termination | gone; none | primary kept with the manager's note; one physical connect (no reconnection) |
| D backend loss, success path | yes | test admin terminated L | the manager's `OperationalError` propagated unchanged | none | gone; none | discarded; one physical connect |
| E refusal | **no** (immediate) | **never entered** | not attempted | the first owner (a distinct PID) holds the key | L gone; the first owner still holds it and is still usable; it then released and was confirmed gone | no note (another holder reported, not a leak) |
| F wrong target (stage: **after** connecting; TLS and authentication succeeded, and target verification refused) | no | never | not attempted | -- | L gone; none | `not_verified`: the verifier validates against the same wrong designation, so it is reported honestly in a note, never as success |
| F authentication failure (stage: **during connect**; wrong password, refused during authentication) | no | never | -- | -- | no lock session after the attempt | `BackupLockSetupError("... (OperationalError)")`, no cause, no secret |
| F weak TLS (stage: **before any engine or connection**; refused by URL validation) | no | never | -- | -- | no session | `ValidationError` before any connection |
| G false unlock (test seam) | yes | body unlocked early | `false`; the manager's error propagated | none after the early unlock | gone; none | discarded; closed_and_released |
| H verifier unavailable (injected) | yes | ran | confirmed | -- | **gone; none** | `not_verified` gives `BackupLockCleanupError`: no false success although the server shows it closed |
| I interrupt | yes | `KeyboardInterrupt` | confirmed | -- | gone; none | same object, no note |
| J repeated x3 | yes | ran | confirmed | three distinct PIDs, each the sole holder | all gone; no advisory row in the database | each closed_and_released |
| K caller's connection | yes | ran | confirmed | -- | caller: same transaction (`now()` equal), `SET LOCAL` kept, `idle in transaction`, no begin, commit or rollback events | closed_and_released |

**Injected behavior, kept separate (fakes, `tests/regression/test_deployment_backup_capture_lock.py`).**
- Ordering, and a single unlock.
- A refusal never unlocks.
- Every cleanup step still runs after a failure in `invalidate`, `close`, `dispose` or the verifier.
- A session still present at the deadline gives no false success.
- Another holder is reported separately.
- Primary interrupts are preserved.
- New interrupts are not swallowed.
- Credential scans pass.
- A real engine pointed at a closed port shows the second-connect refusal.
- Static checks: no unlock, advisory or terminate SQL in executable strings; no commit or begin; no notes access; the exact import set.

**Verified.**

| Command (run in the working tree, then in a clean worktree of `373b354` plus exactly 18 files) | Result | Exit code |
|---|---|---|
| `python -m pytest tests/regression/test_deployment_backup_capture_lock.py tests/regression/test_bulk_import_safety.py -q` | **86 passed** (37 + 49) in both | 0 |
| `python -m pytest tests/postgres/test_postgres_backup_capture_lock.py tests/postgres/test_postgres_bulk_import_safety.py tests/postgres/test_postgres_backup_overlap_quiescence.py -q -m postgres -ra` | **85 passed, 2 xfailed** in both (about 98 s each) | 0 |

- The 2 xfailed are **C10's two unchanged strict expected failures**.
- There were no skips.
- **Corrected in 9B-6C (reconciled from retained evidence, no rerun).** The first working-tree PostgreSQL run had **14 warnings**, all one `PytestWarning`: `record_property` is incompatible with junit_family `xunit2`, pytest's default. These four things are distinct:
  - **Warning:** that run did emit it.
  - **Schema incompatibility:** that is what the warning reports.
  - **Missing XML properties:** **not established.** Pytest's own `finalize()` writes user properties whatever the family; the warning only flags the schema. The single check made then (`grep -c property`) returned **1** on a file with no line breaks, which indicates presence, not absence. That xunit2 file was then **overwritten** by the rerun and was never inspected or exported, so the earlier statement "the evidence was not written" was **unsupported** and is withdrawn.
  - **Exporter failure:** none; the exporter was never run on that file.
- The rerun used `-o junit_family=xunit1` (same selection) and had **0 warnings**. Its JUnit is the one exported (138 `property` elements in total; 14 are this module's scenario records).
- **Commands as actually executed** (`<E>` is the evidence directory `scratchpad/evidence_9b6b`, outside the repository):
  1. `python -m pytest tests/regression/test_deployment_backup_capture_lock.py tests/regression/test_bulk_import_safety.py -q -p no:cacheprovider --junitxml "<E>\wt_regression.xml"`: exit 0.
  2. `python -m pytest tests/postgres/test_postgres_backup_capture_lock.py tests/postgres/test_postgres_bulk_import_safety.py tests/postgres/test_postgres_backup_overlap_quiescence.py -q -m postgres -ra -p no:cacheprovider --junitxml "<E>\wt_postgres.xml"`: exit 0, xunit2, 14 warnings, and the file was later overwritten.
  3. The same selection with `-o junit_family=xunit1 --junitxml "<E>\wt_postgres.xml"`: exit 0, 0 warnings.
  4. In the clean worktree, the regression command with `--junitxml "<E>\clean_regression.xml"` and the PostgreSQL command with `-o junit_family=xunit1 --junitxml "<E>\clean_postgres.xml"`: both exit 0.
- The structural exporter found **14 of 14** required scenario records in both JUnit files, and failed as intended on a negative control with one record removed.
- Output, exit codes and JUnit are retained outside the repository.

**Sensitivity check: 10 of 10 caught.** A defect counts only when the named behavioral assertion fails; setup errors are never counted. The module was restored byte for byte after each.
- pooled-session reuse (caught: "the original lock backend is still present on the server");
- an omitted physical close (same assertion);
- an ignored refusal (the body was entered);
- a masked primary failure;
- a false cleanup confirmation;
- a leaked verifier lock, with the verifier's session kept referenced (caught by J's server holder check);
- no discard on failure;
- silent reconnection allowed;
- a driver message leaked on setup failure;
- a swallowed new interrupt.

**Discrepancies investigated.**
1. In a first sensitivity pass, six defects failed their tests through an unexpected exception type rather than an assertion, so they were not counted. The tests now catch any escaping exception and **assert its exact type**, and scenario A asserts server-side absence before anything else.
2. The first leaked-verifier mutant (verifier connection not closed, but unreferenced) was **not detected**: CPython's reference counting closed the connection as soon as the function returned. That is exactly the kind of closure the module must not rely on, so the mutant was strengthened to keep a reference, as a real leak would, and was then caught.

No expected outcome was changed.

**Credential safety.**
- Live scans against all cluster passwords: the F scenarios (wrong target, setup failure, weak TLS: messages and notes) and scenario I (the report).
- Injected scans in the regression module: a planted password and host checked in every module-written message, note and formatted traceback.
- The setup-failure message is fixed text, and the engine uses `hide_parameters=True`.
- The other live scenarios were not scanned.

**Limits (stated, not claimed away).**
- One PostgreSQL version and configuration.
- The backup role's visibility of its **own** sessions and of `pg_locks` was sufficient here. It was not tested under other roles or settings.
- A successful reacquisition probe shows availability at that moment, not continuous exclusion.
- Verification is bounded: 10 s, 50 ms polls, a 15 s statement timeout and a 15 s connect timeout. These are module constants, not a chosen operational policy.
- The shared key still does not make migrations, grants, role administration or ad hoc sessions cooperate.
- C and G act on L only through the documented test seam.
- **Not implemented:** the snapshot owner, the production fresh-endpoint observation, the manifest and failure-recording gaps, capture, and restore verification. Decisions 6, 7 and 8 remain open.

**Manifests.** Reviewed under the convention established for Phase 9 deployment code (9A, 9B-5A, 9B-5B, 9B-6A): manifest rows are keyed by notebook function name. A `src/deployment` symbol with no notebook origin gets no row, and only a bare-name collision updates an existing row. The 12 public symbols of the new module have no notebook origin and no collision (1,156 data rows each; both files byte-identical to their pre-change hashes). The module **consumes** `bulk_import_advisory_lock` and `verify_database_target`, whose existing rows list only their defining file and are unchanged because those functions did not change. **Stated, not hidden:** Phase 5 modules in `src/pipeline` (for example `bulk_import_safety.py`) *did* receive `src_only` rows, so the project used two conventions over time. Reconciling them is out of scope here and was not done.

### 13.11 Phase 9B-6C: the snapshot-owner session and its baseline reads (2026-10-05)

**Status.** Implemented, tested and verified **within its tested scope**: one disposable PostgreSQL 16 cluster, synthetic data, the 9B-2 grants, and the scenarios and injected faults below. This unit establishes snapshot ownership and baseline-read behavior. It does **not** establish:
- continuous catalog stability (decision 6, the fresh-endpoint observation, stays deferred and is not adopted);
- complete provenance;
- archive consistency;
- a successful capture;
- a recoverable backup or recovery readiness.

It adds no `pg_dump`, restore, artifact or file publication, migration, grant or dependency. Production capture is not implemented, and **Phase 9B remains incomplete.**

**Interface and resource contract (implemented, `src/deployment/backup_capture_snapshot.py`).**
- **Call:** `backup_capture_snapshot(lock, database_url, designation, *, expected_alembic_head, artifact_id)`, a context manager used **inside** an active `backup_capture_lock` context.
- **Yields a `SnapshotHandle`:**
  - `snapshot_id`: the exported id, importable only while the context is open;
  - `visibility_descriptor`: from `pg_current_snapshot()`;
  - `baseline`: the production `BaselineDocument`;
  - `preflight`: the 13.9 facts, observed on S;
  - `facts`: S's PID, `backend_start`, transaction start, server-reported isolation and read-only setting, `system_identifier`, database OID, name, server version, and L's PID.
- **After exit:** every accessor raises `SnapshotHandleClosed`, and `cleanup` holds a `SnapshotCleanupReport`.
- **Owns** one `NullPool` engine (`napsedge_backup_snapshot`, built by the lock module's own `_build_engine`, so a second physical connect is refused) with its single physical session S, plus the lock module's cleanup verifier. **S is never L:** its PID must differ.
- **Reused, not restated:**
  - the lock module's URL and TLS check, engine builder and cleanup verifier;
  - `observe_capture_preflight` (including `verify_database_target`);
  - the preflight's sequence-inventory SQL;
  - `BaselineDocument`, its entry types, `parse_baseline` and `canonical_json_bytes`.

**Dependency map.** The snapshot module depends on the lock module, the preflight module, the artifact contract (`backup_artifact`) and `bulk_import_safety` (designation only). The lock module now also imports three statement constants from the preflight module. Nothing in `src/` imports `tests/`.

**Compatible additions to the lock module (13.10).**
- At setup, L reads **on itself** the database OID and, guarded by `has_function_privilege`, the cluster `system_identifier` (None when unreadable; the lock module does not refuse on it).
- `BackupLockSession.confirm_held()` asks **L itself**, in a new transaction that it then ends, whether L's backend still holds the key. It raises a sanitized `BackupLockNotHeld` otherwise, including after the context has exited.
- The lock behavior, the shared manager, the key and the namespace are unchanged.

**Order (implemented).** No usable handle is yielded unless every step succeeds:
1. Inputs are checked and `lock.confirm_held()` passes, all before anything is created.
2. S connects. Isolation (`REPEATABLE READ`) and read-only mode are set on the connection, and the transaction is begun explicitly (no autobegin).
3. `SET LOCAL lock_timeout = 5000` (S only), then the server's own `transaction_isolation`, `transaction_read_only`, PID, `backend_start` and `now()`. Anything other than `repeatable read` / `on`, or S sharing L's PID, is refused.
4. The preflight runs on S. The source is refused unless `accepted`, with the facts attached.
5. Identity: L's and S's own `system_identifier`, database OID and verified name must agree. A `system_identifier` missing on either side **fails closed** in this unit. The preflight's `accepted` keeps its 13.9 meaning.
6. `confirm_held()`, then `pg_export_snapshot()` and `pg_current_snapshot()`.
7. The baseline, read on S's Connection: the same SQL, order and shapes as `capture_baseline`, plus the C3-C5 sequence reads. It is validated by `parse_baseline` on its own canonical bytes. `now()` is re-read and must be unchanged (one transaction).
8. `confirm_held()`, then yield.
9. After a normal body: `confirm_held()`, and S must still report the same PID, snapshot descriptor and transaction start.
10. Exit: S's transaction ends (a rollback; on any exception S is discarded with `invalidate()`); close; dispose; then bounded server verification of S's absence. **This all completes before the enclosing lock context releases L.**

Lock checks are point observations at those boundaries; **they do not prove continuous ownership between them**, and no monitoring thread exists. Sequence values are read live and are **not** frozen by the snapshot. Nothing calls `nextval` or `setval`, or repairs anything.

**Server-observed results (retained working-tree run; exported from JUnit).**

| Scenario | Evidence |
|---|---|
| A identity | L PID 87, S PID 88, consumer PID 89, all distinct. L's own facts `(7693200524397363235, 16389)` equal S's own and the administrator's `pg_control_system()` and `pg_database`. During the body the administrator saw L as the sole holder of the key |
| B transaction | S reported `repeatable read` and read-only `on`. The administrator saw S as `napsedge_backup_ro`, `napsedge_backup_snapshot`, `idle in transaction`, with `xact_start` equal to S's transaction start and `backend_xmin` 748 equal to the exported snapshot's xmin (descriptor `748:748:`) |
| C import | A distinct consumer ran `SET TRANSACTION SNAPSHOT '00000005-00000003-1'` while S was open. The unchanged test-layer `capture_baseline` plus `read_sequence_details` on it, mapped by the existing adapter, **equalled** S's `BaselineDocument` exactly (24 tables, 13 sequences, head `5b02bea0eb85`) |
| D commit | A migrator inserted one `teams` row inside the body and committed. A fresh administrator read saw it (1 row; teams 3 to 4): an independent proof of the commit. S itself (a SELECT through the test seam) saw 0; the consumer, importing after the commit, saw 0 and 3 teams, equal to the baseline |
| E dead snapshot | After exit a new consumer's `SET TRANSACTION SNAPSHOT` with the old id failed with **22023** (invalid snapshot identifier). The handle accessors raised `SnapshotHandleClosed` |
| F one connection, one transaction | 1 physical connect, 1 BEGIN, every statement on one Connection/DBAPI pair. `now()` was unchanged across the reads (module check). The transaction start equalled the administrator's `xact_start` |
| G refusals | unsupported (PUBLIC grant): `SnapshotRefusedError` with the preflight facts. Wrong target: the existing `DatabaseTargetVerificationError`; the cleanup note honestly reads `not_verified`, because the verifier validates against the same wrong designation. Identity mismatch (another database in the same cluster): `... different sources (database_oid)`. In all three, **no body ran, no S session remained, and L still held the lock** afterwards |
| H failures | **Baseline failure:** a migrator-owned table without a grant; the preflight accepted it and the fingerprint read was denied with 42501. The `ProgrammingError` was preserved and no body ran. **Body failure:** the same object. **S backend lost** (test administrator): `SnapshotSessionLost` at exit. **L backend lost:** `BackupLockNotHeld` at exit, with the shared manager's release note (`PendingRollbackError`), and no reconnection. In every case exactly one physical connect per session, both sessions gone, and no holder |
| I queued DDL | With an ACCESS EXCLUSIVE request confirmed queued on `alembic_version`, the preflight's head read on S failed with **55P03 after 5.19 s** (S's own `lock_timeout`), not the requester's 30 s. No body ran, S was gone, and the request was later cancelled (57014) and never committed |
| J ordering | At the instant L sent `pg_advisory_unlock`, the administrator found **no** S backend and L as the sole holder, on both a normal and a failing exit. Afterwards nothing remained |
| K caller | A separate caller connection kept its transaction (`now()` equal), its `SET LOCAL`, `idle in transaction`, and saw no begin, commit or rollback events |
| L lock additions | L's own identity equalled the administrator's. After `confirm_held()` L was `idle`. After the test administrator terminated L, `confirm_held()` raised `BackupLockNotHeld (OperationalError)`; after exit it raised "not active" |

**Injected behavior, kept separate** (`tests/regression/test_deployment_backup_capture_snapshot.py`, 45 tests):
- ordering and lock-check placement;
- every refusal leaves no export, and no handle is yielded;
- missing or mismatched identity fields;
- a lock lost at each boundary;
- a lost or changed S;
- baseline and body failures;
- transaction drift;
- inputs refused before creation;
- cleanup failures and an unconfirmed cleanup;
- interrupts;
- static checks: no `nextval`, `setval`, advisory, terminate or write statements; reuse; no test import;
- **SQL drift:** the production reader issues exactly the statements, in the same order, as the test-layer `capture_baseline`, run against a recording fake.

**Verified.** `<E>` is `scratchpad/evidence_9b6c`, outside the repository; output, exit codes, JUnit and the review attachment are retained there.

| Command, exactly as executed | Working tree | Clean worktree (`373b354` plus exactly 22 files) |
|---|---|---|
| `python -m pytest tests/regression/test_deployment_backup_capture_snapshot.py tests/regression/test_deployment_backup_capture_preflight.py tests/regression/test_deployment_backup_capture_lock.py -q --junitxml=<E>/snapshot_regression.xml` (the clean run wrote `clean_snapshot_regression.xml`) | **123 passed** (45 + 35 + 43), exit 0 | 123 passed, exit 0 |
| `python -m pytest tests/postgres/test_postgres_backup_capture_snapshot.py tests/postgres/test_postgres_backup_capture_preflight.py tests/postgres/test_postgres_backup_capture_lock.py -q -m postgres -ra -o junit_family=xunit1 --junitxml=<E>/snapshot_postgres.xml` (clean: `clean_snapshot_postgres.xml`) | **40 passed** (13 + 12 + 15), exit 0 | 40 passed, exit 0 |

- No skips, warnings or expected failures occurred in this selection.
- The structural exporter found 14 of 14 required records in both JUnit files, and failed on a negative control with one record removed.
- The lock module changed, so its regression (43) and PostgreSQL (15) modules were rerun in the selection.
- No shared baseline or harness helper changed: `pg_restore_compare.py`, `baseline_document.py`, the preflight and artifact modules, `bulk_import_safety.py` and every harness module are byte-identical to their pre-change hashes. No other accepted suite was rerun.

**Sensitivity check: 7 of 7 caught** by their intended behavioral assertions, with the module restored byte for byte after each.
- Early transaction closure: the consumer's import failed (22023).
- Baseline read on a second Connection: 2 physical connects.
- Wrong isolation (READ COMMITTED, unchecked): S saw the committed row.
- Missing read-only mode: the server reported `off`.
- Source mismatch unchecked: the body ran, "did not raise".
- Lock loss ignored at exit: the wrong exception type.
- Omitted physical close: "an owned snapshot or lock backend is still present on the server".

The first pass counted 6 of 7. Under wrong isolation, the module's own exit check (`SnapshotSessionLost`) had stopped the run before D's visibility assertions. Tests A and D were then restructured to record escaping exceptions and assert server facts first; no expected outcome changed.

**Discrepancies investigated.** A test reordering put a query before `SET TRANSACTION SNAPSHOT`, giving 25001; it was corrected. One test helper's statement-prefix check missed the leading newline of the preflight's inventory SQL; it was corrected. No module behavior changed.

**Limits (stated, not claimed away).**
- One PostgreSQL version and configuration.
- The bounds (S's 5 s `lock_timeout`; the inherited 15 s `statement_timeout`, 15 s connect timeout and 10 s verification) are module constants, not chosen policy. A real fingerprint read on large tables may need a different statement bound.
- Lock checks are boundary observations only.
- S holds ACCESS SHARE on every table it read until it ends (C7's availability trade-off is unchanged).
- The identity-mismatch case used a second database in the same cluster. A `system_identifier` mismatch across clusters was exercised only with fakes.
- The consumer is test instrumentation, not `pg_dump`.
- The baseline reader mirrors the test-layer reads; the test layer stays the comparison authority.

**Manifests.** Reviewed under the documented Phase 9 deployment convention: rows are keyed by notebook function name; a `src/deployment` symbol with no notebook origin gets no row; only a bare-name collision updates a row. The new module's 10 public symbols and `BackupLockNotHeld` have no notebook origin and no collision, so no entry changed (1,156 data rows each, byte-identical). The recorded inconsistency is **preserved, not reconciled**: Phase 5 `src/pipeline` modules received `src_only` rows, while Phase 9 deployment modules receive none.

**Preserved:** C6's original seven expected failures; C10's two; the catalog change-and-reversal and observation-blocking limits; the unresolved identity-test failure; and the rule that a failed R2 (next-ID safety) blocks recovery readiness.

**Smallest subsequent unit (not begun):** run a real `pg_dump --snapshot=<snapshot_id> -Fc` (single process) as the consumer inside the handle's body, to a job-scratch file, plus the end-of-dump checks. It would not write an artifact or restore. That unit would also need decision 6 (adopting the fresh-endpoint observation) before any artifact claims consistency.

### 13.12 Phase 9B-6D: a real `pg_dump` consumer of the snapshot handle (2026-10-05)

**Status.** Implemented, tested and verified **within its tested scope**: one disposable PostgreSQL 16 cluster, synthetic data, the 9B-2 grants, a PostgreSQL **16.15** client, and the scenarios and injected faults below. Successful completion means only that this dump procedure completed and its **basic archive checks** passed. It does **not** establish artifact consistency, data correctness, recoverability or recovery readiness. No finalized artifact, manifest, `COMPLETE` marker, `verification.json` or artifact `FAILED` record is written, and the artifact writer is not used. No restore runs, and no SQL from the archive is executed. Production capture is not implemented, and **Phase 9B remains incomplete.**

**9B-6C source inspected first.** The 9B-6C files are identical to their recorded versions; the editor's "changed on disk" notices were timing artifacts, checked against the retained review attachment. One dependency gap was demonstrated and fixed narrowly. A consumer could not re-confirm S and L while the context was open, and the handle did not retain the exact validated source, so "check before launch and after completion" and "use the same source, no fallback" were impossible. `SnapshotHandle.confirm_active()` now runs the existing exit check on demand: `lock.confirm_held()`, then S's PID, snapshot descriptor and transaction start. The handle also retains, privately, the URL and designation S validated; no public accessor exposes them. Both raise `SnapshotHandleClosed` after exit. The exit check itself is unchanged in substance, and only its message no longer says "at exit". The snapshot tests were rerun in the selection below.

**Interface and ownership contract (implemented, `src/deployment/backup_capture_dump.py`).**
- **Call:** `backup_capture_dump(handle, *, client, dump_timeout_s=600, tool_timeout_s=30)`, a context manager used inside an active snapshot context.
- **Yields a `DumpArchive`:** `archive_path`, which is valid only inside the body; plus `archive_bytes`, `toc_entries`, `toc_sha256`, `pg_dump_version`, `pg_restore_version`, `application_name`, `snapshot_id`, `returncode` and the inventory counts. `cleanup` is set on exit.
- **`ClientHost`, the one seam: where the PostgreSQL client runs.** The production `LocalClientHost` runs local executables with `shell=False`, in a new process group, with every `PG*` variable removed from the environment, and in a private `mkdtemp` directory (0700 on POSIX; on Windows the per-user temp ACL). A host may relocate **only** the network path (`hostaddr`, port) and the location of the same CA file; the module refuses any other change. The production host relocates nothing.
- **Owned, and the only things cleaned up:** the work directory and the files in it (passfile, archive, captured output, TOC); the version-check, `pg_dump` and `pg_restore --list` processes; this dump's own backend, identified by a unique `application_name` (`napsedge_backup_dump_<hex>`), the role and the database; and one short-lived validated verifier session.
- **The only backend ever signalled** is this dump's own. It is ended with `pg_terminate_backend`, which needs no privilege beyond the same role, and only when the client had to be stopped. No grant or privilege was added.

**Invocation.**
- `pg_dump --dbname=<conninfo> --format=custom --snapshot=<the handle's exported id> --no-password --file=<work>/archive.dump`.
- The conninfo carries host, hostaddr, port, dbname, user, `sslmode=verify-full`, `sslrootcert` and `application_name`, each quoted.
- Only `sslmode`, `sslrootcert`, `hostaddr` and `connect_timeout` are accepted from the URL. Anything else (for example `sslcert`, `options`, `target_session_attrs`, a repeated value, a non-numeric timeout) is **refused, never dropped**.
- No `--no-owner`, `--no-acl`, data-only, schema-only, object filter or `-j`.
- The password reaches **only** a passfile: escaped, created exclusively and privately, and removed as soon as `pg_dump` ends, before the offline checks. It never reaches argv, the environment, messages, notes or diagnostics. Both clients must report major 16, and their exact version strings are recorded.

**Order.**
1. `confirm_active()`, before anything is created.
2. The work directory, then both version checks.
3. Localization, the passfile, and `confirm_active()` again.
4. Launch, then a bounded wait.
5. Remove the passfile, check the exit status, then `confirm_active()` **after completion**.
6. A non-empty regular archive file.
7. **Offline** `pg_restore --list --file=<toc> <archive>` (no database option). The table of contents must list exactly the baseline's tables, table data, sequences, sequence values, owners and ACL-bearing relations.
8. Yield.
9. Exit: stop and reap a still-running client (and end its own backend); observe the dump backend's absence with a deadline; remove the passfile if still present; remove the work directory and confirm it is gone.

Every step is attempted even if an earlier one failed. A primary exception is preserved with at most one bounded note. Without one, unconfirmed cleanup raises `DumpCleanupError`. A new interrupt during cleanup propagates after the remaining steps. **Boundary checks of S and L are point observations; they do not establish continuous ownership.**

**Where the client ran in the tests (test layer, stated).** This Windows host has no PostgreSQL client. The tests run the production module with a test-layer `ContainerClientHost`, which runs the cluster image's own PostgreSQL 16.15 `pg_dump`/`pg_restore` through `docker exec` inside the disposable cluster container:
- The work directory is a `0700` directory in the container's `/tmp`.
- The client runs under `env -i`.
- `localize` relocates only `hostaddr`/port to 127.0.0.1:5432 (the server's own address there) and copies the URL's own CA file byte for byte. The host name used for verify-full, the database, the user and the sslmode are unchanged.
- **Stopping kills the real `pg_dump` inside the container by its recorded PID.** A killed client exits with 137, and its absence from `/proc` is confirmed. Killing only the host's `docker exec` client is not accepted as stopping.

The production `LocalClientHost` was exercised with **real processes** (a stand-in client script), not with a real `pg_dump`.

**Server-observed results (retained working-tree run; exported from JUnit).**

| Scenario | Evidence |
|---|---|
| A real dump | `pg_dump (PostgreSQL) 16.15`, exit 0, archive 123,053 bytes, 275 TOC entries. Inventory: 24 tables, 24 table data, 13 sequences, 13 sequence sets, 37 ACL-bearing relations, owners equal to the baseline. The argv shows `--format=custom`, `--snapshot=00000005-00000003-1` and `--no-password`, with no filter, owner or ACL option |
| B in flight | Held by a deterministic barrier (an ACCESS EXCLUSIVE request queued on `conferences` behind S's ACCESS SHARE), the administrator saw the dump backend (PID 145) on the verified database as `napsedge_backup_ro` with its unique application name, inside a transaction, parked in `LOCK TABLE`, with **`backend_xmin` 748 equal to S's exported xmin** (server evidence of the imported snapshot). L (PID 87) was the sole lock holder and S (PID 88) was present throughout |
| C committed marker | A `teams` row committed after S's snapshot and before `pg_dump` started. A fresh reader saw it (1) and S did not (0). **Offline** `pg_restore --data-only --table=teams --file=...` (instrumentation for this one marker) showed 3 COPY rows, equal to the baseline, the seeded control slug present, and **the marker absent** |
| D expired handle | `SnapshotHandleClosed`; the client host recorded **no** operation |
| E invalid snapshot | A well-formed identifier no transaction had exported (test instrumentation on the handle's private slot). `pg_dump` exited 1 with "invalid snapshot identifier"; `DumpFailed(stage="dump")`; nothing yielded; nothing left |
| F deadline | The dump was parked behind the barrier (observed) with an 8 s deadline. `DumpTimeout` after 9.49 s while the barrier was **still queued**. The real `pg_dump` (container PID 858) was killed: exit **137**, gone from `/proc`. Its backend was ended, with no dump backend left; the barrier was later cancelled (57014) and never committed |
| G S lost / L lost | The dump was parked; the test administrator ended S (or L); the barrier was released; **`pg_dump` exited 0**. The post-completion `confirm_active()` raised `SnapshotSessionLost` or `BackupLockNotHeld`; no result was yielded, and the archive, passfile and work directory were removed |
| H failures | Authentication (fault injected at the client host: a wrong password in the passfile; the module's own URL stays valid) gave exit 1, "password authentication failed". TLS (the other CA given to the client) gave exit 1, "certificate verify failed". A missing client (`pg_dump_not_installed`) gave `DumpPreconditionError` from the version check, with no dump started. Each left no process, file or backend |
| I injected cleanup failures | Regression: a failed work-directory removal, a backend still present, an unavailable verifier, or an unconfirmed stop each give a note on a primary failure, or `DumpCleanupError` without one; never success |
| J credentials | The passfile was already gone while the archive was still in use; nothing was left afterwards. No cluster secret appeared in any message, diagnostic or retained evidence. In the regression run the planted password reached only the passfile |
| K caller | A caller's own file (on the host) and its container file were unchanged; a caller connection kept its transaction, its `SET LOCAL` and `idle in transaction`, with no begin, commit or rollback events |

**Real processes through `LocalClientHost`** (regression, stand-in client script):
- the environment reaching the client contained **only** `PGPASSFILE` among `PG*` names, although `PGHOST`, `PGSSLMODE`, `PGPASSWORD`, `PGSERVICE` and others were set in the parent;
- a sleeping client was killed at its deadline and confirmed exited through the Windows process API;
- a nonexistent executable gave `the pg_dump client could not be started (FileNotFoundError)`;
- version 15 was refused;
- exclusive private files refuse to overwrite.

**Verified.** `<E>` is `scratchpad/evidence_9b6d`, outside the repository; output, exit codes, JUnit and the review attachment are retained there.

| Command, exactly as executed | Working tree | Clean worktree (`373b354` plus exactly 23 files) |
|---|---|---|
| `python -m pytest tests/regression/test_deployment_backup_capture_dump.py tests/regression/test_deployment_backup_capture_snapshot.py tests/regression/test_deployment_backup_capture_lock.py -q --junitxml=<E>/dump_regression.xml` (clean: `clean_dump_regression.xml`) | **138 passed** (46 + 49 + 43), exit 0 | 138 passed, exit 0 |
| `python -m pytest tests/postgres/test_postgres_backup_capture_dump.py tests/postgres/test_postgres_backup_capture_snapshot.py tests/postgres/test_postgres_backup_capture_lock.py -q -m postgres -ra -o junit_family=xunit1 --junitxml=<E>/dump_postgres.xml` (clean: `clean_dump_postgres.xml`) | **38 passed** (10 + 13 + 15), exit 0 | 38 passed, exit 0 |

- No skips, warnings or expected failures occurred in these runs.
- An earlier development run of the dump module had 2 warnings: thread exceptions from the failing scenarios fixed below.
- The structural exporter found 17 of 17 required records (10 dump, 7 snapshot) in both JUnit files, and failed on a negative control with one record removed.
- Changed pre-existing files: only `backup_capture_snapshot.py` and its regression module, each keeping its own line-ending convention. The lock module, `bulk_import_safety.py`, the preflight and artifact modules and every harness are byte-identical to their pre-change hashes, so no other accepted suite was rerun.
- Docker Desktop had stopped between units and was started for this one. The existing volume `bonnieswebscraping_napsedge-local-db-data` was not touched, and 0 containers remained.

**Sensitivity check: 11 of 11 caught** by their intended behavioral assertions, with every mutated file restored byte for byte.
- **Omitted `--snapshot`:** failed the **committed-marker archive-data assertion** (the marker was present in the archive); the A test checks archive data before the xmin evidence, so argv inspection is not what decides.
- **Wrong `--snapshot`** (the visibility descriptor): the argv contract.
- **Ignored nonzero exit:** stage `output` instead of `dump`.
- **Ineffective termination, module never stops the client:** exit code not 137.
- **Ineffective termination, the test client kills only the host's `docker exec`:** the real `pg_dump` was not killed.
- **Accepted empty output:** "did not raise".
- **Accepted unreadable or truncated output** (`pg_restore --list` failure ignored).
- **Ignored session loss after completion:** a result was yielded.
- **Leaked partial files:** a work directory remained.
- **Leaked credentials, passfile kept for the archive's lifetime:** seen during the body.
- **Leaked credentials, password in argv.**

**Discrepancies investigated.** All were in tests or tooling; no module behavior changed:
- My first injection of authentication and TLS faults patched the handle's validated URL, which also made the module's own backend verifier fail, so the cleanup note was honestly `not_verified`. The faults are now injected at the client host.
- A test helper leaked one pooled connection per poll (`QueuePool limit`).
- Two ordering assertions matched the version-check processes.
- `URL.set(password=None)` is a no-op in SQLAlchemy, so that case is built with `_replace`.
- In G, an outer `pytest.raises` swallowed an inner "did not raise", so the first sensitivity pass counted 10 of 11. G now records the inner outcome and asserts "no successful result" first.
- The mutation tool compared CRLF bytes with LF anchors; it now normalizes.
- My edit had turned the snapshot module CRLF; it was restored to its original LF.

**Limits (stated, not claimed away).**
- One PostgreSQL version and configuration; one client version (16.15).
- The real `pg_dump` ran inside the server's own container through a test-layer client host. The production `LocalClientHost` has not run a real `pg_dump`.
- On Windows, privacy of the local work directory rests on the per-user temp ACL, and the process "group" is a console process group.
- Boundary checks are point observations.
- The inventory check compares names, owners and ACL presence only, not content or ACL text.
- A readable table of contents is a structural check, not data verification or proof of recoverability.
- Sequence values in the archive are whatever `pg_dump` read and are **not frozen** by the exported data snapshot.
- The timeouts are module constants, not chosen policy.

**Still explicit.**
- Fresh catalog endpoint observations are **not** an adopted production safeguard (decision 6), and **equal catalog endpoints cannot prove stability**.
- A failed R2 (next-ID safety) **blocks recovery readiness**.
- C6's seven and C10's two historical expected failures remain unresolved.
- The existing identity-test failure is unchanged.

**Manifests.** Reviewed under the documented Phase 9 deployment convention: rows are keyed by notebook function name; a `src/deployment` symbol with no notebook origin gets no row; only a bare-name collision updates a row. The dump module's 18 public symbols, and the new handle methods (which are methods, not top-level symbols), have no notebook origin and no collision, so no entry changed (1,156 data rows each, byte-identical). The recorded inconsistency is **preserved**: Phase 5 `src/pipeline` modules received `src_only` rows; Phase 9 deployment modules receive none.

**Smallest subsequent unit (not begun):** disposable restore verification of this unit's archive. Inside the dump context, restore the archive into a fresh, empty, disposable database on a separate disposable cluster prepared by the existing bootstrap; compare it with the handle's baseline using the unchanged comparator and `monotonic_v1` (with R2 blocking); and record the outcome only as a test-layer result. It would write no artifact and claim no recovery readiness.

### 13.13 Phase 9B-6E: disposable round trip of the integrated capture path (2026-10-05)

**Status.** **Tested disposable round-trip behavior, test layer only.** One new module, `tests/postgres/test_postgres_backup_capture_roundtrip.py`. It is **not** a production restore service, an artifact, a verification record or a recovery-ready status. A passing scenario establishes only the observed round-trip result in this setup:
- one disposable PostgreSQL 16 cluster with synthetic data and the 9B-2 grants;
- the cluster image's own PostgreSQL **16.15** `pg_dump` and `pg_restore`, run inside the server's container by the 9B-6D test-layer `ContainerClientHost`. That host relocates the network path and the CA-file location, so **its results do not validate the production `LocalClientHost` route**.

No existing file changed: all 54 pre-change hashes, covering every production capture module, harness, helper and both manifests, are unchanged. Phase 9B remains incomplete.

**Read before relying on 9B-6D's summary.**
- `DumpArchive` carries `archive_bytes` (the archive's **size**) and `toc_sha256`, the digest of the `pg_restore --list` output **only**. 9B-6D records **no archive digest**. This unit computes the archive's SHA-256 itself, separately from the TOC digest and from the baseline file's digest.
- The dump module identifies its own backend by its unique `application_name`, plus `usename = current_user` and the current database, and signals only that backend. The 9B-6D test observer found a parked dump by name prefix, and terminated S or L only by their recorded PIDs.

**Inputs preserved (per scenario).**
1. The archive is produced by the **production** preflight → lock → snapshot → dump path.
2. While the `DumpArchive` is valid, its exact bytes are copied into an independently owned `0700` container directory. The copy's SHA-256 equals the original's (both computed with `sha256sum` before the dump context closes).
3. The exact `BaselineDocument` is written with its established serialization (`canonical_json_bytes(to_document())`) to an owned host file, with its own SHA-256.
4. The dump context, S and L are then closed. The administrator confirmed: no lock, snapshot, verifier or dump session left; L's and S's PIDs gone; no advisory holder; the dump's own work directory gone; the copy present.
5. The handle's accessors raise `SnapshotHandleClosed`.
6. The comparison baseline is then read **only** from the saved file: its digest is re-checked, it is parsed by `parse_baseline`, and it is mapped by the existing pure adapter `baseline_from_document` (no new bridge). **The source is never re-read to manufacture the expected result.** These copies are test inputs, not artifact publication.

**Target and restore.**
- **Target:** created by the existing recipe (`_create_database_owned_by_migrator`) and registered immediately. A guard refuses, before any restore process starts, unless the target:
  - is not the source (by name **and** OID);
  - was created by this case;
  - is owned by the migrator;
  - is empty (the existing `require_destination_database_empty`).
- **Before restore:** `pg_restore --list` and the archive's digest pass the existing `require_archive_ready_for_restore` guard. Both OIDs are recorded; for example, scenario A had source 16389 and target 17052.
- **Restore** uses the accepted recipe exactly: `pg_restore -d <conninfo for the target, user napsedge_migrator, verify-full, a unique application_name> --no-password --exit-on-error --single-transaction <archive copy>`.
  - It runs as the **non-superuser migrator** (`rolsuper` false); the server observed the restore backend's `usename` as `napsedge_migrator` (G).
  - The password comes from the existing private pgpass helper and is removed afterwards.
  - It runs through `ContainerClientHost.start`, so a timeout kills the real in-container process.
  - No `--create`, `--no-owner`, `--no-acl`, trigger disabling, data-only or schema-only mode, or filter.
  - The existing recipe **does** use `--single-transaction`.
  - No migration is run over the restored database.
- Administrative fixture privileges only provision and drop owned targets, and end a timed-out restore's own backend on its own disposable target (by its unique name). They never perform the restore.

**Comparison (independent, fresh transactions).**
- The unchanged `compare_to_baseline` runs against the saved baseline. Every read opens its own connection and transaction on the target.
- **Verdict:**
  - data fingerprints and counts;
  - genuine constraint differences, with only the existing comparator's one permitted CHECK-rendering rewrite allowed;
  - indexes;
  - owners;
  - **exact ACL text** (not the dump inventory's ACL-presence check);
  - the Alembic version;
  - constraint validation.
- **Strict sequence equality** (`pg_sequences.last_value`) is reported separately, and every difference is retained.
- **Policy:** `monotonic_v1` is evaluated separately with the established `evaluate_rules` over `read_sequence_details` (read on the restored target). The policy accepts only when **R0, R1 and R2** hold for every sequence; **R2 failure blocks it** even when strict equality holds; R3 is informational. Nothing calls `nextval` or `setval`, or repairs anything.
- **Provenance is kept separate from content:** the target's OID differs from the source's by design and is not compared as content.

**Results (retained working-tree run; exported from JUnit; clean-worktree run identical).**

| Scenario | Evidence |
|---|---|
| A no change | archive 123,052 bytes, SHA-256 `671223b9…07bf5b` (TOC digest `bcb84877…ec98480`); baseline file `103e7b34…ff86163`; snapshot `00000004-00000003-1`. The restored archive's digest equalled the captured one; restore exit 0 with empty stderr. Every verdict true; strict sequences equal; policy accepts for all 13 sequences (for example `team_aliases_id_seq`: N_b=4, N_r=4, restored maximum 3, R1, R2 and R3 true) |
| B safe advancement | After S's snapshot and before the dump, a migrator inserted a `team_aliases` row with a **sequence-generated id** (4) and committed. A fresh reader saw it (1) and S did not (0); the sequence went from 3 to 4 (fresh read). Restored: the row is **absent**, and data fingerprints equal the captured baseline. **Strict sequence difference retained:** `team_aliases_id_seq` 3 → 4. Policy **accepts** (N_b=4, N_r=5, maximum 3, R2 true). Policy acceptance is not turned into strict equality |
| C equal but unsafe | NEGATIVE-CONTROL SETUP (the C4 fixture, not a repair): an explicit id 1,000,000 inserted on the source, with the sequence unmoved (3, called). After the round trip every verdict is true and **strict sequences are equal**, but `team_aliases_id_seq` has N_b=N_r=4 and restored maximum 1,000,000, so **R2 is false**. **Policy rejected (explicitly)** |
| D data tamper | One `conferences.canonical_name` changed on the target: fingerprint difference for `conferences` only, with the same row count (1) and a different md5 |
| E schema / ACL / ownership | Index added on the target: `index_diff` = `("teams", "zz_roundtrip_tamper")`, with owners and ACLs equal. `REVOKE SELECT ... FROM napsedge_backup_ro` on the target: **ACLs differ**, owners equal. `seasons` given to a registered disposable probe role (fixture privilege, target only): **owners differ** (and ACLs). The target is dropped before the probe role |
| F failed restore | **Damaged archive** (same length, 64 bytes overwritten near the end): `--list` exit **0** (the TOC is readable), restore exit **1** ("could not uncompress data"). **SQL error** (a committed `CREATE TYPE public.teams` the empty-table guard does not see): `--list` 0, restore exit 1 (`relation "teams" already exists`). Both: no comparison, 0 tables committed (single transaction), no process or backend left |
| G deadline | An uncommitted `CREATE TABLE alembic_version` held by the migrator **in the disposable target only** blocked the restore. The server saw the restore backend (PID 2370) on the target as `napsedge_migrator`, waiting on `Lock`. At the 8 s deadline (9.12 s elapsed) the **real `pg_restore`** (container PID 2364) was killed inside the container: exit **137**, gone from `/proc`. Its backend was ended by its unique name, and no restore backend or client process remained |
| H wrong target | The source database (refused: "the target is the source database...") and an unowned database (the harness's bootstrap database; refused: "the target was not created by this case") were both refused **before any restore process started**. The source's fingerprints were unchanged |

**Observed while building F (stated).** Truncating the archive to half made `pg_restore --list` fail. Overwriting 64 bytes at three-quarters of the file, inside the large table of contents, made `pg_restore --list` **crash (exit 139, SIGSEGV)**. Both are nonzero exits, which the dump module's offline check treats as failure. Damage near the end leaves the TOC readable while the restore fails, which is the case F needs.

**Verified.** `<E>` is `scratchpad/evidence_9b6e`, outside the repository; output, exit codes, JUnit and the review attachment are retained there.

| Command, exactly as executed | Working tree | Clean worktree (`373b354` plus exactly 22 files) |
|---|---|---|
| `python -m pytest tests/postgres/test_postgres_backup_capture_roundtrip.py -q -m postgres -ra -o junit_family=xunit1 --junitxml=<E>/roundtrip_postgres.xml` (clean: `clean_roundtrip_postgres.xml`) | **12 passed**, exit 0 | 12 passed, exit 0 |

- No skips, warnings or expected failures.
- The structural exporter found 12 of 12 required records in both JUnit files, and failed on a negative control with one record removed.
- No shared helper changed, so no other accepted module was rerun.
- Containers: 0. The existing Docker volume was not touched.

**Sensitivity check: 9 of 9 caught** by their intended behavioral assertions, with the module restored byte for byte after each.
- Restoring a different archive: "a different archive was restored".
- Comparing with fresh source state instead of the saved baseline: B's fingerprint difference for `team_aliases`.
- Ignoring restore failure: "a failed restore was treated as a success".
- Dropping the data comparison: D.
- Dropping the ACL comparison: E-acl.
- Dropping the ownership comparison: E-ownership.
- Discarding strict sequence differences: B.
- Accepting an R2 failure: C.
- Ineffective timeout cleanup, killing only the host's `docker exec` client: G, the real process was not killed.

The first pass counted 8 of 9. Under "ignoring restore failure", the comparator raised on the empty target before F's assertions ran. F now records any comparison attempt or error and asserts the restore failure first; no expected outcome changed. Two choices of the F damage offset were discarded as described above.

**Gaps that stay explicit.**
- `LocalClientHost` has **not** run real PostgreSQL tools.
- The test-layer client relocates network and certificate paths, so these results do not validate the production local-client route.
- Fresh catalog checks remain **unadopted** (decision 6).
- **Equal catalog endpoints do not prove stability.**
- **Boundary lock checks do not prove continuous exclusion.**
- **Sequence state is not frozen** by the exported data snapshot; B shows it.
- **Timeouts are not an adopted operational policy.**
- A failed R2 blocks recovery readiness.
- C6's seven and C10's two historical expected failures remain, and the identity-test failure is unchanged.

**Round-trip blockers identified (nothing here claims readiness).**
1. **The production client route is unvalidated.** This host has no PostgreSQL client, and the round trip used the test-layer container client. A real run of `LocalClientHost` needs a PostgreSQL 16 client on a job host. That is a provisioning decision, not available without a new dependency.
2. **Strict sequence differences under concurrent capture.** B shows that a normal concurrent insert makes the strict comparator differ (3 → 4) while `monotonic_v1` accepts. 13.5 still withholds recovery-ready on **any** strict difference until it is decided whether a strict `sequences` difference explained by `monotonic_v1` may coexist with claim 4. Until then, every capture taken while writers run is blocked from recovery readiness, by design.
3. **Catalog-observer policy (decision 6).** No production catalog refusal check exists, so an artifact could not yet claim catalog-stable capture.
4. **Recording gaps (13.4).** There is no capture-phase failure recording path, and `catalog_bracket_equal` describes the failed bracket, so no artifact can be written truthfully yet.

**Smallest next steps.**
- **(a) Production client route.** Provide a PostgreSQL 16 client to one disposable Linux job container: a decision on its source image, since the cluster image has the client but no Python. Then run the unchanged dump and round-trip tests with `LocalClientHost` instead of the test-layer host, with no relocation of network or CA paths.
- **(b) Catalog-observer policy.** Decide decision 6: adopt the 12.8 fresh-endpoint observation as a production **refusal** check with its residual risks recorded, or not. If adopted, the smallest unit is a production observation module that reuses the 12.8 queries, run as start and end observations in fresh transactions around the dump, refusing on any difference or timeout. It would claim nothing about stability.

**Manifests.** No production symbol was added or changed, and test modules are not indexed under the documented convention, so no entry changed (1,156 data rows each, byte-identical). The recorded Phase 5 versus Phase 9 convention inconsistency is preserved.

### 13.14 Phase 9B-6F: the bounded fresh catalog observer and refusal check (2026-10-05)

**Status.** Implemented, tested and verified **within its tested scope**: one disposable PostgreSQL 16 cluster, synthetic data, the 9B-2 grants, and the 9B-6D test-layer client. **Adopted for this unit, and only this:**
- the capture is **refused** when the tested catalog observations differ;
- it is **refused** when a required observation, identity verification or cleanup cannot complete;
- equal observations mean only **"no difference observed at these endpoints"**. They do **not** establish continuous catalog stability, exclusion of writers, or artifact consistency.

No finalized artifact, artifact schema, reader or writer changed. Nothing was published, and there is no recovery-ready status. Phase 9B remains incomplete.

**Interpretation corrected (9B-6E, scenario B).** The concurrent-insert scenario showed a strict sequence difference that `monotonic_v1` separately accepts. It does **not** authorize capture with active writers under the controlled-window contract (13.1, 13.3). Not every intervening write necessarily produces a detectable difference, and neither equal catalog observations nor endpoint session counts can prove that writers were absent. Strict sequence differences keep the current conservative outcome (recovery-ready withheld), and a failed R2 blocks readiness.

**Canonical definitions (one authoritative implementation).** `src/deployment/backup_capture_catalog.py` now holds the pure definitions that existed only in the C6 test module: `CatalogObservation` and its per-category SHA-256 digests (over the compact JSON of the rows) with the combined digest, `changed_categories`, `raw_differences` and `catalog_changed`. The C6 module imports them back; the 12.8 candidate module receives them through C6 unchanged. C6 keeps only its own I/O helper (`observe_catalog` over an Engine or a Connection, through the existing `pg_restore_compare` reads) and its harness-only guard.

**Dependency map:**
- the three catalog statements are the **same constants** `backup_capture_snapshot` reads the baseline with (`_CONSTRAINT_SQL`, `_INDEX_SQL`, `_ACL_SQL`, drift-tested statement for statement against `capture_baseline` in 9B-6C);
- identity uses the preflight's statements;
- the session uses the lock module's URL and TLS check, its `NullPool` engine builder with its reconnection guard, and its bounded cleanup verifier;
- `pg_restore_compare` must stay free of application imports, so the keyed-difference helper is a private copy, drift-tested against `pg_restore_compare.keyed_diff`;
- nothing in `src/` imports `tests/`, and coverage is **not** broadened (no role attributes, memberships or other object types).

**Interface (implemented).**
- **`observe_endpoint(database_url, designation, *, label, expected_source)`** returns a `CatalogEndpoint` (label, observation, PID, `backend_start`, transaction start, isolation, read-only, `SourceIdentity`, cleanup verification) or raises `CatalogObservationFailed`:
  - a separate owned physical session (`napsedge_backup_catalog_observer`), never S's snapshot or L's transaction;
  - one **READ COMMITTED, READ ONLY** transaction, set on the connection before it begins and checked from the server. That is the isolation the 12.8 candidate was validated with. Each of the three reads takes its own statement snapshot, and catalog functions render current state, so the endpoint is **not** an atomic catalog snapshot and can be torn by a change between its reads;
  - its own `system_identifier`, OID and name: a missing or different source refuses;
  - bounds: a 15 s connect timeout, `SET LOCAL lock_timeout = 5000`, a 15 s statement timeout, and a 60 s total check;
  - `now()` must be unchanged across the reads;
  - no retry;
  - its session's absence is server-verified, and an unconfirmed cleanup refuses.
- **`require_unchanged(before, after, expected_source)`** returns only a `NoDifferenceObserved`. It raises `CatalogObservationFailed` for a missing, malformed, incomplete, mislabelled, wrong-source, non-distinct or misordered record, and `CatalogChanged` (with categories and raw differences) on any difference.
- Errors keep the established primary-exception, bounded-note and interrupt rules. Messages are fixed text plus type or field names.

**Where the check is invoked.** Only in the **test-layer** guarded flow of `tests/postgres/test_postgres_backup_capture_catalog.py`:
1. Acquire L.
2. `observe_endpoint("before")`, **before** S's snapshot transaction begins.
3. S (baseline), then the real `pg_dump`.
4. Inside the dump body, after `pg_dump` has finished and while S and L are open: `observe_endpoint("after")`, then `require_unchanged(before, after, <L's own identity>)`.
5. Only then the normal path: copy the scratch archive and baseline out, and in scenario A restore and compare.

A refusal propagates out of the dump, snapshot and lock contexts, which clean up under their own contracts. **`backup_capture_lock`, `backup_capture_snapshot` and `backup_capture_dump` are unchanged** and do not call the observer; the standalone dump module is not a guarded capture coordinator, and no coordinator was added.

**Results (retained working-tree run; exported from JUnit; clean-worktree run identical).**

| Scenario | Evidence |
|---|---|
| A no change | L PID 92, before-observer 93, S 95, after-observer 176: four distinct sessions. Before transaction 19:32:54.171 < S 19:32:54.272 < after 19:32:56.693. Identity `(7693268982123651107, 16389, <db>)` on both endpoints, equal to L's. Combined digests equal (`93f5bced…`); `NoDifferenceObserved`. At the check, L held the lock and S was present. Order: before, snapshot, dump, after, check. The normal path proceeded, the restore exited 0, and every strict verdict was true |
| B committed change during the dump | While the real `pg_dump` was parked behind the barrier, `GRANT INSERT ON conferences TO napsedge_runtime_ro` committed (a fresh read shows `runtime_ro=ar`). After digest `8a6adb10…` differed from before `93f5bced…`: `CatalogChanged(["acls"])`, raw difference `("conferences",)` from `…napsedge_runtime_ro=r/…` to `…napsedge_runtime_ro=ar/…`. Order stopped before the check passed: **normal path not entered**, no scratch archive copied |
| C observation blocked | An ACCESS EXCLUSIVE request was confirmed queued on `conferences` behind S's ACCESS SHARE. The after-observer's index read waited and failed after **5.1 s** (its own `lock_timeout`): `CatalogObservationFailed("... did not complete (OperationalError)")`. **No endpoint, no digest and no comparison** exist; normal path not entered |
| D change and reversal (**characterization**) | GRANT then REVOKE, both committed and proven by fresh reads (at 19:33:21.480 and 19:33:21.521), both between the endpoints (before 19:33:19.613, after 19:33:22.314). Endpoints equal, so `NoDifferenceObserved` and the flow proceeded. **This is the known bypass, retained as passing characterization. It is not a safety validation** |
| E freshness | With S open, `GRANT` committed at 19:33:25.976. A SELECT inside S still showed the old ACL, and S's `now()` was its original transaction start (19:33:25.779). The after-endpoint saw the change: `CatalogChanged(["acls"])`. The sessions were distinct, and before < S start < commit < after |
| F identity and missing evidence | An observer on another database of the same cluster: "connected to a different source (database_oid, database_name)". `require_unchanged` refused a missing before, a missing after, a malformed observation, an unconfirmed cleanup, a missing `system_identifier` and swapped labels, each with `CatalogObservationFailed`. The control on the real endpoints gave `NoDifferenceObserved` |
| G failure cleanup | Connection failure (wrong password): "could not connect (OperationalError)", no observer session, password not echoed. Cleanup failure (injected verifier failure): refused with "verification=not_verified" although the server showed the observer gone. Query failure: C's lock timeout. Comparison failure: `CatalogChanged`, normal path not entered. A caller's own connection kept its transaction, `SET LOCAL` and `idle in transaction`, with no begin, commit or rollback events. No observer, verifier or snapshot session remained |

**Verified.** `<E>` is `scratchpad/evidence_9b6f`, outside the repository; output, exit codes, JUnit and the review attachment are retained there.

| Command, exactly as executed | Working tree | Clean worktree (`373b354` plus exactly 26 files) |
|---|---|---|
| `python -m pytest tests/regression/test_deployment_backup_capture_catalog.py -q --junitxml=<E>/catalog_regression.xml` | **39 passed**, exit 0 | 39 passed, exit 0 |
| `python -m pytest tests/postgres/test_postgres_backup_capture_catalog.py -q -m postgres -ra -o junit_family=xunit1 --junitxml=<E>/catalog_postgres.xml` | **7 passed**, exit 0 | 7 passed, exit 0 |
| Accepted modules rerun because definitions moved: `python -m pytest tests/postgres/test_postgres_backup_catalog_consistency.py -q -m postgres -ra` | **120 passed, 7 xfailed** | 120 passed, 7 xfailed |
| `python -m pytest tests/postgres/test_postgres_backup_catalog_candidate.py -q -m postgres -ra` | **143 passed, 2 xfailed** | 143 passed, 2 xfailed |

- The xfails are C6's original **seven** strict expected failures and the candidate's two (change and reversal; observation while an exclusive request is queued), all unchanged.
- There were no skips or warnings.
- The exporter found 7 of 7 records in both JUnit files, and failed on a negative control with one record removed.
- The only pre-existing file changed is the C6 test module (its LF endings kept).
- Containers: 0. The existing Docker volume was not touched.

**Sensitivity check: 7 of 7 caught** by their intended behavioral checks, with every mutated file restored byte for byte.
- Reusing S's stale transaction as the after-observer: B "did not raise".
- Ignoring a difference: B.
- Converting a timeout into an (empty) observation: C saw `CatalogChanged` instead of `CatalogObservationFailed`.
- Accepting incomplete observations: F.
- Skipping source verification: F.
- Continuing the normal path after refusal: B.
- Leaking an observer session: A, where the server still listed the observer.

A was first restructured to assert server-side sessions before an escaping exception.

**Limits.**
- One PostgreSQL version.
- READ COMMITTED endpoints can be torn.
- A change and its reversal is undetected (D).
- Categories other than constraints, indexes and ACL/ownership are not observed.
- The index read can be blocked (C, which refuses).
- The bounds are module constants, not adopted policy.
- The test-layer client still relocates network and CA paths.
- **Artifact-writer integration was not tested.**

**Proposed contract amendment needed before artifact integration (PROPOSED; no schema, reader or writer changed).**
- **What the new records mean.** Add, in a new manifest format version (`napsedge.backup-artifact/2`), a `consistency.catalog_observation` object written only after `require_unchanged` returned `NoDifferenceObserved`:
  - `method: "fresh_endpoints_v1"`;
  - `result: "no_difference_observed"`;
  - per endpoint: the combined and per-category digests, `transaction_start`, isolation and PID;
  - the source identity.

  Its documented meaning is exactly "no difference observed at these endpoints". It is not stability, exclusion or consistency, and it carries the known residual risks (reversal, unobserved categories, torn endpoints, writes that change no catalog row).
- **How incomplete checks are represented.** They are **not** represented inside an artifact. A refused or incomplete check never yields an artifact, so an artifact cannot carry a "check failed" value. A missing `catalog_observation` in a `/2` manifest is a validity failure.
- **Which legacy meaning would change.** `consistency.catalog_bracket_equal` (format `/1`) describes the failed, in-transaction bracket (12.5). Under `/2` it would be **removed**, never reused. `/1` readers stay unchanged, and no `/1` artifact may be reinterpreted as carrying fresh-endpoint evidence.
- **Where capture-phase failures could be recorded.** Today a `FAILED` record can only exist inside an owned `.partial` directory, which the writer creates only when it writes the finished archive (13.4). Two options, both needing a decision:
  - (i) a writer entry point that creates the owned `.partial` at capture start, so the existing classes `catalog_changed`, `timeout`, `snapshot_unavailable`, `source_identity_mismatch` and `dump_failed` can be recorded there;
  - (ii) a separate, bounded capture-run outcome record outside the artifact store, with the same fixed classes, never readable as an artifact.

**Local client gap: read-only inventory (nothing pulled or installed).**
- Host: no `pg_dump`, `pg_restore` or `psql`.
- `postgres:16-alpine` (digest `721873c3…`, the only local PostgreSQL image): `pg_dump (PostgreSQL) 16.15`, **no `python3`** (Python appears only in its build-time dependencies).
- `napsedge-api:phase9a` (Debian 13, from `python:3.13-slim`): Python 3.13.16, SQLAlchemy 2.0.52, psycopg 3.3.6, **no `pg_dump`** (only `libpq-dev`).
- WSL Ubuntu: Python 3.14.4, **no `pg_dump`**.

These probes used throwaway `--rm --network none` containers from local images, plus a read-only WSL command. **No existing environment can exercise `LocalClientHost` with real PostgreSQL 16 tools and the project's Python.** The 9B-6D/E/F container client results do not validate `LocalClientHost`.

**Recipe for the next unit (not executed).** A disposable test-tooling image, used only by tests and then removed:
- `FROM napsedge-api:phase9a`, pinned by its local image ID `bea827f062a0`. This keeps the deployment's own interpreter and dependencies.
- As root, add the PostgreSQL Global Development Group apt repository for the image's Debian release, verifying the signing key `ACCC4CF8` by its full fingerprint before use. Install `postgresql-client-16` **pinned to 16.15** (the server's version), with no other package, then remove the apt lists.
- Label it `org.napsedge.purpose=disposable-test-tooling`.

Its provenance is the local image plus the PGDG-signed client package; this is the only new download, and it is a test-tooling dependency, not an application one. `pytest` would come, as in the 9B-5A/5B Linux runs, from a read-only mount of this machine's site-packages: no install.

Run it as `docker run --rm --network container:<disposable cluster id>`, with the repository and the CA file mounted read-only. Sharing the cluster's network namespace means the URL's host name `localhost` (verify-full), port 5432 and CA path are used **as is**: `LocalClientHost` relocates nothing. A small driver would then run the unchanged dump, catalog and round-trip flows with the production `LocalClientHost`.

Cleanup: the containers are `--rm`; afterwards run `docker image rm` by label and confirm absence; never touch `bonnieswebscraping_napsedge-local-db-data`. **Downloading the client package is a new test-tooling dependency and needs approval before that unit.**

**Remaining artifact-contract blockers.**
1. The amendment above (the `/2` manifest field, removal of `catalog_bracket_equal`, and a capture-phase failure-recording path) is proposed, not adopted.
2. The readiness treatment of strict sequence differences under `monotonic_v1` stays conservative and undecided (13.5).
3. The production `LocalClientHost` route is unvalidated.
4. Timeouts are not an adopted operational policy.
5. Artifact-writer integration of a guarded capture has not been designed or tested.

**Next unit (concrete):** with approval for the PGDG `postgresql-client-16` test-tooling download, build the disposable image above and run the unchanged dump, catalog and round-trip scenarios through `LocalClientHost` with no path relocation. Then remove the image and record the result as `LocalClientHost` validation within that tested scope.

**Manifests.** Reviewed under the documented Phase 9 deployment convention. The new module's 14 public symbols have no notebook origin and no collision, so no entry changed (1,156 rows each, byte-identical). The Phase 5 versus Phase 9 inconsistency is preserved.

**Still explicit.**
- Fresh catalog checks are a refusal check only.
- Equal endpoints prove nothing about stability.
- Boundary lock checks do not prove continuous exclusion.
- Sequences are not frozen by the snapshot.
- C6's seven and C10's two historical expected failures remain, and the identity-test failure is unchanged.

### 13.15 Phase 9B-6G: `LocalClientHost` validated with real PostgreSQL 16 tools on a Linux runner (2026-10-05)

**Status.** Tested and verified **within its tested scope**: the production `LocalClientHost` class has now run the real PostgreSQL **16.15** `pg_dump` and `pg_restore --list` through its own `shell=False` subprocess path, with **no client-side relocation**, in one disposable Linux runner:
- Debian 13 (trixie), amd64, Python 3.13.16;
- the application image's SQLAlchemy 2.0.52 and psycopg 3.3.6;
- a disposable PostgreSQL 16 cluster with synthetic data and the 9B-2 grants.

It does **not** validate the Windows execution path of `LocalClientHost` (still unvalidated), and it does not establish artifact consistency, recoverability or readiness. **No production code changed:** native execution exposed no production defect. Phase 9B remains incomplete.

**Disposable runner (test tooling only; never published; removed after the unit).**
- **Base:** the local `napsedge-api:phase9a`, image ID `sha256:bea827f062a02afa6868768a0c33618aa4e9ace6f732f3eb4a16afcd611ef0e3`, verified before the build. Its layers were confirmed as an exact prefix of the runner's.
- **PostgreSQL client:** the official PGDG apt repository for `trixie`. The signing key was fetched with Python and **refused unless its OpenPGP v4 fingerprint is `B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8`** (key file SHA-256 `0144068502a1eddd…`); apt's own signature and checksum verification was left on.
  - **Resolved before building** (`apt-cache madison` in a throwaway container): `postgresql-client-16` **16.15-1.pgdg13+2** (the previously tested 16.15 was available), with `postgresql-client-common` 293.pgdg13+1.
  - No server package was installed.
  - `pg_dump` and `pg_restore` resolve to the real ELF executables in `/usr/lib/postgresql/16/bin`, which is first on `PATH`, ahead of Debian's `pg_wrapper`. Both report `16.15 (Debian 16.15-1.pgdg13+2)`.
- **Docker CLI:** Debian's own `docker-cli` 26.1.5+dfsg1-9+deb13u1 (API 1.45, within the daemon's 1.40–1.55; daemon 29.7.2), because the existing fixtures shell out to `docker exec`. Its one dependency was `sensible-utils` 0.0.25.
- **Test packages:** the host's exact pins, installed with a pip report and recorded wheel SHA-256s: pytest 9.1.1, pluggy 1.6.0, iniconfig 2.3.0, Pygments 2.19.2, testcontainers 4.15.0, docker 7.2.0, wrapt 2.4.1, python-dotenv 1.2.1. Nothing else was upgraded, and no Windows environment was mounted.
- **No repository change for the image:** requirements files, constraints files, lockfiles and the application Dockerfile are unchanged; the build files live outside the repository and are in the review attachment.
- **Identity:** tag `napsedge-9b6g-runner:20261005160027`, ID `sha256:1eccb59f…`, label `org.napsedge.unit=9b6g`. The base image's inherited `com.docker.compose.*` labels were overridden on every runner container (`com.docker.compose.project=napsedge-9b6g-runner-only`).

**Native execution and routing.**
- **Runner:** `docker run --rm --user 0:0 -v /var/run/docker.sock:/var/run/docker.sock` (the existing fixtures start the cluster through Docker; this is the smallest test-only arrangement, and no Docker Desktop feature or host networking was changed).
- **Mounts:** the repository read-only at `/repo`; a scratch evidence directory at `/evidence`.
- **Environment:** `-e NAPSEDGE_TEST_CLIENT=local -e TESTCONTAINERS_RYUK_DISABLED=true` (the existing ledger removes and verifies its own containers).
- **Canonical source URL:** the runner and the cluster share Docker's default bridge network. Only under the test-only selection, `launched_cluster` builds **one** URL: host `localhost` (so verify-full still checks the certificate's `DNS:localhost` SAN), `hostaddr` = the cluster's bridge address (observed `172.17.0.3`), port 5432, and the runner's own generated CA file. L, S, the observers and `pg_dump` all received exactly that URL.
- **No relocation:** the recorded `pg_dump` invocation's conninfo was compared field by field with the URL (host, hostaddr, port, dbname, user, sslmode, sslrootcert), and `LocalClientHost.localize` returned the target unchanged.

**Native-selection mechanism (test-only).** `NAPSEDGE_TEST_CLIENT`, read only by the new `tests/postgres/capture_client_support.py` and by the guarded branch of `launched_cluster`; no production code reads it (a regression test asserts this).
- **Default (unset or `container`):** unchanged; the historical `ContainerClientHost`.
- **`local`:** production code receives the real `LocalClientHost` instance, **exactly that class** (asserted in A), with the 16-bin tool paths.
- **Test-side companion `NativeTestClient`:** never passed to production code, overrides no `ClientHost` behavior. It gives the existing assertions their inspection points:
  - a recording `subprocess.Popen` **subclass** (argv, environment, PID and return code; otherwise plain `Popen`);
  - local `/proc` scans;
  - local shell helpers;
  - test-instrumentation processes for the restore and the offline extraction, started with the same `shell=False`, new-session and process-group-kill pattern.

  The restore is therefore a test-layer local `pg_restore` (as the migrator), not a `LocalClientHost` call.

**Scenario applicability.**

| Existing scenario | In local mode |
|---|---|
| dump A/B/C/J (real dump, server-evidenced snapshot import, committed marker absent, credentials) | **exercised unchanged**, plus local-only assertions: `LocalClientHost` exact class, real ELF `pg_dump` at `/usr/lib/postgresql/16/bin`, invocation equal to the canonical URL |
| dump D expired handle; E invalid snapshot; G S-loss and L-loss | **exercised unchanged** |
| dump E/H missing client | **transport-only adaptation:** a real `FileNotFoundError` from `LocalClientHost`'s own start ("could not be started"), instead of the container's version-check refusal |
| dump E/H authentication, TLS | **transport-specific fault injection** (a `ContainerClientHost` subclass rewrites the passfile or CA): **explicitly skipped** in local mode (`-ra` shows the reason); retained as container-transport evidence, which passed again in this unit's default run. Never relabelled as native validation |
| dump F deadline | **exercised**; transport-only: the SIGKILL exit status is −9 locally (137 through `docker exec`). Added for both transports: passfile mode 600 and work-directory mode 700, observed while the real `pg_dump` was parked |
| dump K caller | **exercised**; transport-only: the client-side sentinel is a caller-owned file in this runner |
| catalog A–G | **exercised unchanged** (production client passed through) |
| round trip A–H | **exercised unchanged assertions**; transport-only: scratch directory, private pgpass and the restore conninfo are local (the restore conninfo carries the same canonical fields, quoted by the production rule); killed exit status −9 |

**Native results (retained working-tree run; exported from JUnit; clean-worktree run identical).**
- **Dump A:**
  - `pg_dump (PostgreSQL) 16.15 (Debian 16.15-1.pgdg13+2)`, exit 0;
  - archive 123,072 bytes, 275 TOC entries; inventory 24 tables, 24 table data, 13 sequences, 13 sequence sets, 37 ACL relations;
  - **the server saw the dump backend** (PID 91) on the verified database as `napsedge_backup_ro`, parked in `LOCK TABLE`, with `backend_xmin` 748 equal to S's exported xmin;
  - the committed marker was absent from the archive, the control slug present, and 3 team rows;
  - the passfile was gone while the archive was still in use;
  - no leftover process or directory.
- **Dump F:** the real local `pg_dump` (PID 60) was parked behind the barrier, the passfile was **mode 600** and the work directory **700**, and `DumpTimeout` came after 8.04 s with the barrier still queued. The process ended by **SIGKILL (−9)** and was gone from `/proc`; its backend was ended, and no backend remained.
- **Dump E:** an invalid snapshot gave exit 1 and `DumpFailed`. A missing executable gave `DumpPreconditionError("the pg_dump client could not be started (FileNotFoundError)")`.
- **Dump G:** `pg_dump` exited 0, yet `SnapshotSessionLost` / `BackupLockNotHeld` meant no result was yielded.
- **Catalog:**
  - A: the check passed and the normal path proceeded;
  - B and E: `CatalogChanged(["acls"])`, normal path not entered;
  - C: `CatalogObservationFailed` after 5.06 s;
  - D: the change and reversal stayed undetected (characterization);
  - F and G: as in 13.14.
- **Round trip** (the native-produced archive, its SHA-256 recorded separately from the baseline file's, restored as the non-superuser migrator into guarded targets):
  - A: equal everywhere, policy accepts (for example archive `e3a6a085…`, baseline `1a16980e…`, source OID 16389, target OID 17052).
  - B: the row is excluded; the strict sequence difference `team_aliases_id_seq` 3 → 4 is retained; the policy accepts.
  - C: strict equality, R2 false (`team_aliases_id_seq`), **policy rejected**.
  - D/E: the tampering was detected.
  - F: a damaged archive (TOC readable) and an SQL error each gave restore exit 1 and no comparison.
  - G: the blocked restore backend (`napsedge_migrator`, `Lock`) and the **local** `pg_restore` (PID 321) were killed (−9), gone from `/proc`, and no restore backend remained.
  - H: the wrong targets were refused before any process.

**Verified.** `<E>` is `scratchpad/evidence_9b6g`, mounted at `/evidence`.

| Command, exactly as executed | Result |
|---|---|
| `docker run --rm --name napsedge-9b6g-runner-wt --label org.napsedge.unit=9b6g --label com.docker.compose.project=napsedge-9b6g-runner-only --user 0:0 -v /var/run/docker.sock:/var/run/docker.sock --mount type=bind,source=<repo>,target=/repo,readonly --mount type=bind,source=<E>,target=/evidence -w /repo -e NAPSEDGE_TEST_CLIENT=local -e TESTCONTAINERS_RYUK_DISABLED=true napsedge-9b6g-runner:20261005160027 python -m pytest tests/postgres/test_postgres_backup_capture_dump.py tests/postgres/test_postgres_backup_capture_catalog.py tests/postgres/test_postgres_backup_capture_roundtrip.py -q -m postgres -ra -o junit_family=xunit1 --junitxml=/evidence/native_postgres.xml -p no:cacheprovider` | **27 passed, 2 skipped**, exit 0 |
| the same in a clean worktree of `373b354` plus exactly 26 files (`--name napsedge-9b6g-runner-clean`, `--junitxml=/evidence/clean_native_postgres.xml`) | 27 passed, 2 skipped, exit 0 |
| default transport on Windows (no selection): the same three modules, `--junitxml=<E>/default_container_postgres.xml` | **29 passed**, exit 0 (the historical transport is unaffected) |
| `python -m pytest tests/postgres/test_postgres_fresh_cluster_recovery.py -q -m postgres -ra -p no:cacheprovider` (the changed harness owner, default transport) | 36 passed, exit 0 |
| `python -m pytest tests/regression/test_capture_client_support.py -q` | 8 passed, exit 0 |

- **The 2 skips** are exactly the declared transport-specific authentication and TLS fault injections; the exporter fails on any other skip.
- No warnings and no expected failures in these selections.
- The structural exporter found 27 of 27 required records in both native JUnit files, each produced by the local transport.
- **First native attempt:** 10 round-trip failures from one test bug of mine. An evidence helper read the module-level `case` fixture object instead of a parameter; it would have broken the default transport too. It was fixed, and an AST scan found no other such read.

**Native sensitivity check: 6 of 6 caught**, each run inside the runner and each failing its intended assertion, with every mutated file restored byte for byte:
- a `LocalClientHost` **subclass** passed instead of the class: A's exact-class assertion;
- Debian's `pg_wrapper` script instead of the real binaries: A's ELF check;
- client-side CA relocation inside `LocalClientHost.localize`: A's canonical-invocation check;
- timeout cleanup that never kills the local process: F's SIGKILL check;
- an ignored restore failure: round-trip F;
- an accepted R2 failure: round-trip C.

**Limits.**
- One Linux runner (glibc, amd64) and one PostgreSQL version.
- The runner reaches the cluster over Docker's bridge, so a production host's network path is not exercised.
- Windows `LocalClientHost` remains unvalidated (its POSIX-only process-group kill branch is `os.killpg`; Windows uses `kill()`).
- The restore is test instrumentation, not a production service.
- The runner had root and the Docker socket; it is disposable test tooling only.

**Still explicit.**
- Catalog refusal checks are invoked only in the test-layer guarded flow (13.14).
- Equal endpoints do not establish stability.
- Lock observations do not establish continuous exclusion.
- Sequence state is not frozen by the exported snapshot.
- Strict sequence differences keep the conservative readiness outcome.
- A failed R2 blocks readiness.
- C6's seven, the candidate's two and C10's two historical expected failures remain, and the identity-test failure is unchanged.
- The proposed `/2` schema and the capture-failure recording mechanism are **not** adopted here.

**Recommendation for the next artifact-contract unit.**
1. **Adopt the `/2` manifest amendment of 13.14** as a reviewed specification change first, before any code: `consistency.catalog_observation` written only after `NoDifferenceObserved`; removal (never reuse) of `catalog_bracket_equal`; unchanged `/1` readers.
2. **Capture-failure recording: choose option (i)**, the writer creating the owned `.partial` (with `RUNNING`) at capture start. The existing `FAILED` classes (`snapshot_unavailable`, `dump_failed`, `catalog_changed`, `timeout`, `source_identity_mismatch`) then have a home that the verifier already treats as `NOT_FINAL` and never adoptable, reusing the implemented, tested `FAILED` writer and `.partial` lifecycle instead of inventing a second record store.

   Option (ii), a separate run-outcome record, would duplicate that lifecycle and need its own retention and verifier rules.
3. **Then a guarded capture coordinator** that composes the existing lock, snapshot, catalog and dump modules (only in the order of 13.14) and hands the scratch archive to the writer. It would be tested in both transports, still without a recovery-ready status.

**Manifests.** No production symbol was added or changed, and test modules are not indexed under the documented Phase 9 convention, so no entry changed (1,156 rows each, byte-identical). The recorded Phase 5 versus Phase 9 inconsistency is preserved.

### 13.16 Phase 9B-6H: manifest `/2` and the early owned-partial reservation (2026-10-05)

**Status.** Implemented, tested and verified **within its tested scope** (synthetic filesystem and contract tests on Windows; no database, Docker or capture). This **adopts** the 13.14 amendment as written below. It adds no capture coordinator, executes no production-source capture, publishes no user artifact, and adds no restore or readiness state. Phase 9B remains incomplete.

**Identifiers, as found in the repository (not invented).**
- Manifest formats: `napsedge.backup-artifact/1` (`MANIFEST_FORMAT`, unchanged) and the new `napsedge.backup-artifact/2` (`MANIFEST_FORMAT_V2`). Baseline `napsedge.backup-baseline/1`, marker `napsedge.backup-complete/1`, `RUNNING` `napsedge.backup-running/1` and `FAILED` `napsedge.backup-failed/1` are **unchanged**.
- `FAILED` reason classes (`FAILED_REASON_CLASSES`, unchanged, 13): `dump_failed`, `snapshot_unavailable`, `baseline_failed`, `checksum_mismatch`, `source_identity_mismatch`, `schema_head_mismatch`, `catalog_changed`, `timeout`, `storage_full`, `interrupted`, `internal_error`, `destination_exists`, `durability_failed`.
- Observer facts: `CATEGORIES = (constraints, indexes, acls)`; per-category SHA-256 digests plus `combined = sha256("c|i|a")`; cleanup verification `closed_and_released` (`VERIFIED_CLOSED`); endpoints READ COMMITTED, read-only `on`.

**Adopted amendment A: manifest `/2`.** Identical to `/1` except in `consistency`.

| Field | Rule |
|---|---|
| `consistency.catalog_bracket_equal` | **absent** in `/2` (`FIELD_UNKNOWN` if present). In `/1` it keeps its historical meaning (12.5) and is never reinterpreted |
| `consistency.catalog_observation` | **required** object (`FIELD_MISSING` if absent; `FIELD_TYPE` for a bare boolean, string or other non-object) |
| `.method` | exactly `fresh_endpoints_v1` |
| `.result` | exactly `no_difference_observed` |
| `.categories` | exactly `["constraints","indexes","acls"]`, in that order |
| `.source` | `system_identifier` (decimal string), `database_oid` (int >= 0), `database_name` (identifier); each must **equal** the manifest's `source` field of the same name |
| `.endpoints` | exactly two objects, `before` then `after`, each with exactly: `label`, `backend_pid` (int >= 1), `backend_start`, `transaction_start` (timestamps), `isolation` = `read committed`, `read_only` = `on`, `cleanup_verification` = `closed_and_released`, `digests` {`constraints`, `indexes`, `acls`, `combined`} (64 lower-case hex each) |

Cross-field rules:
- for each endpoint, `combined` is the digest of the three category digests, and `transaction_start >= backend_start`;
- the two endpoints' digests are equal (anything else is not a no-difference result);
- the endpoints are distinct transactions (`(backend_pid, backend_start, transaction_start)` differ), and `after.transaction_start >= before.transaction_start`.

Every other `/1` rule applies unchanged. No reason code was added: violations use `FIELD_MISSING`, `FIELD_UNKNOWN`, `FIELD_TYPE`, `FIELD_VALUE`, `TIMESTAMP_INVALID` and `UNSUPPORTED_VERSION`.

- **Version dispatch.** `parse_manifest` and the verifier accept exactly `/1` and `/2`; any other `napsedge.backup-artifact/*` is `UNSUPPORTED_VERSION` (the two existing tests that used `/2` as their unsupported example now use `/3`). The typed manifest's version is **derived** from the consistency record's type (`ConsistencyClaim` gives `/1`, `ConsistencyClaimV2` gives `/2`; any other type is refused before a field is read), so `/1` and `/2` content cannot be mixed when building or writing.
- **Derivation.** `catalog_observation_record(result)` (new `src/deployment/backup_capture_artifact.py`) accepts only a `NoDifferenceObserved`. It re-runs `require_unchanged` on the result's own endpoints and refuses digests that are not the endpoints' own. It copies only facts the observer obtained, renders server times in UTC (naive datetimes are refused), and validates the record through the contract. The capture modules are unchanged and remain unaware of artifacts.
- **Meaning and boundary.** The record means only "no difference observed at these two endpoints by the named method". It is not stability, exclusion or consistency. Structural validation checks shape and internal agreement only. It does **not** authenticate the observations or prove that the caller performed them; anyone who can rewrite the files can forge a well-formed record. Endpoint times come from the server's clock and are **not** compared with `created_utc`/`finished_utc` (a different clock).
- **Unchanged.** The baseline schema, the payload files, `COMPLETE`, the strict comparator and every `/1` reader behavior. No restore-success or recovery-ready field exists in either version.

**Adopted amendment B: early reservation (`backup_artifact_writer.py`).** `reserve_artifact(parent_directory, *, artifact_id, logical_name, created_utc, durability=REQUIRED, ops=None) -> ArtifactReservation`.

| Step or state | Behavior |
|---|---|
| validation (nothing created) | store path by the verifier's component-wise inspection (links refused); `artifact_id`, `logical_name` and `created_utc` by the contract's own validators and directory-name grammar; destination and `.partial-<id>` absent; under `REQUIRED`, the directory-flush probe. Any failure: `PreconditionError`, nothing created |
| ownership begins | exclusive `mkdir` of `.partial-<id>` (an existing entry: `PreconditionError` `PARTIAL_EXISTS`, nothing touched); POSIX mode check; `RUNNING` written and flushed. A failure after the `mkdir` records `FAILED` and raises `WriteFailed`, exactly as the one-shot writer does |
| `reserved` | holds only `RUNNING`. No source fact, baseline or archive is required or recorded. Verifier: `NOT_FINAL` (by hand under the final name: `ARTIFACT_RUNNING`) |
| `record_failure(reason_class, primary=None)` | only from `reserved`, with an enumerated class (anything else: `ValueError`, reservation unchanged). Writes the existing four-key `FAILED` record, which supersedes `RUNNING`, giving state `failed`. Returns `"recorded"` or `"failed: <Type>"` and **never raises because recording failed**. On a recording failure it adds one sanitized note to `primary`, whose object, cause, notes and traceback are otherwise untouched |
| `finalize(archive_path, metadata, baseline)` | only from `reserved`; the metadata must name the reserved `artifact_id`, `logical_name` and `created_utc`. Then **the one-shot writer's own code** runs: archive stream, baseline, manifest, `RUNNING` removal, `COMPLETE`, staged validation with the final name, publication, parent flush. Outcome `finalized`. A refusal or failure before the rename records `FAILED` and raises `WriteFailed` (a refused finalize precondition is `internal_error`, stage `finalize_plan`), giving `failed`; at or after the rename, `PostPublicationError`, giving `publication_uncertain` |
| after any outcome | single-shot: every further call raises `ReservationStateError` and touches nothing |
| dropped / abandoned | no context manager, finalizer or `__del__`: nothing finalizes, records or deletes implicitly. `RUNNING` stays, the partial is `NOT_FINAL`, never adopted, and blocks its id |

- **One-shot writer.** `write_artifact` keeps its signature, order, outputs and failure semantics. It now shares the staging (`_create_staging`, `_begin_staging`), source-descriptor (`_Source`) and completion (`_complete`) code with the reservation. Compared with the pre-change writer on identical `/1` inputs (success and three injected flush failures), the outcome, all file bytes and the operation sequence were identical.
- **Ownership.** Of two competing reservations, exactly one creates the directory. The other gets `PARTIAL_EXISTS` after a single creation attempt and changes nothing. A partial or destination owned by anyone else is never overwritten, adopted, renamed or deleted. The path, symlink and no-replace publication rules are unchanged.
- **Retention.** Failed and abandoned partials are retained (12.1's policy); no cleanup was added.
- **`COMPLETE`.** *(The failed-partial part of this bullet is superseded by 13.17: the marker this run wrote is now removed before `FAILED`.)* Still means files finalized only (claim 1); recording a capture failure can never produce it. One established writer behavior is retained and stated: a failure after the marker step (validation, or a refused or failed rename) leaves `COMPLETE` beside `FAILED` in the partial. That partial is `NOT_FINAL`, or `CONFLICTING_MARKERS` if renamed by hand, and is never adopted. A crash at that point cannot be prevented from leaving the marker.

**Capture-failure mapping (`capture_failure_reason`, by exception TYPE only; no message, SQL, URL or credential is read or recorded).**

| Primary failure | `FAILED` class |
|---|---|
| any `BaseException` that is not an `Exception` (`KeyboardInterrupt`, `SystemExit`) | `interrupted` |
| `CatalogChanged` | `catalog_changed` |
| `DumpTimeout` | `timeout` |
| `DumpFailed`, `DumpPreconditionError` | `dump_failed` |
| `SnapshotSetupError`, `SnapshotSessionLost`, `SnapshotRefusedError` | `snapshot_unavailable` (including a lock/snapshot source mismatch detected inside the snapshot unit, which that type does not distinguish) |
| `DatabaseTargetVerificationError` | `source_identity_mismatch` |
| `CatalogObservationFailed`, `DumpCleanupError`, `SnapshotCleanupError`, `SnapshotHandleClosed`, `BackupLockNotHeld`, `BackupLockSessionLost`, `BackupLockCleanupError`, anything else | `internal_error`. An incomplete observation or an unconfirmed cleanup is **never** `catalog_changed` |
| `AdvisoryLockUnavailableError` | **none**: an overlap is `SKIPPED:overlap`, not a failed capture, and no reservation exists then (the lock precedes the reservation) |

- The `FAILED` schema is unchanged. No class was genuinely required: an observation, cleanup or lock failure is recorded as `internal_error`, which is accurate but coarse; the job log carries the type.
- A durable record is never promised where the filesystem prevents it: a failed recording leaves `RUNNING`, so the partial is still unadoptable.
- No `FAILED` record can exist before the exclusive creation succeeded.

**Coordinator order (documentation for the next unit; NOT implemented).** *(Superseded by 13.17's corrected order and interface: the before-observation precedes S, and publication happens only after the dump, snapshot and lock contexts exit successfully.)*
1. Validate the request; **acquire L** (`backup_capture_lock`). An overlap returns `SKIPPED:overlap`, and nothing is created.
2. `created_utc` = now. `reserve_artifact` (destination and id validated before anything is created).
3. Inside the ownership scope: `backup_capture_snapshot(lock, ..., artifact_id=...)` (S and the baseline); `observe_endpoint(before)`; `backup_capture_dump(handle, client=...)`; `observe_endpoint(after)`; `require_unchanged`; `catalog_observation_record`.
4. Still inside the dump context (its scratch archive exists only there): `finished_utc` = now, compose `ArtifactMetadata` with `ConsistencyClaimV2`, then `reservation.finalize(archive.archive_path, metadata, handle.baseline)`.
5. Any failure after step 2 while `state == "reserved"`: `reservation.record_failure(capture_failure_reason(exc), exc)`, then re-raise the **same** primary. A writer error from `finalize` has already recorded itself.
6. The lock, snapshot and dump contexts exit in reverse order with their own verified cleanup.

**Smallest coordinator interface enabled by this contract.**
- `run_guarded_capture(request, *, client, ops=None) -> PublishedCapture | SkippedOverlap`, where:
  - `request` holds the store, `database_url`, designation, expected source identity, expected Alembic head, `logical_name`, a fresh `artifact_id`, repository and tooling facts, and the durability mode;
  - capture failures are raised (the primary, after recording), not returned.
- Acceptance criteria:
  - (1) an overlap creates nothing;
  - (2) each mapped failure, injected at each step, leaves exactly one `FAILED` partial with the mapped class and re-raises the identical primary;
  - (3) a failure of the recording itself keeps the primary;
  - (4) success publishes a `/2` artifact that the public verifier accepts, whose observation equals the result's endpoints, with the same baseline and archive bytes;
  - (5) no `COMPLETE` without an explicit successful `finalize`;
  - (6) both client transports;
  - (7) every lock, snapshot, observer and dump session and file is verified gone on every path;
  - (8) no recovery-ready status, and the monotonic and strict sequence outcomes unchanged.

**Tested.** `tests/regression/test_deployment_backup_artifact_v2.py`: 126 tests, synthetic data, judged by the public verifier.
- **/1 and /2:**
  - A: `/1` parse, bytes, rejections and writer output against an independent oracle;
  - B: a valid `/2` document, directory and write; the bridge's record equal to an independently built one; UTC rendering; baseline and payload bytes identical between `/1` and `/2`;
  - C: 36 parametrized rejections (missing, boolean, empty, mixed legacy/new, unknown version, method, result, categories, endpoint count, order and completeness, unequal or underived digests, isolation, read-only, cleanup, same or reversed transactions, PID, timestamps, the three source fields, an incomplete source), the `/1` bracket relabelled `/2` (not reinterpreted), plus the bridge's refusals (non-results; incomplete, changed, foreign, naive or tampered results);
  - D: claims and fields.
- **Reservation and failure recording:**
  - E: reservation without inputs, invalid inputs, stores and durability;
  - F: early failure;
  - G: 17 mapped exceptions, each recorded with no planted secret reaching the store;
  - H: record-I/O failure with the primary's identity, cause, traceback and notes preserved;
  - I: abandoned and interrupted partials;
  - J: collisions, a real two-thread race and unowned paths;
  - K: `finalize` byte-identical to `write_artifact` for `/1` and `/2`, metadata-mismatch refusals, failure classification and notes;
  - L: interrupts.
- **Drift and static checks.**

**Verified (Windows 10, Python 3.13.3, pytest 9.1.1).**
- **Focused run:** `python -m pytest tests/regression/test_deployment_backup_artifact_v2.py tests/regression/test_deployment_backup_artifact.py tests/regression/test_deployment_backup_artifact_writer.py tests/regression/test_deployment_backup_capture_catalog.py -q -ra -p no:cacheprovider -o junit_family=xunit1 --junitxml=...`: **784 passed, 13 skipped**, exit 0 (JUnit 797, 0 failures).
- **Clean worktree** of `373b35452bb47f71d5f85cbca1e0624a9e4139f3` plus 20 intended files (byte-identical to the working tree): the same result.
- **Before the change**, the same artifact, writer and catalog modules gave 658 passed, 13 skipped. Afterwards they still pass once the two `/2`-as-unsupported examples were moved to `/3`.
- **Dependent capture regression** (dump, snapshot, lock, preflight): 173 passed.
- **The 13 skips are platform skips, unchanged:** 7 symlink tests (this user cannot create symlinks); 1 POSIX `lstat` fact; 1 POSIX permission test; 1 POSIX-only check; 1 POSIX publication path; 2 real-directory-descriptor tests. No warnings.
- **Sensitivity:** 7 of 7 counted mutants caught, each by an explicit assertion (judged from JUnit failure messages): `/1` read as `/2`; an incomplete category list accepted; source provenance ignored; finalize allowed after a recorded failure; an existing partial adopted; the loser deleting the winner's reservation; a recording failure replacing the primary. One further mutant (a single endpoint duplicated as `after`) is **equivalent**: the distinct-transaction rule still rejects it. Every file was restored byte for byte.

**Limitations.**
- Windows only in this unit; the POSIX-only paths (exclusive publication reservation, mode check, real directory flushes) were not re-run. They are unchanged code, previously verified on Linux in 12.1 and 12.2.
- `internal_error` is coarse.
- The record is not authenticated.
- Endpoint and manifest times are not cross-checked.
- The coordinator does not exist.

**Still explicit:**
- catalog equality is "no difference observed", not stability;
- the READ COMMITTED endpoint queries are not one atomic catalog snapshot;
- lock checks do not establish continuous exclusion;
- sequence values are not frozen by the data snapshot;
- strict sequence differences keep the conservative readiness outcome, and a failed R2 blocks readiness;
- Linux `LocalClientHost` was tested in the documented environment (13.15), while Windows and the two native authentication/TLS injections remain unvalidated;
- the production guarded capture coordinator does not exist;
- the historical expected failures (C6 x7, the candidate x2, C10 x2) and the unresolved identity-test failure are unchanged (not re-run).

**Manifests.** Reviewed under the documented Phase 9 convention. The 20 new public symbols (`MANIFEST_FORMAT_V2`, `MANIFEST_FORMATS`, seven `CATALOG_*` constants, `CatalogDigests`, `CatalogEndpointRecord`, `CatalogObservationRecord`, `ConsistencyClaimV2`, `ObservedSource`, `ArtifactReservation`, `ReservationStateError`, `reserve_artifact`, `CAPTURE_FAILURE_CLASSES`, `capture_failure_reason`, `catalog_observation_record`) have no notebook origin and no bare-name collision, so no entry changed (1,156 data rows each, byte-identical). The Phase 5 versus Phase 9 inconsistency is preserved.

### 13.17 Phase 9B-6H follow-up: failed-partial markers, the publication outcome, the corrected coordinator order, and Linux validation (2026-10-05)

**Status.** A bounded correction of 13.16, implemented, tested and verified **within its tested scope** (synthetic filesystem tests, Windows and Linux). No coordinator, staging framework or new failure class was added. No capture ran and no recovery-readiness claim is made. Phase 9B remains incomplete. This section supersedes 13.16's `COMPLETE` bullet and its coordinator order and interface.

**Source audit: the commit point and the seams.**
- **Commit point.** Publication commits at `ops.rename(staging, final)`. On POSIX the final name is first reserved with an exclusive `mkdir`.
- **Seams after `COMPLETE` is created and before the rename commits:**
  - the marker's own write, flush or close;
  - the staging-directory flush after the marker;
  - staged validation;
  - the `before_publication` checkpoint;
  - the POSIX reservation `mkdir`;
  - a rename that raised while the staging name still exists (outcome "no").
- **After the commit point:** a rename that raised after the staging name disappeared, and a failed parent flush after a rename that succeeded.

**Corrected failed-partial behavior (an intentional change, for `write_artifact` and `ArtifactReservation.finalize`, which share the code).**

| Situation | Before (12.1, 13.16) | Now |
|---|---|---|
| failure at any pre-commit seam after `COMPLETE` was created | partial kept `COMPLETE` **beside** `FAILED`: `NOT_FINAL`, or `CONFLICTING_MARKERS` if renamed by hand | the `COMPLETE` this run created is removed **before** `FAILED` is written. The partial holds `FAILED`, `archive.dump`, `baseline.json`, `manifest.json`. `NOT_FINAL`; if renamed by hand, `ARTIFACT_FAILED` and `MISSING_FILE` |
| marker removal fails | (not applicable) | the partial keeps `COMPLETE` beside `FAILED` (still non-adoptable). The original failure is raised unchanged, with a sanitized note (exception type and errno only) and a `cleanup_failures` entry |
| marker removed, `FAILED` recording fails | (not applicable) | no marker of either kind. `NOT_FINAL`; if renamed by hand, `MISSING_FILE:COMPLETE`. `failed_record = "failed: <Type>"` |
| both fail | (not applicable) | `COMPLETE` and the payload remain, with no `FAILED`. The writer never publishes or adopts it and the public verifier refuses `.partial-*`, but **a manual rename would make it look valid**: a residual limit, reported |
| a NEW interrupt raised by the marker removal | (not applicable) | it propagates, as the established close-cleanup contract does, with the primary as its `__context__`. No `FAILED` record is written then, and the partial stays `NOT_FINAL` |
| failure before `COMPLETE` was created | unchanged | unchanged |

Rules:
- Only the file this run created (ownership is set at its exclusive creation) is removed; nothing else in the partial is touched, and no other path at all.
- The removal is one attempt, never retried, and not followed by a directory flush, so after a crash the marker could reappear beside `FAILED`. That is still non-adoptable.
- The success path is unchanged: successful `/1` output is byte- and operation-identical to the pre-9B-6H and the 9B-6H writers. The only failure-path difference is the added removal, and every remaining file is byte-identical. `/2` validation is unchanged.

**How an uncertain publication is surfaced.** At or after the commit point the writer raises `PostPublicationError`:
- `published` is "yes" or "unknown"; `stage` is "publication" or "flush_parent";
- no `FAILED` record is written and nothing is removed or deleted;
- the reservation moves to `publication_uncertain` and refuses every later call.

The caller must judge the final path with the public verifier before any use; this is never reported as an ordinary failed partial.

**Correction found during Linux validation.** A rename that raised after the staging name disappeared was classified "yes" whenever the final name existed. On POSIX the final name can be only this run's **empty** reservation, so that over-claimed publication. It never deleted anything, so it failed in the safe direction, but it misreported. "yes" now requires a completion marker at the final name; anything else is "unknown". This was found by `test_m_an_unclassifiable_rename_is_reported_unknown_and_nothing_is_deleted` on Linux.

**Corrected coordinator order (documentation only; supersedes 13.16's order and interface).**
1. Acquire L (`backup_capture_lock`). An overlap is `SKIPPED:overlap` and creates nothing. Then take `created_utc` and `reserve_artifact`.
2. Obtain the fresh `before` observation (`observe_endpoint`) **before** starting S.
3. Start S (`backup_capture_snapshot`: the baseline and the exported snapshot), then run the dump (`backup_capture_dump`).
4. Obtain the fresh `after` observation while S and L are still active.
5. `require_unchanged(before, after, ...)`, then `catalog_observation_record(result)` from that checked result.
6. While the dump handle is still valid, copy the archive into a **coordinator-owned private scratch file**. This is a new 0700 directory the coordinator creates and removes; it is **not** the reservation directory, which may hold only contract files. Then:
   - compute its SHA-256 and size during the copy;
   - require the size to equal `DumpArchive.archive_bytes`;
   - keep `handle.baseline` (an immutable `BaselineDocument`), the `/2` record and the archive's TOC fields in memory.
   Nothing is re-read from the live source to reconstruct them.
7. Exit the dump, snapshot and lock contexts **successfully**, including their own cleanup verification.
8. Only then: re-hash the scratch copy and require the SHA-256 and size recorded in step 6, take `finished_utc`, then `reservation.finalize(scratch_copy, metadata, baseline)`. The writer streams and hashes the bytes again and checks that the source did not change during its copy.

After finalizing, the coordinator compares `WrittenArtifact.archive_sha256` with the step-6 digest. A mismatch at that point is a post-publication error: report it, delete nothing. Passing an expected digest into `finalize` would need a narrow API addition, which is left to the coordinator unit. The coordinator then removes its scratch copy on every path.

**Why not publish inside the dump context:** a later dump, snapshot or lock cleanup failure would then follow an already published "success".

**The failure-record boundary, stated honestly.**
- **Before the reservation exists**, nothing is recorded in an artifact `FAILED` record: a lock-setup failure, an overlap (`SKIPPED:overlap`, not a failure), or a reservation `PreconditionError`. These propagate through the job's own error path.
- **After it** (steps 2 to 8, including cleanup failures while exiting the contexts in step 7), a failure while `state == "reserved"` is recorded with `record_failure(capture_failure_reason(exc), exc)`. The same primary is then re-raised.
- **Not every capture failure is recorded**: also excluded are a failure of the recording itself, a new interrupt during cleanup, and anything after `finalize` began (the writer records its own failures, or reports a post-publication outcome).

**Smallest coordinator interface (corrected).** `run_guarded_capture(request, *, client, ops=None) -> PublishedCapture | SkippedOverlap`, with failures raised as described above. Acceptance criteria are those of 13.16, plus three more:
- (9) publication happens only after all three contexts exited with verified cleanup;
- (10) the finalized archive's SHA-256 equals the digest recorded during the step-6 copy, and the baseline and observation are the in-memory values from steps 3 to 5;
- (11) a cleanup failure in step 7 records `FAILED` (`internal_error`) and publishes nothing.

**Tested.**
- **Writer tests:** `test_deployment_backup_artifact_writer.py` updated for the intentional change:
  - the destination-appears and renamed-by-hand cases now assert that the marker is gone;
  - new `test_a_failed_partial_whose_marker_could_not_be_removed_keeps_both_markers_and_is_not_valid`.
- **v2 tests:** `test_deployment_backup_artifact_v2.py` section M covers:
  - every pre-commit seam on both paths: contents, removal before `FAILED`, reason class, the original exception's identity in the cause chain, `NOT_FINAL` and the hand-renamed verdict;
  - an interrupt after `COMPLETE`;
  - removal failure, recording failure, and both together;
  - an interrupt raised by the removal;
  - a rename that happened ("yes"), an unclassifiable rename ("unknown"), and a parent-flush failure after publication.
  The I test was updated for the intentional change.

**Verified.**
- **Windows** (Python 3.13.3, pytest 9.1.1): `python -m pytest tests/regression/test_deployment_backup_artifact_v2.py tests/regression/test_deployment_backup_artifact.py tests/regression/test_deployment_backup_artifact_writer.py tests/regression/test_deployment_backup_capture_catalog.py -q -ra -p no:cacheprovider -o junit_family=xunit1 --junitxml=...` gave **811 passed, 13 skipped**, exit 0.
- **Linux** (Debian 13 amd64, Python 3.13.16, in a disposable container): the same selection gave **820 passed, 2 skipped**, exit 0. The container was built from the unchanged local `napsedge-api:phase9a`, run as its default non-root user (uid 10001), with `--network none`, no Docker socket and no database cluster, the repository mounted read-only, and the host's own pytest stack copied into a read-only scratch mount. Nothing was installed or pulled.
- **All 13 Windows skips executed and passed on Linux:**
  - the 7 symlink tests;
  - the POSIX `lstat` fact;
  - the POSIX non-private-staging check;
  - the POSIX publication reservation;
  - POSIX privacy modes, now enforced as non-root;
  - 2 real directory-descriptor flush tests.
- **The 2 Linux skips:** the NTFS junction test (Windows only) and the Windows privacy statement.
- **Collection differences:** the trailing-separator parametrization de-duplicates on POSIX (4 fewer), and the POSIX reservation seam exists only there (2 more).
- **Clean worktrees** of `373b354` plus the recomputed 21-file dependency closure (byte-identical; it now includes the pre-existing uncommitted `src/scraper.py`, which the 9B-6H clean worktree took from HEAD): the same results on both platforms.
- **Sensitivity:** Windows 5 of 5, Linux 6 of 6; each mutant was caught by explicit assertions in its designated tests, and the file was restored byte for byte. The mutants were: the marker kept; the marker removed after `FAILED`; a removal failure replacing the primary; removal of a non-owned file; a post-publication failure handled as a failed partial; and (Linux) an empty reservation reported as published.

**Limitations.**
- The marker removal is not flushed.
- When both cleanups fail, non-adoptability rests on `NOT_FINAL` alone.
- Coordinator steps 6 and 8 are documented, not implemented.
- `internal_error` remains coarse.
- Windows directory flushes remain unsupported (REQUIRED refuses them).

The statements of 13.16 that stay explicit are unchanged: no-difference-observed is not stability; the endpoints are not one atomic catalog snapshot; there is no continuous exclusion; sequences are not frozen; strict sequence differences stay conservative and a failed R2 blocks readiness; Windows `LocalClientHost` and the two native authentication/TLS injections are unvalidated; and the historical expected failures and the identity-test failure are unchanged.

**Manifests.** Checked against the actual inclusion rule (`docs/phase9a_deployment_contract.md` section 8), not only for name collisions. Rows are keyed by notebook function name; a symbol with no notebook origin gets no row; a bare-name collision with an existing row adds the new file to that row's `in_src_files`, as 9A did for `main`. Because 92 existing rows are underscore names, private names were checked too: all 39 top-level names added by 9B-6H and this follow-up (contract 15, writer 16, bridge 8) have no notebook origin and match no row in either manifest. No row was added or updated (1,156 data rows each, byte-identical to before 9B-6H). The recorded Phase 5 versus Phase 9 inconsistency (Phases 5 to 7 added `src_only` rows; Phase 9 deployment modules get none) is preserved.

### 13.18 Phase 9B-6I: the guarded capture coordinator (2026-10-06)

**Status.** Implemented, tested and verified **within its tested scope**:
- regression tests on Windows and Linux;
- a disposable PostgreSQL 16 cluster, through the production `LocalClientHost` with real PostgreSQL 16.15 tools on the accepted Linux runner.

It composes only accepted modules. There is no CLI, scheduler, restore path or new failure class. No production-source capture was run and nothing was published outside tests. **No recovery-readiness claim.** Phase 9B remains incomplete.

**Source audit (confirmed against the code).**
- **Reservation states:** `reserved`, then `failed`, `finalized` or `publication_uncertain`. `record_failure` is allowed only while the reservation is `reserved`.
- **During `finalize`, the writer records its own `FAILED`**, or reports a post-publication outcome, and consumes the reservation.
- **Commit point:** the directory rename. At or after it, `PostPublicationError` reports `published` "yes" or "unknown".
- **Ownership:**
  - L, S, the observers, the dump process, its passfile and its work directory belong to their modules, which verify their own cleanup;
  - the partial belongs to the writer;
  - the coordinator owns only its scratch directory and the one file in it.

**Manifest facts this path populates truthfully:**

| Field | Source |
|---|---|
| source identity and `server_version_num` | S's facts |
| `backup_role` | the verified URL's login role |
| `pg_dump_version`, `toc_entries`, `toc_sha256` | the `DumpArchive` |
| `alembic_head` | the baseline's `version` |
| `snapshot_descriptor` | the handle |
| `snapshot_unchanged_at_end = true` | only because the snapshot context's own normal exit re-confirmed S's backend and descriptor |
| `monotonic_v1`, `none` | the recorded decisions |
| `repository`, `tooling` | caller-supplied job facts, validated by the contract's patterns |

**Gap found, and the narrow amendment adopted on Anthony's decision (2026-10-06).** `consistency.foreign_write_sessions_start/end` were required integers, but no accepted production module samples them; the count's role set is open decision 8. Writing `0` would invent evidence.
- **Manifest `/2` only:** both fields may be `null`, meaning "not sampled by this capture path". They are always both null or both integers; a mix is `FIELD_VALUE`.
- **`/1` is unchanged** (`null` is `FIELD_TYPE`).
- The coordinator writes `null`. The fields remain evidence only, never exclusion or quiescence.

**Interface.**
- `run_guarded_capture(request, *, client, ops=None) -> PublishedCapture | SkippedOverlap`, in `src/deployment/backup_capture_coordinator.py`.
- `CaptureRequest` holds: `store`, `database_url`, `designation`, `expected_source`, `expected_alembic_head`, `logical_name`, `artifact_id`, `repository`, `tooling`, `durability`.
- Validation is narrow and explicit and creates nothing:
  - types;
  - the contract's patterns;
  - the lock module's verify-full URL check;
  - the login role as an identifier;
  - the client's `ClientHost` operations.
- No request field names a path the coordinator deletes. It does not read test-transport variables or invoke Docker.

**Order, as implemented.**
1. Validate.
2. L; a genuine overlap at the lock's own entry returns `SkippedOverlap` and creates nothing.
3. `reserve_artifact` (`created_utc` is now), then L's own identity must equal `expected_source` (a mismatch is `source_identity_mismatch`).
4. The before-observation, before S.
5. S.
6. The dump.
7. The after-observation while S and L are active.
8. `require_unchanged`, then `catalog_observation_record`.
9. While the dump handle is valid: the archive is copied, streamed and hashed, into a fresh private `napsedge_capture_<random>` directory; its size must equal `DumpArchive.archive_bytes`. Every later fact is copied out here.
10. The dump, snapshot and lock contexts exit with verified cleanup; any failure raises.
11. The copy is re-hashed and must match.
12. `finalize`, then the scratch is removed. The published archive's digest must equal the preserved one.

**Outcomes by publication state.**

| State | Outcome |
|---|---|
| before the reservation | the original error propagates; no artifact record (overlap is not an error) |
| reserved, before `finalize` | one `FAILED` record, mapped by `capture_failure_reason` (`internal_error` when it has none; `checksum_mismatch` for `StagedArchiveMismatch`), then the **same** exception propagates. A failed recording adds a sanitized note and never replaces it; a new interrupt during recording follows the writer's policy |
| `finalize` fails | the writer's own outcome (its `FAILED`, or `PostPublicationError`) propagates; the consumed reservation is never recorded again; scratch-cleanup failures become notes |
| published, but scratch removal failed or the published digest differs | `PublishedCaptureIncomplete` (`problem` "scratch_cleanup_failed" or "archive_digest_mismatch", `published="yes"`, `written`). Neither a failed partial nor a clean success; nothing is deleted |

**Transport-specific coverage (exact).** The coordinator preserves the archive through the local filesystem. That is correct for the production `LocalClientHost`, but the test-only container transport keeps the archive inside the cluster container. So the 6 integration tests run only with `NAPSEDGE_TEST_CLIENT=local`, and skip under the container transport with that reason. The accepted dump, catalog and round-trip modules still run in both transports. Windows `LocalClientHost` remains unvalidated.

**Tested.**
- **`tests/regression/test_deployment_backup_capture_coordinator.py` (36 tests).** The real reservation, writer, verifier, bridge and `require_unchanged` run; only the four database-facing components are recording fakes that yield the production result types. It covers:
  - the exact order, including the before-observation before S;
  - publication only after all three context exits;
  - a verified `/2` artifact with null counts;
  - overlap with no reservation or scratch;
  - invalid requests, lock-setup failure and reservation refusal (no artifact record);
  - 13 reserved-state failures: a catalog difference, two observation failures, a source mismatch, a dump failure and deadline, S lost, L lost, three exit-cleanup failures, a dump size mismatch, an interrupt. Each records one mapped `FAILED`, propagates the original object, publishes nothing and leaves no scratch;
  - archive-copy failure; same-size staged-byte substitution at two points;
  - a failed recording with the primary's identity and traceback preserved;
  - a finalization failure recorded exactly once;
  - publication uncertainty with no `FAILED`;
  - scratch-cleanup failure after publication;
  - caller-owned store and scratch entries untouched;
  - static constraints.
- **`test_deployment_backup_artifact_v2.py`:** 7 tests for the nullable counts.
- **`tests/postgres/test_postgres_backup_capture_coordinator.py` (6 tests, native):**
  - A, a real capture verified by the public verifier, with every owned session, lock holder, client process and scratch directory gone;
  - B, overlap with a real second lock holder: `SkippedOverlap`, nothing created;
  - C, a persistent GRANT committed between the endpoints: `CatalogChanged` (acls), `FAILED catalog_changed`;
  - D, a real missing `pg_dump`: `dump_failed`;
  - E, S or L terminated at the staged checkpoint: `snapshot_unavailable` or `internal_error`, nothing published.

**Verified.**

| Run | Result |
|---|---|
| Windows regression selection (coordinator, v2, contract, writer, catalog, dump, snapshot, lock, preflight, client-support) | 1,035 passed, 13 skipped (the documented platform skips) |
| Linux (no network, non-root, no Docker socket), same selection without `test_capture_client_support.py` | 1,036 passed, 2 skipped (Windows-only) |
| Linux with `test_capture_client_support.py` included | its unchanged 9B-6G test failed: it launches `git`, absent from the image (environmental) |
| Windows container transport, the four PostgreSQL modules | 29 passed, 6 skipped (the coordinator's transport reason) |
| Native runner, same modules | 33 passed, 2 skipped (the declared transport-specific auth/TLS injections) |
| Evidence export | 6 of 6 coordinator records |
| Clean worktree, Windows | 1,035 passed, 13 skipped |
| Clean worktree, native | 33 passed, 2 skipped |
| Sensitivity | 7 of 7, by designated assertions, the file restored byte for byte |

- **Native runner:** rebuilt from the 9B-6G recipe with unchanged pins (PGDG `postgresql-client-16` 16.15), base image ID verified, and removed afterwards.
- **Clean-worktree overlay:** `373b354` plus a 39-file computed closure. It includes three tracked files with pre-existing uncommitted changes:
  - `alembic/env.py` (9B-3);
  - `src/pipeline/bulk_import_safety.py` (9B-5C4);
  - `src/scraper.py`, a prior change unrelated to Phase 9B.
  This unit left all three byte-identical; this is **not** HEAD-only validation.
- **Sensitivity mutants:**
  - the before-observation taken after S;
  - finalization inside the dump context;
  - an ignored lock-cleanup failure;
  - no staged-byte re-check (caught after publication as a digest mismatch);
  - a duplicate `FAILED` write;
  - a post-publication outcome reported as a failure;
  - a post-publication scratch failure reported as success.

**Limitations.**
- The coordinator runs only with a local client host.
- `internal_error` is coarse.
- The counts are not sampled.
- A digest mismatch after publication is reported, not prevented by `finalize`: an expected-digest parameter would be a later, narrow API addition. *(Closed by 13.19: `finalize` now checks the staged bytes against the expected digest and size before `COMPLETE`.)*
- `repository` and `tooling` are caller declarations.
- The retained limits of 13.14 to 13.17 apply: catalog equality is only "no difference observed"; lock checks do not prove continuous exclusion; sequences are not frozen by the snapshot; strict sequence differences retain the conservative readiness outcome; a failed R2 blocks readiness; the historical expected failures and the identity-test failure are unchanged (not re-run).

**Manifests.** Under the 9A rule, all 19 top-level names of the coordinator, private ones included, have no notebook origin and match no row, so nothing was added or updated (1,156 rows each, byte-identical).

### 13.19 Phase 9B-6I follow-up: the expected-archive guard in writer staging (2026-10-06)

**Status.** Implemented, tested and verified **within its tested scope**:
- regression tests on Windows and Linux;
- the real capture path on the accepted Linux runner with the production `LocalClientHost` and PostgreSQL 16.15.

This **closes the expected-archive check during writer staging**. It does **not** establish archive recoverability or recovery readiness, and it does not protect against arbitrary external mutation of the store after verification. Phase 9B remains incomplete.

**The gap (audit of 13.18).**
- The coordinator re-checked its scratch copy, then called `finalize`.
- Any difference between that check and the bytes the writer copied was seen only **after** publication, as `PublishedCaptureIncomplete` (`archive_digest_mismatch`).
- A second check of the source path before copying would not close this: what must be verified is the bytes that become the artifact.

**The mechanism (opt-in).**
- **API:** `ArtifactReservation.finalize(..., *, expected_archive_sha256=None, expected_archive_bytes=None)`. Supply both or neither.
- **Validation of the inputs:** the contract's own validators (`_str` with the 64-lower-hex pattern; `_int`, minimum 1, booleans refused). Anything else is a finalize precondition, recorded like the existing ones: `WriteFailed`, `internal_error`, stage `finalize_plan`, no archive copied.
- **The check:** right after the writer's existing streamed copy and source-unchanged check, the **staged** `archive.dump` is re-read from disk by the verifier's own streaming reader (`_read_file`). Its size and SHA-256 must equal both the expectation and the writer's own copy-stream values.
- **A mismatch:** the existing pre-publication path, `_ValidationFailed` with the existing codes `FILE_SIZE_MISMATCH` / `FILE_DIGEST_MISMATCH`. The result is `WriteFailed`, `checksum_mismatch`, stage `archive`, `published="no"`, and `FAILED` recorded **once by the writer**.
- **Timing:** this happens **before** the baseline, manifest and `COMPLETE` exist, so no completion marker can be left behind.
- **No expectations:** behavior is unchanged. Compared with the writer as it stood before this follow-up, success and three failure paths were outcome-, byte- and operation-identical. Successful `/1` bytes and the `/2` semantics are unchanged.
- No manifest version, reason code or failure class was added.

**Coordinator wiring.**
- `run_guarded_capture` passes the SHA-256 and size recorded while preserving the dump archive (step 9).
- The order is unchanged: all dump, snapshot and lock cleanup succeeds before `finalize` begins. A `before_finalize` checkpoint (a no-op in production) sits immediately before `finalize`.
- The pre-finalization scratch re-check and the post-publication digest comparison both remain, as additional checks.
- `PostPublicationError` and `PublishedCaptureIncomplete` keep their meaning for genuinely post-publication problems.
- On a rejection:
  - no final directory exists;
  - the partial holds `FAILED` and `archive.dump` only;
  - the coordinator makes no second record (the reservation is consumed);
  - scratch cleanup follows the accepted policy (a failure becomes a sanitized note on the writer's error).

**Tested.**
- **Writer level (section O of `test_deployment_backup_artifact_v2.py`, real reservation and writer, real files):**
  - matching expectations publish exactly what omitting them publishes;
  - a wrong digest of the correct size, and a correct digest with the wrong size, are rejected before publication (contents, single `FAILED`, no `COMPLETE`, no rename, `NOT_FINAL`);
  - same-size substituted source bytes are rejected;
  - a deterministic same-size modification **during** the copy (4 KiB chunks; the tail rewritten after the first read and the source mtime restored, so the existing size/mtime check cannot see it) is rejected only by the guard. A characterization without expectations publishes the mixed bytes;
  - 8 malformed expectation forms are refused;
  - a failed `FAILED` record during a rejection keeps the rejection and publishes nothing;
  - post-publication classification is unchanged.
- **Coordinator level (`test_deployment_backup_capture_coordinator.py`):**
  - the published archive equals the preserved digest and size;
  - a same-size substitution after the scratch re-check is rejected by the writer, with one `FAILED` and no final directory;
  - the same rejection, once with a failing scratch cleanup and once with a failing record.
- **Native (`test_postgres_backup_capture_coordinator.py`):** A now asserts that the published archive equals the preserved digest and size. The new F swaps same-size bytes at `before_finalize` in a real capture, and the writer rejects them (`FILE_DIGEST_MISMATCH`) before publication with nothing left running.
- A test whose requirement is prevention accepts only the pre-publication `WriteFailed`. A late `PublishedCaptureIncomplete` or `PostPublicationError` fails it.

**Verified (exact commands in the review attachment).**

| Run | Result |
|---|---|
| Windows regression selection | 1,055 passed, 13 skipped (documented platform skips) |
| Linux (no network, non-root, no Docker socket), the full selection | **1 failed**, 1,063 passed, 2 skipped |
| Linux, without `test_capture_client_support.py` | 1,056 passed, 2 skipped |
| Windows container transport, PostgreSQL | 29 passed, 7 skipped (the coordinator's transport reason) |
| Native runner (rebuilt from the unchanged recipe, removed afterwards) | 34 passed, 2 skipped (the declared auth/TLS injections); all 7 coordinator records exported |
| Clean worktree, 373b354 plus the same 39-file overlay as 13.18 | identical results on every selection, including the one environmental failure |
| Sensitivity | 4 of 4 |

- **The one Linux failure** is environmental: the unchanged 9B-6G test launches `git`, which the image lacks.
- **Clean-worktree overlay:** it includes the pre-existing uncommitted `alembic/env.py`, `src/pipeline/bulk_import_safety.py` and `src/scraper.py`, which this unit left byte-identical.
- **Sensitivity mutants,** each caught by its designated assertion:
  - digest guard disabled;
  - size guard disabled;
  - only the source checked, before copying;
  - the guard moved after publication.

**Remaining limits (unchanged):**
- Foreign-write counts stay null ("not sampled"); decision 8 is open.
- The catalog, locking and sequence limitations stand: "no difference observed" only, no continuous exclusion, sequences not frozen, strict differences conservative, and a failed R2 blocks readiness.
- The coordinator requires a local client host, and Windows `LocalClientHost` is unvalidated.

**Manifests.** One new private name (`_expected_archive`); no notebook origin and no collision under the 9A rule, so nothing changed (1,156 rows each, byte-identical).

### 13.20 Phase 9B-6J: published-artifact round-trip validation (2026-10-06)

**Status.** Test-layer integration only, verified within its tested scope. **No production code changed:** no restore service, CLI or scheduler, and no readiness-policy change. Phase 9B remains incomplete.

**What this demonstrates, exactly.** In this setup (one disposable PostgreSQL 16 cluster, synthetic data, the 9B-2 grants, real PostgreSQL 16.15 client tools through the production `LocalClientHost` on the accepted Linux runner):
- `run_guarded_capture` published a `/2` artifact;
- after it returned, its sessions, lock, processes and scratch were observed gone;
- reopened from disk through the public verifier, the artifact's archive and baseline matched their recorded SHA-256 and sizes;
- restored into a guarded disposable database as the non-superuser migrator, the result compared as reported below against the artifact's **own saved baseline**.

It does **not** demonstrate recovery readiness. A successful restore, strict equality or `monotonic_v1` acceptance is not a readiness claim, and null foreign-write counts mean "not sampled", not quiescence.

**Method.**
- **Test module:** `tests/postgres/test_postgres_backup_published_roundtrip.py`.
- **`open_published`:**
  - `verify_artifact_directory` first (any `INVALID` refuses);
  - then an independent re-hash of `archive.dump` and `baseline.json` against the manifest's inventory;
  - `parse_baseline`, and the existing adapter `baseline_from_document`.
- **The accepted 9B-6E helpers' input record** is filled **only** from that verified artifact: paths inside it, plus digests, sizes and the source name and OID from its manifest. Nothing comes from a live handle, and nothing is reconstructed from the source; source-session inspection is used only for cleanup evidence.
- **`restore` and `compare` are reused unchanged:**
  - the guard refuses the source, an unowned target and a non-empty target before any process starts;
  - `pg_restore -d <conninfo> --no-password --exit-on-error --single-transaction <the published archive.dump>` as the migrator, with no owner or ACL stripping;
  - the strict comparator: fingerprints, constraints, indexes, owners, exact ACL text, version and sequence state;
  - `monotonic_v1` R0 to R3 per sequence, reported separately.
- The artifact is never modified: corruption scenarios change a copy, and every scenario re-hashes the original.

**Scenarios and observed outcomes (native, working tree and clean worktree).**

| Scenario | Outcome |
|---|---|
| A, unchanged source | restore exit 0; every strict category equal; strict sequences equal; all 13 sequences R0, R1 and R2 true; policy accepts |
| B, a sequence-generated row committed after S exported its snapshot and before `pg_dump` started (a test seam wrapping the real dump: the 9B-6E `before_dump` ordering) | restored data equals the saved baseline and excludes the row; the strict difference `team_aliases_id_seq` 3 → 4 is **retained**; the policy accepts separately |
| C, a manually assigned high id (the C4 fixture) | strict equality holds; R1 true, **R2 false**; `r2_failures = [team_aliases_id_seq]`; the policy **rejects** |
| D, a copy with one same-size byte changed in `archive.dump` or in `baseline.json` (`captured_utc`), manifest and marker not updated | the verifier refuses (`FILE_DIGEST_MISMATCH`) before any target or restore process exists |
| E, the source, an unowned database, a migrator-owned target holding a committed table | each refused (`RestoreRefused` / `PreconditionFailed`); no restore process; each database's fingerprints unchanged |
| F, a committed `TYPE public.teams` in the target (the guard counts tables and passes) | restore fails ("already exists"); no comparison; 0 tables committed; no process or backend left |
| F, an uncommitted conflicting table holding the lock | at the 8 s deadline the **real** `pg_restore` was killed (−9) and was gone, its backend was ended, and no comparison was made |

Every injected fault leaves the guards intact; each fault is explained in its test.

**Runner prerequisite closed.**
- **Change:** the disposable runner recipe adds Debian `git=1:2.47.3-0+deb13u1` (git 2.47.3). The PostgreSQL client pins are unchanged (`postgresql-client-16` 16.15-1.pgdg13+2), and no host dependency changed.
- **Effect:** the previously failing, unchanged `test_capture_client_support.py::test_the_recording_popen_records_only_the_client_tools_and_otherwise_behaves_like_popen` **passed**. The full Linux regression selection now runs inside the runner (no network, no Docker socket, uid 10001) with **no exclusion**.

**Verified.**

| Run | Result |
|---|---|
| Windows regression | 1,055 passed, 13 skipped (the documented platform skips) |
| Linux regression, full selection | 1,064 passed, 2 skipped (Windows-only) |
| Windows container transport, published round trip + coordinator + round trip + dump + catalog | 29 passed, 17 skipped (the 10 published and 7 coordinator tests skip for the local-filesystem transport reason) |
| Native, same modules | 44 passed, 2 skipped (the declared auth/TLS injections) |
| Exporter | 10 of 10 records; its negative control (one record removed) was refused |
| Clean worktree, 373b354 + a 40-file overlay (13.19's 39 plus the new module) | identical on every selection |
| Sensitivity | 7 of 7, after one test restructuring |

- **Clean-worktree overlay:** it includes the pre-existing uncommitted `alembic/env.py`, `src/pipeline/bulk_import_safety.py` and `src/scraper.py`, left byte-identical.
- **Sensitivity mutants,** each caught by its designated assertion, with every file restored byte for byte:
  - a different archive restored;
  - comparison against the live source;
  - artifact verification ignored (the independent re-check then refused, but without the verifier's code);
  - restore failure reported as success;
  - strict sequence differences discarded;
  - a failed R2 accepted;
  - the deadline abandoning the wait without signalling the real `pg_restore`. In local mode there is no transport wrapper process, so this is its local analogue.
- **The restructuring:** the first sensitivity run missed the restore-failure mutant, because a comparison crash preceded the gate assertion. The F test now contains and records comparison errors, as the 9B-6E test does.

**Remaining limits:**
- One cluster, version and setup.
- The restore is test instrumentation, not a service.
- Foreign-write counts are not sampled (decision 8 open).
- Catalog equality is only "no difference observed".
- Lock checks are not continuous exclusion.
- Sequences are not frozen.
- Strict differences keep the conservative readiness outcome, and a failed R2 blocks readiness.
- Windows `LocalClientHost` is unvalidated, and the coordinator requires a local client host.

**Manifests.** No production symbol was added, and test modules are not indexed (1,156 rows each, byte-identical).

### 13.21 Phase 9B-6K: the one-shot backup job entrypoint (2026-10-08)

**Status.** Implemented, tested and verified **within its tested scope**. It runs **one** capture and reports it:
- no schedule, deployment, database provider, remote storage, restore path or retry;
- a published artifact is not a recovery-readiness claim;
- Phase 9B remains incomplete.

**Handoff re-verified against the repository.**
- 9B-6J's module (`tests/postgres/test_postgres_backup_published_roundtrip.py`, unchanged since its mutation-checked hash) restores the coordinator-published `/2` artifact's own `archive.dump` and compares against its own `baseline.json`, both reopened through the verifier.
- **Implemented:** the coordinator, the writer guard and now the job.
- **Tested:** the regression suites, the native integration modules on the accepted Linux runner, and 9B-6J's test-layer restore.
- **Deployment gaps:** everything in the checklist below.
- Earlier completion percentages are informal estimates, not acceptance criteria.

**Command (the project's existing entrypoint convention).**
```
python -m src.deployment.entrypoints backup-capture
```
It validates, calls `run_guarded_capture` once with the production `LocalClientHost`, prints **one** JSON line on stdout, and exits with a documented status. The logic lives in `src/deployment/backup_job.py`; `entrypoints.py` gains a lazily importing subcommand.

**Configuration (process environment, the project's existing convention).** This does **not** decide production secrets storage (open decision 9). Placeholders only:
```
# required
NAPSEDGE_BACKUP_DATABASE_URL=postgresql+psycopg://<backup_role>@<host>:<port>/<database>?sslmode=verify-full&sslrootcert=<path-to-CA-file>
NAPSEDGE_BACKUP_DATABASE_PASSWORD=<injected secret; never in the URL, argv or a file in the repository>
NAPSEDGE_BACKUP_TARGET_ACKNOWLEDGEMENT=<the exact acknowledgement phrase for <database>>
NAPSEDGE_BACKUP_EXPECTED_SYSTEM_IDENTIFIER=<source cluster system_identifier>
NAPSEDGE_BACKUP_EXPECTED_DATABASE_OID=<source database OID>
NAPSEDGE_BACKUP_STORE_DIR=<existing artifact store directory>
NAPSEDGE_BACKUP_LOGICAL_NAME=<logical name, [a-z0-9_]{1,40}>
NAPSEDGE_BACKUP_PG_DUMP=<absolute path to the PostgreSQL 16 pg_dump>
NAPSEDGE_BACKUP_PG_RESTORE=<absolute path to the PostgreSQL 16 pg_restore>
NAPSEDGE_BACKUP_GIT_REVISION=<40-hex revision of the job's code>
NAPSEDGE_BACKUP_GIT_DIRTY=<true|false>
NAPSEDGE_BACKUP_TOOLING_VERSION=<job version label>
# optional
NAPSEDGE_BACKUP_DURABILITY=<required (default) | best_effort_reported>
NAPSEDGE_BACKUP_ARTIFACT_ID=<lower-case UUID4; default: a fresh one>
```
- **Credentials:** a URL carrying a password is **refused**. The password arrives only in its own variable and is combined in memory.
- **Expected head:** not configured; the job reads the repository's single Alembic head the way `src.services.health` does.
- **Pre-capture validation:** the coordinator's own validation (verify-full URL, contract patterns, client operations) runs before any capture resource. A refusal names variables only, never a value.

**Report (`napsedge.backup-job-report/1`) and exit status.**
- stdout carries only the report. stderr is the process's diagnostic logging under the project's convention (`src.logging_config`, `NAPSEDGE_LOG_LEVEL`).
- The report's fields are explicitly selected after auditing the exception interfaces:
  - exception **type** names;
  - the writer's `stage`, `reason_class` and `published`;
  - `PublishedCaptureIncomplete.problem`, with whether scratch remains;
  - the `PostPublicationError` distinction;
  - the store's actual record, read from disk: whether `.partial-<id>` exists, whether `FAILED` is present, and its enumerated class.
- Never in the report: exception messages, notes, `DumpFailed.diagnostic`, tracebacks, URLs or tool output.

| Exit | `outcome` | Meaning |
|---|---|---|
| 0 | `published` | the `/2` artifact was published and the coordinator's checks completed; carries the archive and manifest digests and `foreign_write_sessions: "not_sampled"`; lists what it does not establish (including recovery readiness) |
| 1 | `failed` | refused or failed before publication; `artifact_record` is "none" before a reservation, else what the owned partial holds |
| 3 | `skipped_overlap` | another job held the lock; nothing was created; **not a failed backup** |
| 4 | `published_incomplete` | published, but the scratch cleanup or the digest re-check did not complete; not clean success |
| 5 | `publication_uncertain` | the writer reported "yes" or "unknown" after a failure at or after the rename; not clean success |
| 64 | `configuration_refused` | invalid configuration; nothing was created or connected |
| 130 | `interrupted` | `KeyboardInterrupt` or `SystemExit` during the run |

The job never records `FAILED`, retries, re-reserves, deletes or re-runs anything; the coordinator and writer own those. The foreign-write counts stay null and mean "not sampled"; decision 8 is not implemented.

**Sanitized example output** (from the native runs; digests shortened, ids elided):
```
# A_published: exit 0
{"archive_bytes": 123075, "archive_sha256": "5545ef7babad...", "artifact": "napsedge-napsedge-20261008T023925Z-<artifact-id>", "artifact_id": "<artifact-id>", "does_not_establish": ["catalog_stability", "continuous_exclusion", "frozen_sequences", "restore_verified", "recovery_readiness"], "establishes": ["files_complete", "staged_structure_validated", "no_catalog_difference_observed_at_two_endpoints"], "exit_code": 0, "foreign_write_sessions": "not_sampled", "job": "napsedge-backup-capture", "manifest_format": "napsedge.backup-artifact/2", "manifest_sha256": "f493d18529a3...", "outcome": "published", "report_format": "napsedge.backup-job-report/1"}
# B_overlap: exit 3
{"artifact_id": "<artifact-id>", "exit_code": 3, "job": "napsedge-backup-capture", "meaning": "another job held the lock; nothing was created; not a failed backup", "outcome": "skipped_overlap", "report_format": "napsedge.backup-job-report/1"}
# C_missing_pg_dump: exit 1
{"artifact_id": "<artifact-id>", "artifact_record": {"failed_reason_class": "dump_failed", "failed_record": "present", "partial": ".partial-<artifact-id>", "partial_present": true}, "error_type": "DumpPreconditionError", "exit_code": 1, "job": "napsedge-backup-capture", "outcome": "failed", "report_format": "napsedge.backup-job-report/1"}
# C_wrong_source_identity: exit 1
{"artifact_id": "<artifact-id>", "artifact_record": {"failed_reason_class": "source_identity_mismatch", "failed_record": "present", "partial": ".partial-<artifact-id>", "partial_present": true}, "error_type": "DatabaseTargetVerificationError", "exit_code": 1, "job": "napsedge-backup-capture", "outcome": "failed", "report_format": "napsedge.backup-job-report/1"}
```

**Tested.**
- **Regression, `tests/regression/test_deployment_backup_job.py` (49 tests):**
  - a valid plan with the exact `LocalClientHost` and the repository's own head;
  - optional defaults;
  - each of the 12 required variables missing;
  - 14 invalid forms, refused before capture without echoing values: a password-bearing URL, an unparseable URL, no database, not verify-full, a wrong acknowledgement, a non-decimal identity, a missing store, a relative tool path, a non-boolean dirty flag, an unknown durability, and an artifact id, logical name or revision refused by the coordinator's own validation;
  - every outcome and exit code, with the record read from on-disk partials;
  - interruption during the run and during validation;
  - exactly one invocation with no retry, and the store untouched;
  - redaction (planted password and host absent from every output);
  - the real command as a subprocess (exit 64);
  - static constraints.
- **Native, `tests/postgres/test_postgres_backup_job.py` (4 tests):** the real command as a subprocess on the runner, with `NAPSEDGE_TEST_CLIENT` removed from the child's environment:
  - A, published, verified `/2`;
  - B, a real overlapping lock holder: skip, nothing created, empty stderr at `NAPSEDGE_LOG_LEVEL=WARNING`;
  - C, a missing `pg_dump` (`dump_failed`) and a wrong expected system identifier (`source_identity_mismatch`), each with the `FAILED` record read from disk.
  - Every run sanitized; no owned session, lock holder, client process or scratch left.
- **Windows:** the native module skips (it needs the real PostgreSQL 16 tools). No Windows native-client coverage is claimed.

**Verified.**

| Run | Result |
|---|---|
| Windows regression (job, entrypoints, env validation, coordinator, v2, contract, writer, catalog, dump, snapshot, lock, preflight, client support) | 1,135 passed, 13 skipped (the documented platform skips) |
| Linux regression, full selection, inside the runner (no network, no Docker socket, uid 10001) | 1,144 passed, 2 skipped (Windows-only) |
| Windows container transport (job, published round trip, coordinator, round trip, dump, catalog) | 29 passed, 21 skipped (the 4 job, 10 published and 7 coordinator tests need the local client) |
| Native runner (rebuilt from the 9B-6J recipe with unchanged pins including git, removed afterwards), same modules | 48 passed, 2 skipped (the declared auth/TLS injections) |
| Clean worktree, 373b354 + a 44-file overlay (adds the job module, the modified `entrypoints.py` and the two test modules) | identical on every selection |
| Focused sensitivity | 5 of 5 |

- **Clean-worktree overlay:** it includes the pre-existing uncommitted `alembic/env.py`, `src/pipeline/bulk_import_safety.py` and `src/scraper.py`, left byte-identical.
- **Sensitivity mutants:** a password-bearing URL accepted; an overlap reported as a failure; an incomplete publication reported as success; an exception message in the report; a retry.
- **Re-run:** every selection was repeated after the unused `backup_job.main` was removed (see Manifests).

**Infrastructure decision checklist (decisions, not defaults; none is chosen here).**

| Decision | Blocks |
|---|---|
| Database provider and deployment runtime (managed or self-hosted PostgreSQL 16; where the job runs; the TLS endpoint and CA) | a real source for the job; Windows/Linux client choice; the job's container target; any restore drill on real infrastructure |
| Production secrets storage and injection (decision 9) | running the job anywhere but a test; rotation; the restore role's credentials |
| Backup frequency and the acceptable data-loss window (RPO) | any schedule (none exists); monitoring thresholds for "no recent valid artifact" |
| Off-host storage and encryption (location, key custody, access split between writer and restorer) | upload of published artifacts; recovery from loss of the host; retention enforcement |
| Retention and deletion policy (counts, monthly exemptions, never deleting the newest valid artifact) | any pruning code (none exists); storage sizing |
| Restore-time objective and an operational restore drill (who, how often, on what target) | a production restore path; `verification.json` records; any readiness claim |
| Decision 8: foreign-write session sampling (the role set, a privilege change, or dropping it) | non-null `foreign_write_sessions_*`; any quiescence evidence |

**Retained limits:**
- Catalog equality is only "no difference observed".
- Lock checks are not continuous exclusion.
- Sequences are not frozen.
- Strict differences stay conservative, and a failed R2 blocks readiness.
- The coordinator requires a local client host, and Windows `LocalClientHost` is unvalidated.

**Manifests.** Checked under the 9A rule. The first review found one bare-name collision: an unused `backup_job.main` against the existing `main` row. It was removed rather than indexed, because the command runs through `entrypoints backup-capture`. The remaining new names, including `entrypoints.run_backup_capture`, have no notebook origin and no collision, so no row changed (1,156 each, byte-identical).

### 13.22 Phase 9B-7A: the backup job container and the password-file credential source (2026-10-08)

**Status.** Packaging and credential-file support only: implemented, tested and verified **within its tested scope**; uncommitted.
- It is **not** a production deployment and **not** a recovery-readiness claim.
- It does **not** adopt a provider (DigitalOcean or another), B2, the proposed budget, a schedule or a retention policy (decision package, 9B-6L).
- Nothing was provisioned; no application connection pool, API image or Streamlit image changed.
- Phase 9B remains incomplete.

**Password file (`src/deployment/backup_job.py`).** The job now accepts **exactly one** credential source:
- `NAPSEDGE_BACKUP_DATABASE_PASSWORD`, unchanged from 13.21; or
- `NAPSEDGE_BACKUP_DATABASE_PASSWORD_FILE`, an absolute path to a file holding the password, intended for a read-only credential mount.
- **Both or neither is refused** (exit 64) before any capture resource, as are an unreadable or invalid file and a URL carrying a password.

| Rule | Behavior |
|---|---|
| Path | absolute; a regular file; symbolic links are followed (container secret and credential mounts use them) |
| Permissions | POSIX: refused if writable by group or others; readability is not checked, because container secret mounts are commonly 0444 and the mount, not the mode, is the boundary. Windows: no mode check |
| Size | at most 4096 bytes (checked on `fstat` and again by a bounded read) |
| Encoding | strict UTF-8; a byte-order mark is refused |
| Terminal newline | exactly one terminal LF is removed if present; nothing else is removed or normalized |
| Whitespace | leading and trailing spaces and tabs are part of the password |
| Refused content | empty after the one LF; any CR, LF or NUL remaining (so a CRLF-terminated file and a file ending in two LFs are refused, not rewritten) |
| Handling | read once into memory and combined with the URL in memory; never placed in another environment variable or argument |
| Diagnostics | every refusal names the variable only; never the content, never the path |

**Container target (`Dockerfile`, `--target backup`).**
- **Base:** the existing `base` stage, i.e. the job, its migration closure (`alembic/`, `alembic.ini`) and the application's pinned dependencies, unchanged. The API, Streamlit and Discord targets are untouched.
- **PostgreSQL 16 client:** installed from the PGDG apt repository (Debian 13 "trixie" asserted).
  - Pins: `postgresql-client-16=16.15-1.pgdg13+2` and `postgresql-client-common=293.pgdg13+1` (the accepted client pins).
  - The signing key is fetched by `docker/backup/fetch_pgdg_key.py`, which refuses any key whose fingerprint is not `B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8`.
  - apt signature and checksum verification is left on.
- **Runtime:**
  - user `napsedge` (uid/gid 10001);
  - `TMPDIR=/var/lib/napsedge-backup/scratch`;
  - writable locations `/var/lib/napsedge-backup/scratch` and `/var/lib/napsedge-backup/store` (0700, owned by 10001);
  - the exec-form entrypoint `python -m src.deployment.entrypoints backup-capture`.
- **What it needs and doesn't.** No Docker socket and no test-transport setting are needed or read. The build context's `.dockerignore` (unchanged) keeps `.env` files, test evidence, tests and local data out.

**Reproducible build** (from the repository root):
```
docker build --target backup -t napsedge-backup:<tag> .
```

**Placeholder-only run example.** Nothing here is a real host, path or credential.
```
docker run --rm --read-only --user 10001:10001 --cap-drop ALL --security-opt no-new-privileges \
  --network <network that reaches the database> \
  -v <credential directory or secret>:/run/napsedge-backup/credentials:ro \
  -v <CA directory>:/run/napsedge-backup/ca:ro \
  -v <scratch volume>:/var/lib/napsedge-backup/scratch \
  -v <artifact store volume>:/var/lib/napsedge-backup/store \
  --env-file <file holding ONLY the non-secret settings below> \
  napsedge-backup:<tag>
```
```
NAPSEDGE_BACKUP_DATABASE_URL=postgresql+psycopg://<backup_role>@<host>:<port>/<database>?sslmode=verify-full&sslrootcert=/run/napsedge-backup/ca/<ca file>
NAPSEDGE_BACKUP_DATABASE_PASSWORD_FILE=/run/napsedge-backup/credentials/<password file>
NAPSEDGE_BACKUP_TARGET_ACKNOWLEDGEMENT=<the exact acknowledgement phrase for <database>>
NAPSEDGE_BACKUP_EXPECTED_SYSTEM_IDENTIFIER=<source cluster system_identifier>
NAPSEDGE_BACKUP_EXPECTED_DATABASE_OID=<source database OID>
NAPSEDGE_BACKUP_STORE_DIR=/var/lib/napsedge-backup/store
NAPSEDGE_BACKUP_LOGICAL_NAME=<logical name>
NAPSEDGE_BACKUP_PG_DUMP=/usr/lib/postgresql/16/bin/pg_dump
NAPSEDGE_BACKUP_PG_RESTORE=/usr/lib/postgresql/16/bin/pg_restore
NAPSEDGE_BACKUP_GIT_REVISION=<40-hex revision the image was built from>
NAPSEDGE_BACKUP_GIT_DIRTY=<true|false>
NAPSEDGE_BACKUP_TOOLING_VERSION=<job version label>
```

Requirements for the mounts:
- The password file must be readable by uid 10001 and not writable by group or others.
- The CA file must be readable.
- Scratch and the store must be writable by uid 10001 and private (0700). The read-only root filesystem offers no other writable path.

**What the retained image is.** It is a local build only; it was not pushed and not published.

| Property | Value |
|---|---|
| Tag | `napsedge-backup:9b7a-20261008154259` |
| Image ID (local content digest) | `sha256:0841275e79abc54a33f33afebc826019fd39a0794e8383b54750418a52a8d3ae` |
| Base | `python:3.13-slim@sha256:bf44cdfcb76cd3b41e879bc058fc37ec5872002ccfde7fcb765e218cde0cd79c` (Python 3.13.16, Debian 13) |
| Client | `pg_dump`/`pg_restore` (PostgreSQL) 16.15 (Debian 16.15-1.pgdg13+2); `postgresql-client-common` 293.pgdg13+1 |
| User | `napsedge` (10001:10001) |

**Rebuild note.** The first build (`napsedge-backup:9b7a-20261008150845`) came from a Dockerfile whose `RUN` continuations had been collapsed onto one line by an editing repair.
- The continuations were restored. The commands are unchanged token for token, and Docker's parser joins them to the same instruction.
- The image was rebuilt. Its layers, history and configuration are identical to the first build; only the local image ID differs.
- Every Dockerfile- or image-dependent run was repeated against the rebuilt image, and the superseded tag was removed.

**Image contents and history inspection.**
- 32 history lines, none secret-like (the only key-shaped values are the base image's public `GPG_KEY` and `PYTHON_SHA256`).
- `/app` holds the runtime inputs only: no `.env`, `tests/`, evidence or docs beyond `docs/sr_slug_reconciliation.json`.
- None of the planted test credentials are present.
- **Observation, not changed:** `.dockerignore` excludes only a top-level `__pycache__/`. Nested `.pyc` files from the build machine therefore enter the shared base layer, including one for a migration with no source file. Python and Alembic do not load source-less files from `__pycache__`. This also affects the existing images, so it is left to a later unit.

**Tested.**
- **Regression, `tests/regression/test_deployment_backup_job.py`:** 75 tests (48 before this unit plus 27 new).
  - the exact password from a file: plain, one LF removed, spaces and tabs kept, non-ASCII, colon and backslash;
  - nine malformed files refused without echoing content or path: empty, a lone LF, two LFs, CRLF, an embedded LF, NUL, non-UTF-8, a BOM, too large;
  - 4096 bytes accepted;
  - missing, relative and directory paths refused;
  - POSIX: group- or world-writable refused, a 0444 mount-style file accepted, a symbolic link followed;
  - both and neither sources refused;
  - a URL password still refused;
  - the environment password unchanged;
  - redaction of a file-sourced password across every outcome.
- **The built image, `tests/postgres/test_postgres_backup_image.py`** (5 tests; skipped unless `NAPSEDGE_BACKUP_IMAGE` names an image). The image runs exactly as above: `--read-only`, uid 10001, all capabilities dropped, `no-new-privileges`, no socket, no test-transport setting. It uses labelled volumes for the read-only credential and CA mounts and for writable scratch and store, and connects to the disposable harness cluster with `verify-full`.
  - **A:** exit 0, a `/2` artifact verified with `verify_artifact_directory`; a caller-owned file in the store unchanged; the credential file unchanged.
  - **B:** a real overlapping lock holder gives exit 3, nothing created, and empty stderr at `NAPSEDGE_LOG_LEVEL=WARNING`.
  - **C:** a wrong password gives exit 1 and nothing published.
  - **D:** a wrong expected source gives exit 1 and `source_identity_mismatch` read from the on-disk `FAILED` record.
  - **E:** the user is uid 10001, the root is read-only, `pg_dump`/`pg_restore` are dpkg-owned ELF binaries of 16.15, `TMPDIR` is scratch, and no socket is present.
  - **After every run:** no password, URL or traceback in any output; stderr is log lines only; no owned session, lock holder, client process, scratch entry or leftover container.
- **Platform coverage.** The image tests are the **native Linux** coverage of the packaged job. The Windows regression covers the job's logic only; its POSIX permission and symbolic-link tests skip there and run in the Linux runner. Windows `LocalClientHost` remains unvalidated.

**Verified** (JUnit retained with the unit's evidence):

| Run | Result |
|---|---|
| Windows regression (job, entrypoints, env validation, container contract, coordinator, v2, contract, writer, catalog, dump, snapshot, lock, preflight, client support) | 1,215 passed, 18 skipped (the documented platform skips, including the 5 POSIX password-file tests) |
| Linux regression, same selection, in the disposable runner (no network, no socket, uid 10001) | 1,227 passed, 2 skipped, 2 failed: the pre-existing `TestGitignoreRehearsalRule` tests call `git check-ignore`, which git refuses in a bind mount owned by another uid ("dubious ownership"). With `/repo` marked safe through environment-only git configuration, that module passes 59 of 59. Environmental; not a product failure |
| Windows container-transport PostgreSQL (job, published round trip, coordinator, round trip, dump, catalog) | 29 passed, 21 transport skips |
| Native runner (rebuilt from the 9B-6K recipe with unchanged pins, removed afterwards), same modules | 48 passed, 2 declared skips |
| The built image against the disposable harness | 5 passed |
| Clean worktree, 373b354 + a 47-file overlay (adds the Dockerfile, the key script and the image test), image rebuilt from that tree | identical on every selection (see below) |
| Focused sensitivity | 3 of 3 |

- **Clean-worktree image.** It was built from the overlay, tested, and removed. Its `/app` differs from the retained image in three files only:
  - `app.py` and `src/deployment/env_validation.py`: line endings only (the system `core.autocrlf=true` checks a fresh worktree out with CRLF);
  - `src/ovr_model.py`: empty at HEAD; the repository copy is a pre-existing uncommitted change outside the job's import closure.
- **Sensitivity mutants:**
  - a password file's content placed into a refusal;
  - both credential sources accepted;
  - a stand-in shell-script `pg_dump` in a temporary mutant image (caught by E and A). The mutant image was removed.

**Retained limits:**
- Catalog equality is only "no difference observed".
- Lock checks are not continuous exclusion.
- Sequences are not frozen.
- Strict differences stay conservative, and a failed R2 blocks readiness.
- Null foreign-write counts mean "not sampled" (decision 8 open).

**Manifests.** Checked under the 9A rule: no row added or updated.
- The new `backup_job` names (`CREDENTIAL_SOURCES`, `PASSWORD_FILE_MAX_BYTES`, `_read_password_file`) have no notebook origin and no collision.
- `docker/backup/fetch_pgdg_key.py` is a build-time helper outside `src/`. The manifests index only `src/`, so its `main` is out of scope.

**Remaining infrastructure decisions** (the 9B-6L bundles; none adopted):
- bundle 1: database provider, runtime and budget, including whether the 1 GiB plan needs app pool caps;
- bundle 2: frequency, RPO and RTO;
- bundle 3: secrets injection, encrypted off-host storage, key custody and retention;
- the readiness rule for strict sequence differences;
- the definition of "recovery ready";
- the refusal-retry policy;
- decision 8.

**Smallest live compatibility probe after a provider is selected.** This is a recommendation; none was run.
- **Setup:** one disposable database on the chosen provider with synthetic data, the three roles with the 9B-2 grants, and migrations run as the migrator (decision package 6, item 3).
- **Run:** this image exactly as in the run example, once alone and once alongside a second instance. That covers items 1-5 and 8.
  - The single run exercises the server version, `pg_control_system()` for the backup role, the ownership preflight, the advisory lock and snapshot import on the direct endpoint, and `verify-full` with the provider CA.
  - Expected: exit 0 with a verifying artifact, and exit 3 for the overlapping instance.
  - Any refusal names its stage and class in the report.
- **Then:** item 9 (the published round trip).
- **Excluded from this unit:** scheduling, remote encryption and upload, pruning, pool caps and readiness-policy changes.

### 13.23 Phase 9B-7A follow-up: build-context bytecode hygiene and Linux validation correction (2026-10-08)

**Status.** Build-context hygiene and a validation correction only; uncommitted.
- It is **not** a deployment and **not** a recovery-readiness claim.
- No provider, budget, schedule or retention policy is adopted, and nothing was provisioned.
- No job, coordinator, writer or contract code changed.
- Phase 9B remains incomplete.

**Defect.** In `.dockerignore`, a bare `__pycache__/` or `*.pyc` matches only at the build-context root.
- The build machine's nested caches therefore entered **every** image through `COPY src/`, `COPY alembic/` and `COPY pages/`. The accepted 9B-7A backup image held 283 host bytecode files under `/app`, in 21 `__pycache__` directories.
- `.pyo` was not excluded at all.
- No Dockerfile-specific ignore file exists, so `.dockerignore` governs every target.

**Correction (`.dockerignore` only).**
- The two root-only lines are replaced by `**/__pycache__`, `**/*.pyc` and `**/*.pyo`, which match at any depth, including the root.
- They form the file's **last** block, because a later `!` exception would otherwise re-include bytecode. No current exception matches bytecode.
- Required sources, migrations, dependency files and `docker/backup/fetch_pgdg_key.py` are unaffected.
- Anthony's working-tree caches were not touched.

**Host bytecode versus install-generated bytecode.**
- The rules exclude only what the host wrote into the context.
- pip compiles dependencies while installing them inside the image: 10,121 `.pyc` files under `/usr/local/lib/python3.13`, unchanged. They are legitimate and are **not** claimed absent.
- The claim is limited to `/app`, the copied application, which now holds no bytecode.

**Verified through actual builds** (`tests/postgres/test_backup_image_build_context.py`; runs only with `NAPSEDGE_BUILD_CONTEXT_TEST=1`).
- **Setup:**
  - A disposable copy of the build inputs is made without host caches.
  - Sentinel "bytecode" (harmless marker bytes, never imported) is planted at the context root (`napsedge_sentinel_root.pyc`, `__pycache__/`) and at `src/`, `src/__pycache__/`, `src/deployment/__pycache__/`, `src/deployment/*.pyo`, `alembic/versions/__pycache__/` and `pages/__pycache__/`.
  - An ordinary `.py` sentinel is planted as the positive control.
- **Context probe** (`FROM scratch` + `COPY . /`, exported locally):
  - no sentinel and no bytecode of any name is transferred;
  - every required input is transferred: the `src/`, `alembic/` (including every migration) and `pages/` sources with identical digests (pages 01-09 excluded by design), the dependency files, the key script and the positive control.
- **The real `backup` target from that context:**
  - no bytecode under `/app`;
  - every source and migration present with an identical digest;
  - the real `pg_dump` 16.15.
  - The accepted job runs: with no configuration it reports `configuration_refused`, exit 64.
- **Negative controls** (the repository's `.dockerignore` mutated, judged on designated assertions only, restored byte for byte):
  - **NC1**, the faulty 9B-7A rules restored, fails the context, image and static depth assertions. The root sentinels were excluded, and every nested one leaked.
  - **NC2**, a later `!src/**/__pycache__` exception, fails the context, image and rule-ordering assertions.
- **Static regression** (`test_deployment_container_contract.py`, 2 new tests): the three rules are present, and no `!` rule follows them.

**Effect on the shared build context.** The api, streamlit and discord targets were built from the corrected context (disposable tags, removed afterwards). In each:
- `/app` holds no bytecode;
- `/app` holds the same 230 files with the same content digest as the backup image;
- every `/app` source compiles in memory;
- the entrypoint refuses missing configuration cleanly (exit 1, no traceback).

The existing `napsedge-api/streamlit/discord:phase9a` images were neither rebuilt nor retagged. They still contain the host bytecode from their original build.

**Base image pinned for the build.**
- Every build used `--platform linux/amd64` and a named build context that pins `python:3.13-slim` to the accepted index `sha256:bf44cdfcb76cd3b41e879bc058fc37ec5872002ccfde7fcb765e218cde0cd79c` (linux/amd64 manifest `sha256:8fb4cfa1a2616d7b8e0c2175cc6ad68f5729c34ea8488c0b360d2934b7be9024`, 3.13.16-slim-trixie). The floating tag still resolved to the same index on 2026-10-08.
- Layers 1-9 (the base, the apt build dependencies, the user, the requirements and the pip install) are byte-identical to the accepted 9B-7A image. The layers from `COPY src/` onward were rebuilt; the PGDG step installed the same pinned client.
- The Dockerfile's `FROM` is unchanged.

Reproducible build command:
```
docker build --target backup --platform linux/amd64 \
  --build-context python:3.13-slim=docker-image://python:3.13-slim@sha256:bf44cdfcb76cd3b41e879bc058fc37ec5872002ccfde7fcb765e218cde0cd79c \
  -t napsedge-backup:<tag> .
```
The 13.22 run example is unchanged.

**Retained corrected image** (local only; the 9B-7A image `napsedge-backup:9b7a-20261008154259` is kept, unmodified):

| Property | Value |
|---|---|
| Tag | `napsedge-backup:9b7a1-20261008162134` |
| Image ID (local content digest) | `sha256:7e5dced289b1eec1f3cb97ff933230f4a0eee0ba383a4abb8c44eac80b5d675c` |
| `/app` | 230 files, identical to the accepted image's `/app` minus its 283 bytecode files; no `.env`, tests, evidence or planted credentials; 32 history lines, none secret-like |
| Client | `pg_dump`/`pg_restore` 16.15 (Debian 16.15-1.pgdg13+2); `postgresql-client-common` 293.pgdg13+1; Python 3.13.16; user 10001 |

**Linux runner correction.**
- **Before:** the 9B-7A Linux selection ran the pre-existing `TestGitignoreRehearsalRule` tests with git refusing the foreign-owned, read-only bind mount ("dubious ownership"). That produced 2 failures, and the earlier 59-of-59 check covered only the isolated module.
- **Fix:** the disposable runner now carries `safe.directory = /repo` in its own `/etc/gitconfig`, which is exactly the mounted repository path; no wildcard is used. Nothing is passed at run time, and the host's git configuration is unchanged. The mount stays read-only and the runner uid 10001.
- **Clean run:** a fresh standalone clone at 373b354 is mounted at the same `/repo`. A linked worktree's `.git` file names a Windows path the Linux runner cannot resolve.

**Verified:**

| Run | Repository | Clean clone (373b354 + 50-file overlay; images rebuilt from it) |
|---|---|---|
| Windows regression (job, entrypoints, env validation, container contract, coordinator, v2, contract, writer, catalog, dump, snapshot, lock, preflight, client support) | 1,217 passed, 18 skipped | 1,217 passed, 18 skipped |
| Linux regression, the **full** selection, in the runner | 1,231 passed, 2 skipped, **0 failed** (both `.gitignore` tests pass) | 1,231 passed, 2 skipped, 0 failed |
| The built image against the disposable harness (publish and verify, overlap, wrong password, wrong source, non-root read-only probe, cleanup) | 5 passed | 5 passed |
| Sentinel build-context module | 4 passed | 4 passed |
| Other targets' smoke | api, streamlit and discord as above | the same; `/app` differs from the repository build only in `app.py` and `env_validation.py` (CRLF checkout under the system `core.autocrlf`) and `src/ovr_model.py` (empty at HEAD; a pre-existing uncommitted change outside the job's closure) |
| Negative controls | 2 of 2 | -- |

- **Not repeated:** the container-transport and native PostgreSQL selections, because no code they exercise changed. Their 9B-7A results stand.
- **Windows coverage:** the Windows regression covers the static rules only. The real builds ran through the Linux Docker engine.

**Manifests.** Unchanged: no `src/` symbol was added.

**Next infrastructure choice.** The live compatibility probe (13.22) first needs **bundle 1**: the database provider, the runtime and the operating budget. The 9B-6L recommendation (DigitalOcean Managed PostgreSQL v16 with a Droplet, or self-managed PostgreSQL 16 as the fallback) stays **provisional**. Secret storage, scheduling, encryption, retention and the readiness decisions stay open.

### 13.24 Phase 9B-7B: the live-provider compatibility probe -- prepared and locally rehearsed (2026-10-08)

**Status, in three separate states:**
- **Probe prepared:** yes.
- **Locally rehearsed:** yes, against a local stand-in only.
- **Provider verified:** **no.**

No provider was selected, nothing was purchased or provisioned, no image was pushed, and the probe has never run against a real endpoint. The decision package's recommendation stays provisional. This is not a deployment or recovery-readiness claim. Phase 9B remains incomplete. The full procedure is `docs/phase9b_provider_probe_runbook.md`.

**What was added:**
- **`tests/postgres/provider_probe.py`** (test layer; never in an image). One fixed procedure with seven steps: `inspect`, `setup`, `identity`, `hold-lock`, `verify-restore`, `residue` and `cleanup`.
  - Every step except the capture reuses the accepted helpers unchanged: the 9B-2/9B-4 bootstrap and grants, Alembic as the migrator, the synthetic seed, the production preflight and lock, 9B-6J `open_published`, and 9B-6E `restore` and `compare`.
  - The capture is the retained image `napsedge-backup:9b7a1-20261008162134`, unmodified.
  - **Exit codes:** 0 pass, 2 blocker, 3 refused, 4 difference, 1 error.
  - **Mutations:** every mutation needs an explicit `--confirm` value and is listed in the evidence.
  - **Ledger:** each created resource is recorded the moment it exists. `cleanup` removes only ledger entries.
  - **Credentials** come from files under the job's own password-file rules. Role passwords reach the server only as client-computed SCRAM verifiers.
  - **Evidence:** every evidence record is checked for the configured passwords before it is written.
- **`tests/postgres/provider_probe.Dockerfile`**: the probe-tooling image. It is built FROM the retained backup image with no build context, and adds only the accepted runner's test-package pins.
- **Probe additions where the decision package needed evidence the existing helpers did not record:**
  - **check 6:** the backup role ends a second session of its own with `pg_terminate_backend`;
  - **check 7:** connection limits are recorded as an observation only;
  - **check 4:** a pooled-endpoint detection, i.e. one connection must keep one backend across transactions.
- **Comparison reader:** the restored target's owner (the migrator), so the provider admin needs no table access. The local rehearsal proves the migrator's reads equal a superuser's reads of the same target.
- **PostgreSQL 16 finding** (from the non-superuser rehearsal admin): a CREATEROLE admin receives ADMIN on the roles it creates, but not SET. `CREATE DATABASE ... OWNER` needs SET.
  - `setup` therefore uses `SET createrole_self_grant = 'set'` in one session, for the migrator only.
  - No INHERIT is granted, so the admin gets no access to the migrator's objects.
  - The membership goes away with the role. An endpoint that refuses this is a blocker.

**Locally rehearsed (`tests/postgres/test_provider_probe_rehearsal.py`)** against one disposable verified-TLS PostgreSQL 16 cluster with no NapsEdge roles, with a NOSUPERUSER CREATEROLE CREATEDB stand-in admin:
- **A:** the full runbook from configuration to cleanup:
  - identity read by the backup role;
  - a real capture by the image, then a real overlap skip (exit 3);
  - the `/2` artifact verified and restored into the guarded target;
  - strict comparison equal, and the sequence policy accepting;
  - a second `verify-restore` refused because the target exists;
  - residue empty, cleanup complete, no credential in any output or evidence.
- **B:** a revoked `pg_control_system()` is a blocker. No `job.env` is written and nothing is substituted or granted.
- **C:** a pre-existing role with a probe role's name is refused, and `cleanup` leaves it untouched.

The remaining differences from a provider run are listed in the runbook (section 9).

**Verified (repository and clean clone):**

| Run | Result |
|---|---|
| Windows regression | 1,242 passed, 18 skipped |
| Linux regression, full selection, in the runner | 1,256 passed, 2 skipped, 0 failed |
| Probe rehearsal | 3 passed |
| Clean clone (373b354 + 55-file overlay) | identical: 1,242/18 skips; 1,256/2 skips; rehearsal 3 passed |
| Probe regression (no Docker) | 25 tests, included above |
| Sensitivity | 4 of 4: evidence carrying a credential, a foreign ledger entry, a substituted source identity, cleanup dropping an unowned role |

**Not repeated:** the image, build-context, container-transport and native PostgreSQL selections, because no code they exercise changed. Their 9B-7A and follow-up results stand.

**Official prices rechecked 2026-10-08, unchanged:**
- DigitalOcean Managed PostgreSQL Standard: 1 GiB $15.15/month, 2 GiB $30.45/month.
- Basic Droplets: 1 GiB $6, 2 GiB $12 (1 vCPU) or $18 (2 vCPU), 4 GiB $24.
- B2: $6.95/TB-month, first 10 GB free.

**Retained limits** (unchanged):
- Catalog equality is only "no difference observed".
- Lock checks are not continuous exclusion.
- Sequences are not frozen.
- Strict differences stay conservative, and a failed R2 blocks readiness.
- Null foreign-write counts mean "not sampled" (decision 8 open).
- Windows `LocalClientHost` is unvalidated.

### 13.25 Phase 9B-8B: scheduling, encrypted off-site transfer, retrieval, restore drills and retention -- implemented locally (2026-10-08)

**Four statuses, kept separate:**
- **Local implementation: implemented and verified within its tested scope; uncommitted.**
- **Live storage validation: not done.** No storage account exists; the store in every test is an emulator.
- **Provider compatibility: not verified.** The DigitalOcean probe is deferred.
- **Demonstrated recovery on real infrastructure: not done.**

Nothing is scheduled; off-site transfer and deletion are disabled by default. This is not a recovery-readiness claim, and Phase 9B remains incomplete. Operations and activation are in `docs/backup_operations_runbook.md`.

**Configured targets** (`src/deployment/backup_operations.py`; documented, configurable, never reported as measured):
- a daily cycle at 03:30 UTC (`Persistent=true`: one catch-up run), plus an optional cycle after an import;
- RPO 24 h and RTO 4 h;
- retention of 7 daily, 4 weekly and 6 monthly, bucketed in UTC;
- a monthly drill (overdue after 31 days);
- overlap: 4 attempts, 900 s apart, within a 2 h deadline;
- uploads: 3 attempts with backoff.

**Implemented:**
- **`backup_cycle.run_cycle` (`backup-cycle`):**
  - runs the accepted job unchanged, keeping its report and exit code verbatim;
  - retries an overlap within its bounds;
  - uploads only after `published`, and only when enabled;
  - writes one sanitized cycle record with separate `capture` and `offsite` parts.
  - Exits: 0 ok; 3 all attempts overlapped (the systemd unit's `SuccessExitStatus=3`); the capture's own 1/4/5/64/130; 20 off-site failed, 21 uncertain, 22 refused, 23 configuration refused; 65 operations configuration refused.
- **`backup_cycle.backup_status` (`backup-status`, hourly timer):** exit 10 means attention. It alerts on:
  - no remotely verified copy within the RPO;
  - 2 or more consecutive overlap skips;
  - a failed last cycle;
  - an overdue drill.
- **`backup_offsite`:**
  - the accepted verifier must pass as `/2`;
  - a deterministic tar of exactly the four artifact files;
  - `age` encryption to public recipients only (a secret key in the recipients file is refused);
  - an S3-compatible upload (boto3, lazily imported) with bounded attempts;
  - a **full read-back SHA-256** (never an ETag);
  - only then the completion record `offsite.json`, itself read back byte for byte.
  - Outcomes: `stored_verified`, `already_stored`, `refused`, `failed`, `uncertain` (never reported as stored). Each is recorded in `<state>/offsite/`; the capture writer's records are never touched.
  - Retrieval checks, in order: the record, the payload size and SHA-256, decryption with the separately held identity, the plaintext SHA-256, strict extraction (exactly four regular files; no link or traversal), the accepted verifier, and the manifest against the record.
- **`backup_retention` (`backup-retention`):**
  - a deterministic GFS dry-run plan with `plan_sha256`;
  - always keeps the newest complete copy and the newest copy with a passing drill;
  - refuses on an incomplete listing, an incomplete or inconsistent artifact, an unreadable drill record, or no passing drill.
  - Deletion needs `NAPSEDGE_BACKUP_RETENTION_DELETE_ENABLED=true`, `--execute` and the reviewed digest. Each artifact's record is deleted first, then its payload; every deletion is confirmed by re-listing.
- **`tests/postgres/restore_drill.py`** (the recovery-host procedure, test layer, reusing the probe and the accepted 9B-6J/6E helpers):
  - fetches and reopens the off-site copy;
  - restores into a NEW guarded target, refusing an existing or source database;
  - compares against the artifact's saved baseline, with the strict verdict and `monotonic_v1` reported separately and R2 blocking;
  - publishes `drills/<artifact>/<ts>.json`.
  - `passed` requires restore exit 0, every strict check equal, strictly equal sequences, and the policy accepting.
- **Deployment files:**
  - `deploy/systemd/` (a daily timer and one-shot service with an outer timeout, `LoadCredentialEncrypted=` credentials staged to the unit's runtime directory, and `OnFailure=` an inactive journal-only notification hook; an hourly status timer);
  - `deploy/backup/backup.env.example` (placeholders; credentials are file paths).
- **Backup image only:**
  - Debian `age=1.2.1-1+b5`;
  - `requirements-backup.txt` (boto3 1.43.110, botocore 1.43.110, s3transfer 0.19.2, jmespath 1.1.0), installed with the app constraints;
  - a private state directory.
  - The api, streamlit and discord images are unchanged.
- **Connection budget:** measured on the server, the backup job uses at most **3** concurrent backends (budgeted 4); a refused overlapping attempt plus its holder uses at most **2**. One uvicorn worker, one memoized engine per Streamlit and Discord process, and no replicas: **17 of 22**, or 20 of 22 with a concurrent capped import (runbook 9).

**Verified:**

| Run | Result |
|---|---|
| Windows full default suite | 6,546 passed, 21 skipped |
| Linux runner selection (read-only, uid 10001, no network) | 1,344 passed, 2 skipped |
| Native: real capture + real `age` + boto3 over HTTP to moto (an S3 EMULATOR, not B2) + a drill from the DOWNLOADED copy (local artifact deleted first); wrong and missing key; corrupted payload; unsafe target; interrupted real upload (retried, or failed with no record); overlap skip; measured connection use; plus the accepted native capture modules | 55 passed, 2 declared skips |
| Pool caps on the host transport | 3 passed |
| The built image: `backup-cycle` to the emulator with its own `age` and boto3; the private key never mounted; recovery in a separate container with the separate key; the 9B-7A image tests | 6 passed |
| Provider-probe rehearsal on the new images | 3 passed |
| Build-context bytecode module (now including `requirements-backup.txt`) | 4 passed |
| Sensitivity | 6 of 6: record without read-back, size-as-verification, retrieval without the payload digest, traversal/links in extraction, deletion without a passing drill, an off-site failure reported as success (three tests were tightened after a first pass that caught the defects only through non-assertion failures) |
| Clean clone | identical on every run (373b354 + 96-file overlay, images rebuilt from it); its 461 extra Windows skips are git-ignored local data fixtures and an untracked file absent from a fresh clone |

**Labelled substitutes:**
- moto (S3 emulator) for B2;
- HTTP to the emulator (a test-only switch);
- synthetic credentials and throwaway age keys;
- disposable local PostgreSQL 16 clusters.

**Not established:** B2 behaviour (consistency, multipart ETags, Object Lock, key scoping); the provider; production artifact size and timings; the notification channel; Object Lock; recovery readiness.

**Handoff refreshed:**
- `C:\Users\Anthony\napsedge-9b8b-handoff\napsedge-9b8b-handoff.tar`, SHA-256 `c9cee15cd890aa1d205f93a544987c33f9e3b59f82c9a85b7dee9f5fe858f567`.
- 173 source files (113 committed, 11 modified, 49 untracked, each with provenance), plus both 9B-8B images in one archive.
- It is an uncommitted snapshot of 373b354, not a release.
- Re-verified from a fresh unpack: inventory, images, module origin, the probe regression, the rehearsal and the remote commands.
- The 9B-7B package (`C:\Users\Anthony\napsedge-9b7b-handoff`) is superseded and left in place.

## Appendix A -- compact example (synthetic values; digests abbreviated with `...` for display only -- real files hold full 64-character digests)

`manifest.json` (shown pretty-printed; the file is canonical single-line):

```json
{
  "archive": {"format": "custom", "toc_entries": 187, "toc_sha256": "9c1e...e204"},
  "artifact_id": "3f2b8c1e-6a47-4d0e-9b53-1c7a2d9e5f08",
  "baseline": {"row_total": 17, "table_count": 24},
  "consistency": {
    "catalog_bracket_equal": true, "ddl_exclusion": "none", "foreign_write_sessions_end": 0,
    "foreign_write_sessions_start": 0, "mode": "exported_snapshot", "sequence_rule": "monotonic_v1",
    "snapshot_descriptor": "1042:1042:", "snapshot_unchanged_at_end": true
  },
  "created_utc": "2026-10-02T18:30:00.000000Z",
  "files": [
    {"bytes": 48213, "path": "archive.dump", "sha256": "a41d...77c0"},
    {"bytes": 6120, "path": "baseline.json", "sha256": "5be0...19aa"}
  ],
  "finished_utc": "2026-10-02T18:30:03.482911Z",
  "format": "napsedge.backup-artifact/1",
  "repository": {"dirty": false, "git_revision": "373b35452bb47f71d5f85cbca1e0624a9e4139f3"},
  "schema": {"alembic_head": "5b02bea0eb85", "expected_head": "5b02bea0eb85", "heads_match": true},
  "source": {
    "backup_role": "napsedge_backup_ro", "database_name": "napsedge", "database_oid": 16384,
    "logical_name": "napsedge", "pg_dump_version": "pg_dump (PostgreSQL) 16.4",
    "server_version_num": 160004, "system_identifier": "7550123456789012345"
  },
  "tooling": {"name": "napsedge-backup", "version": "0.0.0-example"}
}
```

`baseline.json` (abbreviated: one entry per list shown; real files list every table, constraint, index, ACL and sequence):

```json
{
  "acls": [["teams", "r", "napsedge_migrator", "napsedge_migrator=arwdDxt/napsedge_migrator,napsedge_runtime_ro=r/napsedge_migrator,napsedge_backup_ro=r/napsedge_migrator"]],
  "artifact_id": "3f2b8c1e-6a47-4d0e-9b53-1c7a2d9e5f08",
  "captured_utc": "2026-10-02T18:30:00.210344Z",
  "constraints": [["teams", "pk_teams", "p", true, "PRIMARY KEY (team_id)"]],
  "fingerprints": [["teams", 3, "0d9f4c1a7b6e2f8d3a5c9e1b4f7a2d60"]],
  "format": "napsedge.backup-baseline/1",
  "indexes": [["teams", "pk_teams", "CREATE UNIQUE INDEX pk_teams ON public.teams USING btree (team_id)"]],
  "max_alias_id": 3,
  "sequence_details": [
    {"cache": 1, "column_max": 3, "cycle": false, "data_type": "bigint", "increment": 1, "is_called": true,
     "last_value": 3, "maximum": 9223372036854775807, "minimum": 1, "name": "team_aliases_id_seq",
     "owned_by": ["team_aliases", "id"]}
  ],
  "sequences": [["team_aliases_id_seq", 3]],
  "version": "5b02bea0eb85"
}
```

(Keys appear in sorted order in the real, canonical files; the examples above are illustrative and not produced by any code, because no code exists.)
