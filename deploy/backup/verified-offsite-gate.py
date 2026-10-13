"""
Phase 9B-8C: PREPARED, NOT ACTIVE -- the gate in front of the dead-man's-switch heartbeat.

The cycle exits 0 both when the off-site copy was stored AND verified, and when off-site is disabled, so the exit status alone must
not ping. The heartbeat must also describe THIS run, never an earlier successful one, so the gate is bound to the run by position,
not by time:
  - before the cycle starts, the unit records the byte size of `<state>/cycles.jsonl` (ExecStartPre, the "offset");
  - after it stops, the gate reads ONLY the bytes appended since that offset. There must be EXACTLY ONE complete record there
    (a cycle writes exactly one). None (this run wrote no record), more than one (another writer interleaved), a partial line, a
    file shorter than the offset (rewritten or truncated) or a missing offset all refuse.
That one record passes (exit 0) only if ALL hold:
  - format napsedge.backup-cycle/1 and exit_code 0;
  - the capture published (states.locally_published) and the artifact ids of capture and off-site match;
  - offsite.outcome == "stored_verified" (payload read back and matched, completion record written and read back) -- not
    "already_stored", "disabled", "uncertain" or anything else;
  - states.remotely_stored is true, a 64-hex payload_sha256 and a record_key are present.
Otherwise exit 1. It prints one line with the reason; no secret is ever read (the record holds none).
Usage (backup image, no network):  python verified-offsite-gate.py <cycles.jsonl> --offset <bytes before this run>
"""
from __future__ import annotations

import json
import re
import sys

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def check(record: dict) -> str | None:
    """None when the record proves a verified off-site copy; otherwise the reason."""
    if record.get("format") != "napsedge.backup-cycle/1":
        return "unexpected record format"
    if record.get("exit_code") != 0:
        return f"cycle exit {record.get('exit_code')}"
    states = record.get("states") or {}
    offsite = record.get("offsite") or {}
    capture = (record.get("capture") or {}).get("report") or {}
    if states.get("locally_published") is not True:
        return "capture not published"
    if offsite.get("outcome") != "stored_verified":
        return f"off-site outcome {offsite.get('outcome')!r}"
    if states.get("remotely_stored") is not True:
        return "not remotely stored"
    if not _HEX64.match(str(offsite.get("payload_sha256", ""))) or not offsite.get("record_key"):
        return "verified digest or completion record missing"
    if not offsite.get("artifact_id") or offsite.get("artifact_id") != capture.get("artifact_id"):
        return "artifact mismatch between capture and off-site"
    return None


def this_run_record(data: bytes, offset: int) -> tuple[dict | None, str | None]:
    """The single record appended after `offset`, or the reason there is not exactly one."""
    if offset < 0 or len(data) < offset:
        return None, "the cycle log is shorter than the offset (rewritten or truncated)"
    if offset and data[offset - 1:offset] != b"\n":
        return None, "the offset is not at a record boundary"
    appended = data[offset:]
    if not appended:
        return None, "this run wrote no cycle record"
    if not appended.endswith(b"\n"):
        return None, "the appended record is incomplete"
    lines = [line for line in appended.split(b"\n") if line.strip()]
    if len(lines) != 1:
        return None, f"{len(lines)} records were appended during this run (expected exactly 1)"
    try:
        record = json.loads(lines[0].decode("utf-8"))
    except ValueError:
        return None, "the appended record is not valid JSON"
    return (record, None) if isinstance(record, dict) else (None, "the appended record is not an object")


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] != "--offset" or not argv[2].strip().isdigit():
        print("gate: NO PING (usage: verified-offsite-gate.py <cycles.jsonl> --offset <bytes before this run>)")
        return 1 if argv else 2
    try:
        with open(argv[0], "rb") as handle:
            data = handle.read()
    except OSError as exc:
        print(f"gate: NO PING (cannot read the cycle record: {type(exc).__name__})")
        return 1
    record, reason = this_run_record(data, int(argv[2].strip()))
    reason = reason or check(record)
    if reason:
        print(f"gate: NO PING ({reason})")
        return 1
    print("gate: PING (verified off-site copy recorded by this run)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
