"""
Phase 9B-5A: the baseline adapter against REAL captures.

The regression tests in tests/regression/test_deployment_backup_artifact.py use
synthetic baselines whose Python shapes were copied from an inspection of real
ones. This module removes the copy: it captures a genuine `DatabaseBaseline`
from a disposable, migrated PostgreSQL (existing `pg_migrated_database_url`
fixture, nothing new), pushes it through the production schema as bytes, reads
it back, and requires (a) exact equality with the original and (b) that the
UNCHANGED comparator still reports no difference when handed the deserialized
baseline.

`sequence_details` is synthetic here (derived from the captured sequence names):
capturing the real values is a later, separately authorized unit, and nothing
here reads, evaluates or asserts the sequence rule.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine

from src.db.repositories import ConferenceRepository, TeamAliasRepository, TeamRepository
from src.db.session import build_engine, build_session_factory, unit_of_work
from src.deployment.backup_artifact import SequenceDetail, parse_baseline
from tests.postgres.baseline_document import baseline_from_document, baseline_to_document
from tests.postgres.pg_restore_compare import capture_baseline, compare_to_baseline

pytestmark = pytest.mark.postgres

ARTIFACT_ID = "3f2b8c1e-6a47-4d0e-9b53-1c7a2d9e5f08"
CAPTURED = "2026-10-02T18:30:00.210344Z"


def synthetic_details(baseline) -> tuple[SequenceDetail, ...]:
    big = 9223372036854775807
    details = []
    for name, last_value in baseline.sequences:
        table = name[: -len("_id_seq")] if name.endswith("_id_seq") else name
        details.append(SequenceDetail(name, (table, "id"), "bigint", 1, 1, big, 1, False, last_value, last_value is not None, None))
    return tuple(details)


def test_a_real_capture_round_trips_through_the_production_schema_and_still_compares_clean(pg_migrated_database_url):
    admin = create_engine(pg_migrated_database_url)
    engine = build_engine(pg_migrated_database_url)
    try:
        with unit_of_work(build_session_factory(engine)) as session:
            ConferenceRepository(session).upsert(conference_id=1, canonical_name="Synthetic Conference")
            TeamRepository(session).upsert(team_id=5, display_name="Synthetic Team", sr_slug="synthetic-team", conference_id=1)
            TeamAliasRepository(session).upsert(team_id=5, source="sr", raw_value="Synthetic Team X", normalized_value="synthetic team x")

        captured = capture_baseline(admin)

        # the shapes the adapter and the schema were written against are the shapes real captures have
        assert isinstance(captured.fingerprints, dict) and all(isinstance(v, tuple) and len(v) == 2 for v in captured.fingerprints.values())
        md5_kinds = {"empty" if md5 == "empty" else "hex32" for _, md5 in captured.fingerprints.values()}
        assert md5_kinds == {"empty", "hex32"}, "expected both empty tables and populated ones in this capture"
        assert any(last is None for _, last in captured.sequences) and any(isinstance(last, int) for _, last in captured.sequences)
        assert isinstance(captured.max_alias_id, int) and isinstance(captured.version, str)
        assert all(isinstance(row, tuple) for rows in (captured.acls, captured.constraints, captured.indexes, captured.sequences) for row in rows)
        assert all(isinstance(row[3], bool) for row in captured.constraints)

        document = baseline_to_document(captured, artifact_id=ARTIFACT_ID, captured_utc=CAPTURED, sequence_details=synthetic_details(captured))
        wire = document.to_bytes()
        assert wire == json.dumps(json.loads(wire), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"

        restored, _ = baseline_from_document(parse_baseline(wire))
        assert restored == captured, "a real capture did not survive the production schema unchanged"

        comparison = compare_to_baseline(restored, admin)  # the comparator, unchanged, on the deserialized baseline
        assert comparison.fingerprint_diffs == {}
        assert comparison.constraint_diff == {} and comparison.constraint_genuine_diff == {}
        assert comparison.index_diff == {}
        assert comparison.owners_equal is True and comparison.acls_equal is True
        assert comparison.sequence_source == comparison.sequence_restored
        assert comparison.source_version == comparison.restored_version
    finally:
        engine.dispose()
        admin.dispose()
