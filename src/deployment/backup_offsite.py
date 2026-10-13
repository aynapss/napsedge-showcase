"""
Phase 9B-8B: encrypted off-site copy and retrieval of PUBLISHED backup artifacts.

Upload (`upload_artifact`), in order; any failure stops it and nothing later is claimed:
1. the artifact directory must pass the accepted public verifier (`verify_artifact_directory`) as format /2;
2. its four files (archive.dump, baseline.json, manifest.json, COMPLETE) -- the complete recovery artifact, saved baseline and
   manifest included -- become one deterministic tar (sorted names, fixed metadata, no other entry);
3. the tar is encrypted with `age` to the recipients (PUBLIC keys) in a file; the decryption identity is never needed here;
4. the ciphertext is uploaded to `<prefix>/artifacts/<artifact>/payload.tar.age`, then READ BACK in full and its SHA-256 compared
   with the local ciphertext digest. An object-store ETag is never treated as a content digest. Bounded attempts, with backoff;
5. only after a verified read-back is the completion record `<prefix>/artifacts/<artifact>/offsite.json` written, and it is read
   back and compared byte for byte. A payload without a verified record is "not remotely stored".

Outcomes (`OffsiteResult.outcome`): `stored_verified`, `already_stored`, `refused` (nothing uploaded: unverified artifact or a
conflicting remote record), `failed` (no verified payload within the attempts; no record written), `uncertain` (the payload verified
but the record could not be written or read back -- never reported as stored). Every outcome is written to the LOCAL state directory
(`<state>/offsite/<artifact_id>.json`); the capture writer's records (FAILED, partials, the artifact itself) are never touched.

Retrieval (`fetch_artifact`): record, then payload size and SHA-256 against the record, then decryption with a separately held
identity, then the plaintext digest, then a strictly checked extraction (exactly the four regular files, no links or traversal), then
the accepted verifier, then the manifest's identity against the record. The restore uses only the result.

Version history (Phase 9B-8C; Backblaze B2 keeps every version, and an upload key may overwrite or hide by name): BEFORE anything
is downloaded, `version_history` reads every version and delete marker of the artifact's two objects. Retrieval is REFUSED, unless
the caller explicitly accepts the anomaly, when the completion record has anything but exactly one version and no delete marker,
when the payload changed after verification, when the current payload is missing or hidden, or when unexpected objects sit in the
artifact's folder. "Changed after verification" is decided EXACTLY when the record names the verified payload version
(`payload.version_id`, written by uploads from Phase 9B-8C on): that version must still be the current one. For older records it
falls back to timestamps (any payload version or marker NEWER than the record), which cannot see a change within the same second.
Payload versions older than the verified one are expected (the leftovers of a retried upload) and only reported. These are anomaly checks, NOT proof of authenticity: an artifact forged under
a new name with one version each passes them, and only stronger controls (Object Lock, signed records) would prevent that.

Credentials (store keys) are read from FILES under the job's password-file rules and kept in memory; never in an argument, a log,
an exception message or a record. Store errors are reported by type and service error code only.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from src.deployment.backup_artifact import ARCHIVE_FILE, BASELINE_FILE, MANIFEST_FILE, MARKER_FILE, verify_artifact_directory

OFFSITE_RECORD_FORMAT = "napsedge.backup-offsite/1"
STATE_RECORD_FORMAT = "napsedge.backup-offsite-state/1"
ARTIFACT_FILES = (ARCHIVE_FILE, BASELINE_FILE, MANIFEST_FILE, MARKER_FILE)
PAYLOAD_NAME, RECORD_NAME = "payload.tar.age", "offsite.json"
RECORD_LIMIT = 64 * 1024
TOOL_TIMEOUT_S = 3600
_CHUNK = 1024 * 1024


class OffsiteError(Exception):
    """Fixed text plus type names only; never a credential, a path from content or a raw service message."""


class StoreError(OffsiteError):
    pass


class EncryptionError(OffsiteError):
    pass


class RetrievalRefused(OffsiteError):
    pass


# ----------------------------------------------------------------------------------------------------------- object stores
@dataclasses.dataclass(frozen=True)
class ObjectInfo:
    key: str
    size: int
    version_id: str | None = None  # the current version, when the store is versioned (B2 always is)


@dataclasses.dataclass(frozen=True)
class VersionInfo:
    """One version of one object, or a delete marker (`delete_marker=True`, size 0)."""

    key: str
    version_id: str
    size: int
    last_modified: datetime
    is_latest: bool
    delete_marker: bool


class ObjectStore(Protocol):
    def put_file(self, key: str, path: str) -> None: ...
    def put_bytes(self, key: str, data: bytes) -> None: ...
    def head(self, key: str) -> ObjectInfo | None: ...
    def get_to_file(self, key: str, path: str) -> None: ...
    def get_bytes(self, key: str, limit: int) -> bytes: ...
    def list(self, prefix: str) -> list[ObjectInfo]: ...
    def versions(self, prefix: str) -> list[VersionInfo]: ...
    def delete(self, key: str) -> None: ...
    def delete_version(self, key: str, version_id: str) -> None: ...


class S3ObjectStore:
    """An S3-compatible store (Backblaze B2's S3 API in production; an emulator in tests). boto3 is imported lazily."""

    def __init__(self, *, endpoint_url: str, region: str, bucket: str, key_id_file: str, secret_key_file: str) -> None:
        import boto3
        from botocore.config import Config

        from src.deployment.backup_job import JobConfigurationError, _read_password_file

        try:
            key_id, secret = _read_password_file(key_id_file), _read_password_file(secret_key_file)
        except JobConfigurationError as exc:
            raise StoreError("a store credential file was refused: " + str(exc).replace("NAPSEDGE_BACKUP_DATABASE_PASSWORD_FILE", "credential file")) from None
        self.bucket = bucket
        self._client = boto3.client(
            "s3", endpoint_url=endpoint_url, region_name=region, aws_access_key_id=key_id, aws_secret_access_key=secret,
            config=Config(retries={"max_attempts": 1, "mode": "standard"}, connect_timeout=15, read_timeout=120, signature_version="s3v4",
                          s3={"addressing_style": "path"}, request_checksum_calculation="when_required", response_checksum_validation="when_required"))
        del key_id, secret

    def _call(self, what: str, fn, *args, **kwargs):
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            return fn(*args, **kwargs)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", "unknown"))[:40]
            raise StoreError(f"{what} failed (ClientError {code})") from None
        except BotoCoreError as exc:
            raise StoreError(f"{what} failed ({type(exc).__name__})") from None

    def put_file(self, key: str, path: str) -> None:
        from boto3.s3.transfer import TransferConfig

        cfg = TransferConfig(multipart_threshold=64 * 1024 * 1024, multipart_chunksize=64 * 1024 * 1024, use_threads=False)
        self._call("upload", self._client.upload_file, path, self.bucket, key, Config=cfg)

    def put_bytes(self, key: str, data: bytes) -> None:
        self._call("upload", self._client.put_object, Bucket=self.bucket, Key=key, Body=data)

    def head(self, key: str) -> ObjectInfo | None:
        from botocore.exceptions import ClientError

        try:
            response = self._client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code"))
            if code in ("404", "NoSuchKey", "NotFound"):
                return None
            if code in ("403", "Forbidden", "AccessDenied") and self._absent_by_listing(key):
                return None
            raise StoreError(f"head failed (ClientError {code[:40]})") from None
        version = response.get("VersionId")
        return ObjectInfo(key, int(response["ContentLength"]), str(version) if version and version != "null" else None)

    def _absent_by_listing(self, key: str) -> bool:
        """Phase 9B-8C (observed on real B2): with a prefix-restricted key, HEAD of a MISSING object returns 403, not 404. A 403
        means "missing" ONLY if a listing of exactly this name succeeds and does not contain it; if the listing names the object,
        or fails, the 403 stays a refusal (so a real permission problem can never make an existing object look absent)."""
        page = self._call("list", self._client.list_objects_v2, Bucket=self.bucket, Prefix=key, MaxKeys=1)
        return not any(o.get("Key") == key for o in page.get("Contents", []))

    def get_to_file(self, key: str, path: str) -> None:
        from boto3.s3.transfer import TransferConfig

        self._call("download", self._client.download_file, self.bucket, key, path, Config=TransferConfig(use_threads=False))

    def get_bytes(self, key: str, limit: int) -> bytes:
        response = self._call("download", self._client.get_object, Bucket=self.bucket, Key=key)
        data = response["Body"].read(limit + 1)
        if len(data) > limit:
            raise StoreError("download failed (object larger than its limit)")
        return data

    def list(self, prefix: str) -> list[ObjectInfo]:
        """Every object under `prefix`; raises unless the listing completed (an incomplete inventory is never acted on)."""
        found, token = [], None
        while True:
            kwargs = {"Bucket": self.bucket, "Prefix": prefix, "MaxKeys": 1000, **({"ContinuationToken": token} if token else {})}
            page = self._call("list", self._client.list_objects_v2, **kwargs)
            found += [ObjectInfo(o["Key"], int(o["Size"])) for o in page.get("Contents", [])]
            if not page.get("IsTruncated"):
                return found
            token = page.get("NextContinuationToken")
            if not token:
                raise StoreError("list failed (truncated without a continuation token)")

    def versions(self, prefix: str) -> list[VersionInfo]:
        """Every version AND delete marker under `prefix`; raises unless the listing completed."""
        found: list[VersionInfo] = []
        markers: dict = {}
        while True:
            page = self._call("version list", self._client.list_object_versions, Bucket=self.bucket, Prefix=prefix, MaxKeys=1000, **markers)
            for v in page.get("Versions", []):
                found.append(VersionInfo(v["Key"], str(v.get("VersionId")), int(v.get("Size", 0)), v["LastModified"], bool(v.get("IsLatest")), False))
            for m in page.get("DeleteMarkers", []):
                found.append(VersionInfo(m["Key"], str(m.get("VersionId")), 0, m["LastModified"], bool(m.get("IsLatest")), True))
            if not page.get("IsTruncated"):
                return found
            if not page.get("NextKeyMarker"):
                raise StoreError("version list failed (truncated without a marker)")
            markers = {"KeyMarker": page["NextKeyMarker"], **({"VersionIdMarker": page["NextVersionIdMarker"]} if page.get("NextVersionIdMarker") else {})}

    def delete(self, key: str) -> None:
        """Delete BY NAME: on a versioned store (B2) this only adds a delete marker (hides); the data stays stored."""
        self._call("delete", self._client.delete_object, Bucket=self.bucket, Key=key)

    def delete_version(self, key: str, version_id: str) -> None:
        """Permanently delete ONE version (or delete marker); needs a key with `deleteFiles` on B2."""
        self._call("delete", self._client.delete_object, Bucket=self.bucket, Key=key, VersionId=version_id)


# ------------------------------------------------------------------------------------------------------------- encryption
class AgeTool:
    """The `age` command-line tool (Debian `age` package). Recipients are public keys; the identity is the private key."""

    def __init__(self, executable: str = "/usr/bin/age") -> None:
        self.executable = executable

    def _run(self, argv: list[str], what: str) -> None:
        try:
            result = subprocess.run([self.executable, *argv], stdin=subprocess.DEVNULL, capture_output=True, timeout=TOOL_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise EncryptionError(f"{what} could not run ({type(exc).__name__})") from None
        if result.returncode != 0:
            raise EncryptionError(f"{what} failed (age exit {result.returncode})")

    def version(self) -> str:
        try:
            out = subprocess.run([self.executable, "--version"], capture_output=True, timeout=30, text=True).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return "unknown"
        return out[:40] or "unknown"

    def encrypt(self, source: str, target: str, recipients_file: str) -> None:
        self._run(["--encrypt", "--recipients-file", recipients_file, "--output", target, source], "encryption")

    def decrypt(self, source: str, target: str, identity_file: str) -> None:
        self._run(["--decrypt", "--identity", identity_file, "--output", target, source], "decryption")


def read_recipients(path: str) -> list[str]:
    """The public recipients (`age1...` lines); comments and blank lines ignored. Public keys are safe to record."""
    lines = [line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines()]
    recipients = [line for line in lines if line and not line.startswith("#")]
    if not recipients or any(not r.startswith("age1") or len(r) > 100 for r in recipients):
        raise EncryptionError("the recipients file must hold one or more age1... public keys (and never a secret key)")
    return recipients


# ---------------------------------------------------------------------------------------------------------------- archives
def sha256_file(path: str) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with open(path, "rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def build_archive(artifact_dir: Path, target: str) -> None:
    """Exactly the four artifact files under `<artifact name>/`, sorted, with fixed metadata (byte-stable for the same artifact)."""
    names = sorted(p.name for p in artifact_dir.iterdir())
    if names != sorted(ARTIFACT_FILES):
        raise OffsiteError("the artifact directory does not hold exactly the four artifact files")
    with tarfile.open(target, "w", format=tarfile.USTAR_FORMAT) as tar:
        for name in sorted(ARTIFACT_FILES):
            path = artifact_dir / name
            info = tarfile.TarInfo(f"{artifact_dir.name}/{name}")
            info.size, info.mtime, info.mode, info.uid, info.gid, info.uname, info.gname = path.stat().st_size, 0, 0o600, 0, 0, "", ""
            with open(path, "rb") as handle:
                tar.addfile(info, handle)


def safe_extract(archive: str, destination: Path, expected_name: str, *, max_bytes: int) -> Path:
    """Extracts ONLY `<expected_name>/<one of the four files>` regular files; anything else refuses the whole archive."""
    root = destination / expected_name
    with tarfile.open(archive, "r:") as tar:
        members = tar.getmembers()
        wanted = {f"{expected_name}/{n}" for n in ARTIFACT_FILES}
        if sorted(m.name for m in members) != sorted(wanted) or any(not m.isreg() for m in members):
            raise RetrievalRefused("the decrypted archive is not exactly the four artifact files")
        if sum(m.size for m in members) > max_bytes:
            raise RetrievalRefused("the decrypted archive is larger than its recorded size")
        root.mkdir(mode=0o700)
        for member in members:
            target = root / member.name.split("/", 1)[1]
            source = tar.extractfile(member)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
            with os.fdopen(fd, "wb") as out:
                shutil.copyfileobj(source, out, _CHUNK)
    return root


# --------------------------------------------------------------------------------------------------------- keys and records
def object_keys(prefix: str, artifact_name: str) -> tuple[str, str]:
    base = f"{prefix}/artifacts/{artifact_name}/"
    return base + PAYLOAD_NAME, base + RECORD_NAME


def canonical(data: dict) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def parse_record(data: bytes, artifact_name: str) -> dict:
    try:
        record = json.loads(data.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        raise RetrievalRefused("the completion record is not valid JSON") from None
    ok = (isinstance(record, dict) and record.get("format") == OFFSITE_RECORD_FORMAT and record.get("artifact") == artifact_name
          and record.get("readback_verified") is True and isinstance(record.get("payload"), dict) and isinstance(record.get("plaintext"), dict)
          and all(isinstance(record["payload"].get(k), t) for k, t in (("sha256", str), ("bytes", int), ("key", str)))
          and (("version_id" not in record["payload"]) or (isinstance(record["payload"]["version_id"], str) and record["payload"]["version_id"]))
          and all(isinstance(record["plaintext"].get(k), t) for k, t in (("sha256", str), ("bytes", int)))
          and isinstance(record.get("artifact_id"), str) and isinstance(record.get("created_utc"), str) and isinstance(record.get("manifest_sha256"), str))
    if not ok:
        raise RetrievalRefused("the completion record is malformed")
    return record


@dataclasses.dataclass
class OffsiteResult:
    outcome: str
    artifact: str | None
    artifact_id: str | None
    detail: str = ""
    attempts: int = 0
    payload_sha256: str | None = None
    record_key: str | None = None
    state_file: str | None = None

    def report(self) -> dict:
        return {k: v for k, v in dataclasses.asdict(self).items() if v not in (None, "")}


def _utc(now: Callable[[], datetime]) -> str:
    return now().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_state(state_dir: str, result: OffsiteResult, now: Callable[[], datetime]) -> str:
    """The LOCAL record of this upload attempt (never the capture writer's records)."""
    directory = Path(state_dir) / "offsite"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = (result.artifact_id or "unknown") + ".json"
    tmp = directory / (name + ".tmp")
    tmp.write_bytes(canonical({"format": STATE_RECORD_FORMAT, "recorded_utc": _utc(now), **result.report()}))
    os.replace(tmp, directory / name)
    return str(directory / name)


# ------------------------------------------------------------------------------------------------------------------ upload
def upload_artifact(artifact_dir: str | os.PathLike, *, store: ObjectStore, prefix: str, age: AgeTool, recipients_file: str,
                    workdir: str, state_dir: str, attempts: int = 3, backoff_seconds: float = 30,
                    sleep: Callable[[float], None] = time.sleep, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> OffsiteResult:
    path = Path(artifact_dir)
    verdict = verify_artifact_directory(path)
    if not verdict.valid or verdict.manifest is None or verdict.manifest.format != "napsedge.backup-artifact/2":
        result = OffsiteResult("refused", path.name, None, "the artifact did not pass the accepted verifier as /2: " + ", ".join(verdict.codes)[:200])
        result.state_file = write_state(state_dir, result, now)
        return result
    manifest = verdict.manifest
    payload_key, record_key = object_keys(prefix, path.name)
    result = OffsiteResult("failed", path.name, manifest.artifact_id, record_key=record_key)
    try:
        existing = store.head(record_key)
        if existing is not None:
            try:
                record = parse_record(store.get_bytes(record_key, RECORD_LIMIT), path.name)
            except RetrievalRefused:
                result.outcome, result.detail = "refused", "an unreadable remote record exists for this artifact (manual review; nothing overwritten)"
                return result
            remote = store.head(payload_key)
            if record["artifact_id"] != manifest.artifact_id or remote is None or remote.size != record["payload"]["bytes"]:
                result.outcome, result.detail = "refused", "a conflicting remote record exists for this artifact (manual review; nothing overwritten)"
            else:
                result.outcome, result.payload_sha256, result.detail = "already_stored", record["payload"]["sha256"], "a verified completion record already exists"
            return result
        recipients = read_recipients(recipients_file)
        scratch = Path(tempfile.mkdtemp(prefix="napsedge_offsite_", dir=workdir))
        try:
            plain, cipher, readback = str(scratch / "artifact.tar"), str(scratch / PAYLOAD_NAME), str(scratch / "readback")
            build_archive(path, plain)
            plain_sha, plain_bytes = sha256_file(plain)
            age.encrypt(plain, cipher, recipients_file)
            os.remove(plain)  # the unencrypted copy never outlives encryption
            cipher_sha, cipher_bytes = sha256_file(cipher)
            verified, payload_version = False, None
            for attempt in range(1, attempts + 1):
                result.attempts = attempt
                try:
                    store.put_file(payload_key, cipher)
                    info = store.head(payload_key)
                    if info is not None and info.size == cipher_bytes:
                        if os.path.exists(readback):
                            os.remove(readback)
                        store.get_to_file(payload_key, readback)
                        after = store.head(payload_key)  # the version read back must be the one bound into the record
                        if sha256_file(readback) == (cipher_sha, cipher_bytes) and after is not None and after.version_id == info.version_id:
                            verified, payload_version = True, info.version_id
                            break
                    result.detail = "the read-back did not match the uploaded bytes"
                except StoreError as exc:
                    result.detail = str(exc)
                if attempt < attempts:
                    sleep(backoff_seconds * attempt)
            if not verified:
                result.outcome = "failed"
                return result
            manifest_sha, _ = sha256_file(str(path / MANIFEST_FILE))
            record = {"format": OFFSITE_RECORD_FORMAT, "artifact": path.name, "artifact_id": manifest.artifact_id, "created_utc": manifest.created_utc,
                      "manifest_sha256": manifest_sha,
                      "payload": {"key": payload_key, "bytes": cipher_bytes, "sha256": cipher_sha, **({"version_id": payload_version} if payload_version else {})},
                      "plaintext": {"bytes": plain_bytes, "sha256": plain_sha, "layout": "tar of the four artifact files"},
                      "encryption": {"tool": "age", "version": age.version(), "recipients": recipients},
                      "readback_verified": True, "uploaded_utc": _utc(now), "attempts": result.attempts}
            body = canonical(record)
            try:
                store.put_bytes(record_key, body)
                confirmed = store.get_bytes(record_key, RECORD_LIMIT) == body
            except StoreError as exc:
                confirmed, result.detail = False, str(exc)
            result.payload_sha256 = cipher_sha
            if confirmed:
                result.outcome, result.detail = "stored_verified", "payload read back and matched; completion record written and read back"
            else:
                result.outcome = "uncertain"
                result.detail = "the payload verified but the completion record was not confirmed: NOT remotely stored (" + result.detail[:120] + ")"
            return result
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
    except (OffsiteError, OSError) as exc:
        result.outcome = "failed"
        result.detail = str(exc)[:200] if isinstance(exc, OffsiteError) else f"local file error ({type(exc).__name__})"
        return result
    finally:
        result.state_file = write_state(state_dir, result, now)


# --------------------------------------------------------------------------------------------------------- version history
def analyse_versions(entries: list[VersionInfo], prefix: str, artifact_name: str, payload_version_id: str | None = None) -> dict:
    """The version history of ONE artifact's folder (see the module docstring). Anomaly checks only; never proof of authenticity."""
    payload_key, record_key = object_keys(prefix, artifact_name)
    record_v = [e for e in entries if e.key == record_key]
    payload_v = [e for e in entries if e.key == payload_key]
    others = sorted({e.key for e in entries} - {record_key, payload_key})
    record_data = [e for e in record_v if not e.delete_marker]
    payload_data = [e for e in payload_v if not e.delete_marker]
    anomalies: list[str] = []
    # a folder that never had a completion record (an interrupted or failed upload) is INCOMPLETE, not an anomaly: nothing was
    # ever verified there. It is still never retrieved (no record) and never deleted automatically (retention refuses on it).
    incomplete = not record_v and not any(e.delete_marker for e in payload_v)
    if any(e.delete_marker for e in record_v):
        anomalies.append("the completion record has a delete marker (it was hidden)")
    if len(record_data) > 1 or (record_v and not record_data):
        anomalies.append(f"the completion record has {len(record_data)} versions (expected exactly 1)")
    written = record_data[0].last_modified if len(record_data) == 1 else None
    before = 0
    if payload_version_id:
        binding = "version_id"
        bound = [e for e in payload_data if e.version_id == payload_version_id]
        if not bound:
            anomalies.append("the payload version named by the completion record no longer exists")
        elif not bound[0].is_latest:
            anomalies.append("the payload changed after its completion record was written (the verified version is no longer current)")
        else:
            before = len(payload_data)
    elif written is not None:
        binding = "timestamps (second precision; a change within the same second is not visible)"
        later = [e for e in payload_v if e.last_modified > written]
        if later:
            anomalies.append(f"the payload changed after its completion record was written ({len(later)} later version(s) or delete marker(s))")
        before = sum(1 for e in payload_data if e.last_modified <= written)
    else:
        binding = "none (no single completion record)"
    current = [e for e in payload_v if e.is_latest]
    if not incomplete and (not current or current[0].delete_marker):
        anomalies.append("the current payload is missing or hidden")
    if others:
        anomalies.append(f"{len(others)} unexpected object(s) in the artifact's folder")
    return {"anomalies": anomalies, "incomplete": incomplete, "record_versions": len(record_data), "payload_versions": len(payload_data),
            "payload_versions_before_record": before, "binding": binding,
            "delete_markers": sum(1 for e in entries if e.delete_marker), "other_objects": len(others),
            "note": "anomaly checks only; not proof of authenticity or immutability"}


def _recorded_version(store: ObjectStore, entries: list[VersionInfo], prefix: str, artifact_name: str) -> tuple[str | None, str | None]:
    """(the payload version id the CURRENT completion record names, or None; an error text if the record cannot be read)."""
    record_key = object_keys(prefix, artifact_name)[1]
    if not any(e.key == record_key and e.is_latest and not e.delete_marker for e in entries):
        return None, None
    try:
        return parse_record(store.get_bytes(record_key, RECORD_LIMIT), artifact_name)["payload"].get("version_id"), None
    except OffsiteError as exc:
        return None, f"the completion record cannot be read ({exc})"


def _analyse(store: ObjectStore, entries: list[VersionInfo], prefix: str, artifact_name: str) -> dict:
    version_id, error = _recorded_version(store, entries, prefix, artifact_name)
    history = analyse_versions(entries, prefix, artifact_name, version_id)
    if error:
        history["anomalies"].append(error)
    return history


def version_history(store: ObjectStore, prefix: str, artifact_name: str) -> dict:
    return _analyse(store, store.versions(f"{prefix}/artifacts/{artifact_name}/"), prefix, artifact_name)


def version_inventory(store: ObjectStore, prefix: str) -> dict[str, dict]:
    """The version history of EVERY artifact folder, including hidden ones that a plain listing no longer shows."""
    grouped: dict[str, list[VersionInfo]] = {}
    base = f"{prefix}/artifacts/"
    for entry in store.versions(base):
        grouped.setdefault(entry.key[len(base):].partition("/")[0], []).append(entry)
    return {name: _analyse(store, entries, prefix, name) for name, entries in sorted(grouped.items())}


# --------------------------------------------------------------------------------------------------------------- retrieval
@dataclasses.dataclass
class Fetched:
    artifact_dir: Path
    record: dict
    verdict: object
    history: dict = dataclasses.field(default_factory=dict)


def fetch_artifact(artifact_name: str, *, store: ObjectStore, prefix: str, age: AgeTool, identity_file: str, destination: str,
                   accept_version_anomalies: bool = False) -> Fetched:
    """Download, verify, decrypt and reopen ONE remote artifact into `destination` (a private, empty directory).
    The version history is checked FIRST; an anomaly refuses unless `accept_version_anomalies` is explicitly true (then it is still
    reported in `Fetched.history`, and every digest check below still applies)."""
    payload_key, record_key = object_keys(prefix, artifact_name)
    history = version_history(store, prefix, artifact_name)
    history["anomalies_accepted"] = bool(history["anomalies"] and accept_version_anomalies)
    if history["anomalies"] and not accept_version_anomalies:
        raise RetrievalRefused("version history anomaly (possible replacement or hiding after verification): " + "; ".join(history["anomalies"])
                               + ". Nothing was downloaded; inspect the versions manually, or accept the anomaly explicitly")
    record = parse_record(store.get_bytes(record_key, RECORD_LIMIT), artifact_name)
    if record["payload"]["key"] != payload_key:
        raise RetrievalRefused("the completion record names a different payload")
    info = store.head(payload_key)
    if info is None or info.size != record["payload"]["bytes"]:
        raise RetrievalRefused("the payload is missing or its size differs from the completion record")
    work = Path(destination)
    cipher, plain = str(work / PAYLOAD_NAME), str(work / "artifact.tar")
    store.get_to_file(payload_key, cipher)
    if sha256_file(cipher) != (record["payload"]["sha256"], record["payload"]["bytes"]):
        raise RetrievalRefused("the downloaded payload's SHA-256 differs from the completion record (corrupt or replaced)")
    age.decrypt(cipher, plain, identity_file)
    os.remove(cipher)
    if sha256_file(plain) != (record["plaintext"]["sha256"], record["plaintext"]["bytes"]):
        raise RetrievalRefused("the decrypted archive's SHA-256 differs from the completion record")
    artifact_dir = safe_extract(plain, work, artifact_name, max_bytes=record["plaintext"]["bytes"])
    os.remove(plain)
    verdict = verify_artifact_directory(artifact_dir)
    if not verdict.valid or verdict.manifest is None:
        raise RetrievalRefused("the retrieved artifact did not pass the accepted verifier: " + ", ".join(verdict.codes)[:200])
    if verdict.manifest.artifact_id != record["artifact_id"] or sha256_file(str(artifact_dir / MANIFEST_FILE))[0] != record["manifest_sha256"]:
        raise RetrievalRefused("the retrieved manifest is not the one the completion record names")
    return Fetched(artifact_dir, record, verdict, history)


def inventory(store: ObjectStore, prefix: str) -> dict:
    """Every remote artifact by name: which objects exist and their sizes (the listing must complete, or StoreError)."""
    found: dict[str, dict] = {}
    for obj in store.list(f"{prefix}/artifacts/"):
        rest = obj.key[len(prefix) + len("/artifacts/"):]
        name, _, leaf = rest.partition("/")
        found.setdefault(name, {})[leaf] = obj.size
    drills: dict[str, list[str]] = {}
    for obj in store.list(f"{prefix}/drills/"):
        rest = obj.key[len(prefix) + len("/drills/"):]
        name, _, leaf = rest.partition("/")
        drills.setdefault(name, []).append(leaf)
    return {"artifacts": found, "drills": drills}


__all__ = ["OFFSITE_RECORD_FORMAT", "ObjectStore", "S3ObjectStore", "AgeTool", "OffsiteResult", "upload_artifact", "fetch_artifact",
           "inventory", "version_history", "version_inventory", "analyse_versions", "VersionInfo", "object_keys", "parse_record", "canonical", "build_archive", "safe_extract", "read_recipients", "sha256_file",
           "StoreError", "EncryptionError", "RetrievalRefused", "OffsiteError", "ObjectInfo"]
