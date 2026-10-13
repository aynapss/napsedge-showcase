"""
Phase 9B-6A: the read-only capture preflight (`src/deployment/backup_capture_preflight.py`, specification 13.7)
against one disposable, synthetic PostgreSQL 16 cluster (the existing 9B harness: verified TLS, SCRAM, the three
bootstrap roles, each scenario on its own migrated database with the unchanged 9B-2 backup-role grants).

Every observation runs AS THE BACKUP ROLE. Deviations are introduced by the migrator (the owner) or the cluster
administrator in the scenario's own disposable database, with disposable NOLOGIN roles only; the application,
backup and migrator roles are never altered. Nothing here captures, locks, dumps or restores, and nothing here
establishes artifact consistency or authorizes production capture.
"""
from __future__ import annotations

import contextlib
import threading
import time
import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import DBAPIError

from src.deployment import backup_capture_preflight as preflight_module
from src.deployment.backup_capture_preflight import (
    PUBLIC_GRANTEE,
    CapturePreflight,
    OwnershipFinding,
    UnsupportedObject,
    observe_capture_preflight,
    v1_policy,
)
from src.exceptions import DatabaseTargetVerificationError
from src.pipeline.bulk_import_safety import DatabaseTargetDesignation, build_expected_acknowledgement
from tests.postgres.conftest import TEST_DB_PREFIX
from tests.postgres.test_postgres_backup_sequence_consistency import _INVENTORY_SQL
from tests.postgres.test_postgres_backup_snapshot_consistency import Harness, owned_case
from tests.postgres.test_postgres_fresh_cluster_recovery import bootstrap_sql, launched_cluster, owned_resources, psql_as_admin
from tests.postgres.test_postgres_roles_and_restore import ACCEPTED_ALEMBIC_HEAD, MIGRATOR_ROLE, RUNTIME_ROLE, _create_database_owned_by_migrator
from tests.postgres.test_postgres_tls_backup_restore import (
    BACKUP_ROLE,
    isolated_database_environment,  # noqa: F401  (autouse module fixture: removes inherited PG* / NAPSEDGE_DATABASE_* variables)
    provision_database,
    remove_files,
    write_pgpass,
)

pytestmark = pytest.mark.postgres

LOCK_NOT_AVAILABLE = "55P03"
QUEUE_WAIT_S = 30


def designation_for(database: str) -> DatabaseTargetDesignation:
    return DatabaseTargetDesignation(expected_database_name=database, acknowledgement=build_expected_acknowledgement(database))


@dataclass
class Observed:
    result: CapturePreflight
    transaction_start_before: object
    transaction_start_after: object
    timeout_setting_after: str
    in_transaction_after: bool
    transaction_events: list
    backend_state_after: str | None
    elapsed_s: float


@contextlib.contextmanager
def disposable_role(cluster):
    """A NOLOGIN role that exists only for one scenario; dropped (and confirmed gone) after the scenario's database is dropped."""
    name = f"zz_preflight_{uuid.uuid4().hex[:8]}"
    admin = cluster.admin_engine()
    try:
        with admin.connect() as conn:
            conn.execute(text(f"CREATE ROLE {name} NOLOGIN"))
        yield name
    finally:
        with admin.connect() as conn:
            conn.execute(text(f"DROP ROLE IF EXISTS {name}"))
            assert conn.execute(text("SELECT count(*) FROM pg_roles WHERE rolname = :n"), {"n": name}).scalar() == 0
        admin.dispose()


def migrated_database(cluster, ledger) -> str:
    database = _create_database_owned_by_migrator(cluster)
    ledger.databases.append(database)
    provision_database(cluster, database, seed=True)
    return database


def as_migrator(cluster, ledger, database: str, *statements: str) -> None:
    engine = create_engine(cluster.migrator_url(database))
    ledger.engines.append(engine)
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))


def as_admin(cluster, database: str, *statements: str) -> None:
    admin = cluster.admin_engine(database)
    try:
        with admin.connect() as conn:
            for statement in statements:
                conn.execute(text(statement))
    finally:
        admin.dispose()


def observe_as_backup(cluster, ledger, database: str, *, expected_head: str = ACCEPTED_ALEMBIC_HEAD, name: str | None = None) -> Observed:
    """The backup role opens a transaction, sets a LOCAL timeout, calls the preflight, and the transaction is inspected afterwards (still the caller's)."""
    engine = create_engine(cluster.backup_url(database))
    ledger.engines.append(engine)
    events: list = []
    for kind in ("commit", "rollback", "begin"):
        event.listen(engine, kind, lambda conn, kind=kind: events.append(kind))
    policy = v1_policy(designation_for(name or database), expected_head)
    connection = engine.connect()
    ledger.connections.append(connection)
    connection.begin()
    events.clear()  # only what happens from here on counts
    connection.execute(text("SET LOCAL lock_timeout = '3s'"))
    before = connection.execute(text("SELECT now()")).scalar_one()
    started = time.monotonic()
    result = observe_capture_preflight(connection, policy)
    elapsed = time.monotonic() - started
    after = connection.execute(text("SELECT now()")).scalar_one()
    timeout_after = connection.execute(text("SELECT current_setting('lock_timeout')")).scalar_one()
    pid = connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
    admin = cluster.admin_engine()
    try:
        with admin.connect() as conn:
            state = conn.execute(text("SELECT state FROM pg_stat_activity WHERE pid = :p"), {"p": pid}).scalar()
    finally:
        admin.dispose()
    observed = Observed(result, before, after, timeout_after, connection.in_transaction(), list(events), state, elapsed)
    connection.rollback()
    return observed


# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def harness(docker_available):
    with owned_resources() as resources, launched_cluster(resources) as (cluster, container_id):
        boot_database = f"{TEST_DB_PREFIX}boot_{uuid.uuid4().hex[:8]}"
        pgpass = write_pgpass(cluster, [(cluster.admin_url.username, cluster.admin_password)])
        try:
            boot = psql_as_admin(cluster, pgpass, bootstrap_sql(cluster, boot_database))
            assert boot.returncode == 0, boot.diagnostics()
            yield Harness(cluster, container_id, boot_database)
        finally:
            remove_files(cluster, pgpass)


def test_the_production_sequence_inventory_is_character_for_character_the_validated_test_layer_query():
    assert preflight_module._SEQUENCE_INVENTORY_SQL == _INVENTORY_SQL


def test_a_conforming_migrated_source_is_accepted_as_the_backup_role_and_the_callers_transaction_is_untouched(harness):
    cluster = harness.cluster
    with owned_case(cluster) as ledger:
        database = migrated_database(cluster, ledger)
        observed = observe_as_backup(cluster, ledger, database)
        admin = cluster.admin_engine(database)
        try:
            with admin.connect() as conn:
                expected_oid = conn.execute(text("SELECT oid FROM pg_database WHERE datname = current_database()")).scalar_one()
                expected_version = int(conn.execute(text("SHOW server_version_num")).scalar_one())
                expected_sysid = str(conn.execute(text("SELECT system_identifier FROM pg_control_system()")).scalar_one())
        finally:
            admin.dispose()
    result = observed.result
    assert result.accepted, result
    assert result.verified_target.database_name == database
    assert (result.database_oid, result.database_owner, result.server_version_num, result.server_major) == (expected_oid, MIGRATOR_ROLE, expected_version, 16)
    assert result.alembic_heads == (ACCEPTED_ALEMBIC_HEAD,)
    assert result.ownership_findings == () and result.unsupported_objects == ()
    assert len(result.sequences) == 13 and all(s.applicable and s.validated_configuration for s in result.sequences)
    # system_identifier: the server's own answer for the backup role is recorded, whichever it is; if readable it must be the real one
    if result.system_identifier_readable:
        assert result.system_identifier == expected_sysid
    else:
        assert result.system_identifier is None
    print(f"\nbackup role may execute pg_control_system(): {result.system_identifier_readable}")
    # the caller's transaction: same transaction before and after, still open, its LOCAL setting intact, no commit/rollback/begin issued
    assert observed.transaction_start_before == observed.transaction_start_after
    assert observed.in_transaction_after and observed.timeout_setting_after == "3s"
    assert observed.transaction_events == []
    assert observed.backend_state_after == "idle in transaction"


def test_a_database_name_mismatch_raises_the_existing_verification_error(harness):
    cluster = harness.cluster
    with owned_case(cluster) as ledger:
        database = migrated_database(cluster, ledger)
        with pytest.raises(DatabaseTargetVerificationError):
            observe_as_backup(cluster, ledger, database, name=f"{TEST_DB_PREFIX}not_this_one")


def test_a_head_other_than_the_expected_one_is_not_accepted(harness):
    cluster = harness.cluster
    with owned_case(cluster) as ledger:
        database = migrated_database(cluster, ledger)
        result = observe_as_backup(cluster, ledger, database, expected_head="ffffffffffff").result
    assert result.alembic_heads == (ACCEPTED_ALEMBIC_HEAD,) and not result.head_matches and not result.accepted


def test_a_table_owned_by_a_disposable_role_is_refused(harness):
    cluster = harness.cluster
    with disposable_role(cluster) as role:
        with owned_case(cluster) as ledger:
            database = migrated_database(cluster, ledger)
            as_admin(cluster, database, f"ALTER TABLE public.seasons OWNER TO {role}")
            admin = cluster.admin_engine(database)
            try:
                with admin.connect() as conn:
                    indexes = {r[0] for r in conn.execute(text("SELECT indexrelid::regclass::text FROM pg_index WHERE indrelid = 'public.seasons'::regclass"))}
            finally:
                admin.dispose()
            result = observe_as_backup(cluster, ledger, database).result
    # ALTER TABLE ... OWNER TO moves the table's indexes with it and rewrites its ACL so the NEW owner holds the owner's own entries:
    # every one of those is outside the policy, and nothing else is reported
    expected_owner_findings = {OwnershipFinding("seasons", "r", role)} | {OwnershipFinding(i, "i", role) for i in indexes}
    owner_findings = {f for f in result.ownership_findings if f.grantee is None}
    grantee_findings = {f for f in result.ownership_findings if f.grantee is not None}
    assert indexes and owner_findings == expected_owner_findings
    assert grantee_findings == {OwnershipFinding("seasons", "r", role, grantee=role)}
    assert not result.accepted


def test_a_relation_grant_to_a_non_permitted_role_is_refused(harness):
    cluster = harness.cluster
    with disposable_role(cluster) as role:
        with owned_case(cluster) as ledger:
            database = migrated_database(cluster, ledger)
            as_migrator(cluster, ledger, database, f"GRANT SELECT ON public.teams TO {role}")
            result = observe_as_backup(cluster, ledger, database).result
    assert result.ownership_findings == (OwnershipFinding("teams", "r", MIGRATOR_ROLE, grantee=role),)
    assert not result.accepted


def test_a_public_grant_is_refused(harness):
    cluster = harness.cluster
    with owned_case(cluster) as ledger:
        database = migrated_database(cluster, ledger)
        as_migrator(cluster, ledger, database, "GRANT SELECT ON public.teams TO PUBLIC")
        result = observe_as_backup(cluster, ledger, database).result
    assert result.ownership_findings == (OwnershipFinding("teams", "r", MIGRATOR_ROLE, grantee=PUBLIC_GRANTEE),)
    assert not result.accepted


def test_a_column_grant_to_a_non_permitted_role_is_refused(harness):
    cluster = harness.cluster
    with disposable_role(cluster) as role:
        with owned_case(cluster) as ledger:
            database = migrated_database(cluster, ledger)
            admin = cluster.admin_engine(database)
            try:
                with admin.connect() as conn:
                    column = conn.execute(text(
                        "SELECT attname FROM pg_attribute WHERE attrelid = 'public.teams'::regclass AND attnum > 0 AND NOT attisdropped ORDER BY attnum LIMIT 1"
                    )).scalar_one()
            finally:
                admin.dispose()
            as_migrator(cluster, ledger, database, f'GRANT SELECT ("{column}") ON public.teams TO {role}')
            result = observe_as_backup(cluster, ledger, database).result
    assert result.ownership_findings == (OwnershipFinding("teams", "r", MIGRATOR_ROLE, grantee=role, column=column),)
    assert not result.accepted


def test_a_view_is_an_unsupported_object_and_is_refused(harness):
    cluster = harness.cluster
    with owned_case(cluster) as ledger:
        database = migrated_database(cluster, ledger)
        as_migrator(cluster, ledger, database, "CREATE VIEW public.zz_preflight_view AS SELECT 1 AS one")
        result = observe_as_backup(cluster, ledger, database).result
    assert result.unsupported_objects == (UnsupportedObject("relation:v", "zz_preflight_view"),)
    assert result.ownership_findings == ()
    assert not result.accepted


@pytest.mark.parametrize("alteration, applicable, validated", [
    ("CYCLE", False, False),
    ("INCREMENT BY -1", False, False),
])
def test_r0_inapplicable_sequences_are_refused_the_c5_setups(harness, alteration, applicable, validated):
    cluster = harness.cluster
    with owned_case(cluster) as ledger:
        database = migrated_database(cluster, ledger)
        as_migrator(cluster, ledger, database, f"ALTER SEQUENCE public.team_aliases_id_seq {alteration}")
        result = observe_as_backup(cluster, ledger, database).result
    by_name = {s.name: s for s in result.sequences}
    altered = by_name["team_aliases_id_seq"]
    assert (altered.applicable, altered.validated_configuration) == (applicable, validated)
    assert all(s.applicable and s.validated_configuration for name, s in by_name.items() if name != "team_aliases_id_seq")
    assert not result.sequences_supported and not result.accepted


def test_the_preflight_completes_while_an_access_exclusive_request_is_queued_on_an_application_table(harness):
    """
    A migrator session holds ACCESS SHARE on `teams`; a second migrator session requests ACCESS EXCLUSIVE and is CONFIRMED queued from the
    server's own views. A control read of `teams` by the backup role then times out (55P03), proving the queue blocks ordinary readers;
    the preflight, under the same 3 s lock_timeout, completes. The queued request is cancelled and never commits.
    """
    cluster = harness.cluster
    with owned_case(cluster) as ledger:
        database = migrated_database(cluster, ledger)
        holder_engine = create_engine(cluster.migrator_url(database))
        requester_engine = create_engine(cluster.migrator_url(database))
        ledger.engines += [holder_engine, requester_engine]
        holder = holder_engine.connect()
        ledger.connections.append(holder)
        holder.begin()
        holder.execute(text("LOCK TABLE public.teams IN ACCESS SHARE MODE"))
        outcome: dict = {}

        def request_exclusive() -> None:
            with requester_engine.connect() as conn:
                outcome["pid"] = conn.execute(text("SELECT pg_backend_pid()")).scalar_one()
                conn.rollback()  # end the autobegun transaction so the DDL runs in its own explicit one
                conn.begin()
                conn.execute(text(f"SET LOCAL lock_timeout = '{QUEUE_WAIT_S}s'"))
                try:
                    conn.execute(text("ALTER TABLE public.teams ADD COLUMN zz_preflight_probe integer"))
                    outcome["result"] = "acquired"
                except DBAPIError as exc:
                    outcome["result"] = getattr(exc.orig, "sqlstate", None) or type(exc).__name__
                conn.rollback()

        thread = threading.Thread(target=request_exclusive, name="preflight-exclusive-request")
        ledger.threads.append(thread)
        thread.start()
        admin = cluster.admin_engine(database)
        try:
            queued = False
            deadline = time.monotonic() + QUEUE_WAIT_S
            while time.monotonic() < deadline and not queued:
                with admin.connect() as conn:
                    queued = bool(conn.execute(text(
                        "SELECT count(*) FROM pg_locks WHERE relation = 'public.teams'::regclass AND mode = 'AccessExclusiveLock' AND NOT granted"
                    )).scalar())
                if not queued:
                    time.sleep(0.05)
            assert queued, "the ACCESS EXCLUSIVE request was never observed queued"

            control_engine = create_engine(cluster.backup_url(database))
            ledger.engines.append(control_engine)
            with control_engine.connect() as conn:
                conn.begin()
                conn.execute(text("SET LOCAL lock_timeout = '1s'"))
                with pytest.raises(DBAPIError) as blocked:
                    conn.execute(text("SELECT count(*) FROM public.teams"))
                assert getattr(blocked.value.orig, "sqlstate", None) == LOCK_NOT_AVAILABLE
                conn.rollback()

            observed = observe_as_backup(cluster, ledger, database)
            with admin.connect() as conn:
                still_queued = bool(conn.execute(text(
                    "SELECT count(*) FROM pg_locks WHERE relation = 'public.teams'::regclass AND mode = 'AccessExclusiveLock' AND NOT granted"
                )).scalar())
                conn.execute(text("SELECT pg_cancel_backend(:p)"), {"p": outcome["pid"]})
        finally:
            admin.dispose()
        thread.join(timeout=QUEUE_WAIT_S + 5)
        holder.rollback()
        admin = cluster.admin_engine(database)
        try:
            with admin.connect() as conn:
                column_added = conn.execute(text(
                    "SELECT count(*) FROM pg_attribute WHERE attrelid = 'public.teams'::regclass AND attname = 'zz_preflight_probe'"
                )).scalar()
        finally:
            admin.dispose()
    assert still_queued, "the request must still be queued while the preflight ran"
    assert observed.result.accepted
    assert observed.elapsed_s < 3.0, observed.elapsed_s
    assert outcome["result"] == "57014"  # cancelled by the test, never acquired
    assert column_added == 0
