"""
Phase 9B-1: least-privilege role separation and a synthetic backup/restore
drill, against the existing disposable PostgreSQL test framework only.

Scope (and non-scope)
---------------------
* Reuses `pg_container`/`pg_version` from `tests/postgres/conftest.py`
  unmodified. Every role and database this module creates lives only on that
  session-scoped, randomly-credentialed `testcontainers` instance and is
  removed again; nothing here ever connects to, or removes, any named Docker
  volume or any real database.
* Two roles are tested: a NON-superuser migrator (owns the database, runs
  `alembic upgrade head`, and is the only role that grants anything) and a
  separate READ-ONLY runtime role. An application *writer* role is
  deliberately deferred until an accepted write workflow requires one; the
  existing write-oriented tests keep their own credentials.
* Role names are the contract names (`napsedge_migrator`,
  `napsedge_runtime_ro`). Roles are cluster-global, so the fixture refuses to
  run unless the cluster is the disposable one (admin name carries the
  conftest's `pgtest_` prefix) and refuses to reuse a pre-existing role.
* The runtime role's `SELECT` set is not guessed: `REQUIRED_READ_TABLES` was
  derived by inspecting the accepted call paths (`check_readiness`,
  `TeamRepository.get_by_id` + the lazily loaded `Team.conference`,
  `PlayerRepository.get_by_id`, `PlayerStatSnapshotRepository.list_for_player`)
  and is then confirmed empirically here by capturing every SQL statement
  those paths issue under runtime credentials.
* Runtime evidence is a real, password-authenticated connection from the host
  (through the container's published port, which the image's pg_hba.conf
  covers with scram-sha-256). `SET ROLE` is never used as evidence.
  `pg_dump`/`pg_restore` run *inside* the container over localhost, which that
  same pg_hba.conf trusts -- so those two commands prove behaviour as the
  migrator role but are NOT claimed as password-authenticated.
* `alembic/env.py` prefers `NAPSEDGE_DATABASE_URL` over a URL set on the
  Config. Every Alembic call here therefore runs with that variable removed,
  so this module can never migrate anything but its own disposable database.
"""
from __future__ import annotations

import contextlib
import os
import re
import secrets
import subprocess
import uuid
from dataclasses import dataclass, field

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.exc import DBAPIError, OperationalError

from tests.postgres.conftest import TEST_DB_PREFIX, alembic_config_for, assert_disposable_database_name
from tests.postgres.pg_restore_compare import (
    acl_snapshot as _acl_snapshot,
    all_fingerprints as _all_fingerprints,
    canonicalize_restored_check,
    compare_databases,
    describe_diff as _describe_diff,
    escape_for_alembic,
    public_tables as _public_tables,
    rows as _rows,
    scalar as _scalar,
    table_fingerprint as _table_fingerprint,
)

pytestmark = pytest.mark.postgres

MIGRATOR_ROLE = "napsedge_migrator"
RUNTIME_ROLE = "napsedge_runtime_ro"
ACCEPTED_ALEMBIC_HEAD = "5b02bea0eb85"

#: Derived from inspecting the accepted read paths, then confirmed by
#: `test_accepted_read_paths_succeed_under_runtime_credentials`, which fails if
#: the SQL those paths actually issue differs from this set in either direction.
REQUIRED_READ_TABLES = frozenset({"alembic_version", "conferences", "players", "player_stat_snapshots", "teams"})

INSUFFICIENT_PRIVILEGE = "42501"
_SAFE_SECRET = re.compile(r"[A-Za-z0-9_-]+")
_SAFE_IDENT = re.compile(r"[a-z_][a-z0-9_]*")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def _literal(secret: str) -> str:
    """Inline a generated password into DDL (CREATE ROLE cannot take bind parameters). Only a strict alphabet is ever accepted."""
    assert _SAFE_SECRET.fullmatch(secret), "refusing to inline a non-token-alphabet secret"
    return f"'{secret}'"


def _ident(name: str) -> str:
    assert _SAFE_IDENT.fullmatch(name) or name.startswith(TEST_DB_PREFIX), f"unexpected identifier {name!r}"
    return f'"{name}"'


def _as_string(url: URL) -> str:
    return url.render_as_string(hide_password=False)


@contextlib.contextmanager
def _without_database_env():
    """Removes NAPSEDGE_DATABASE_URL for the duration of an Alembic call (see module docstring) and always restores it."""
    saved = os.environ.pop("NAPSEDGE_DATABASE_URL", None)
    try:
        yield
    finally:
        if saved is not None:
            os.environ["NAPSEDGE_DATABASE_URL"] = saved


@dataclass(frozen=True)
class RoleCluster:
    container: object
    admin_url: URL
    admin_password: str
    migrator_password: str
    runtime_password: str

    def url(self, user: str, password: str, database: str) -> URL:
        return self.admin_url.set(username=user, password=password, database=database)

    def migrator_url(self, database: str) -> URL:
        return self.url(MIGRATOR_ROLE, self.migrator_password, database)

    def runtime_url(self, database: str) -> URL:
        return self.url(RUNTIME_ROLE, self.runtime_password, database)

    def admin_engine(self, database: str | None = None, *, autocommit: bool = True) -> Engine:
        url = self.admin_url if database is None else self.admin_url.set(database=database)
        return create_engine(url, isolation_level="AUTOCOMMIT") if autocommit else create_engine(url)

    def secrets_to_scrub(self) -> tuple[str, ...]:
        return (self.admin_password, self.migrator_password, self.runtime_password)


@dataclass
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str

    def diagnostics(self) -> str:
        return f"exit={self.returncode} argv={' '.join(self.argv[:6])}... stderr={self.stderr.strip()[-600:]!r}"


def _run_in_container(cluster: RoleCluster, args: list[str], *, user: str, password: str, timeout: int = 300) -> CommandResult:
    """
    `docker exec` into the disposable container. The password reaches the
    process through the environment (`-e PGPASSWORD` with no value forwards the
    client's), never through argv, and every captured byte is scrubbed of all
    three generated secrets before it can appear in a failure message.
    """
    container_id = cluster.container.get_wrapped_container().id
    env = {**os.environ, "PGPASSWORD": password}
    argv = ["docker", "exec", "-e", "PGPASSWORD", container_id, *args]
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
    scrub = lambda s: _scrub(s, cluster)  # noqa: E731
    return CommandResult(argv=[a for a in argv if a != container_id], returncode=completed.returncode,
                         stdout=scrub(completed.stdout), stderr=scrub(completed.stderr))


def _scrub(value: str, cluster: RoleCluster) -> str:
    for secret in cluster.secrets_to_scrub():
        value = value.replace(secret, "***")
    return value


def _sqlstate(excinfo: pytest.ExceptionInfo) -> str | None:
    return getattr(excinfo.value.orig, "sqlstate", None)


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def role_cluster(pg_container, pg_version):
    admin_url = make_url(pg_container.get_connection_url())
    assert admin_url.username.startswith("pgtest_"), "refusing to create roles on a cluster that is not the disposable test instance"
    assert_disposable_database_name(admin_url.database)
    migrator_password = secrets.token_urlsafe(24)
    runtime_password = secrets.token_urlsafe(24)
    cluster = RoleCluster(
        container=pg_container, admin_url=admin_url, admin_password=admin_url.password,
        migrator_password=migrator_password, runtime_password=runtime_password,
    )
    engine = cluster.admin_engine()
    try:
        with engine.connect() as conn:
            existing = conn.execute(
                text("SELECT rolname FROM pg_roles WHERE rolname IN (:m, :r)"), {"m": MIGRATOR_ROLE, "r": RUNTIME_ROLE}
            ).fetchall()
            assert not existing, f"refusing to reuse pre-existing roles: {existing}"
            conn.execute(text(
                f"CREATE ROLE {MIGRATOR_ROLE} LOGIN PASSWORD {_literal(migrator_password)} "
                "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
            ))
            conn.execute(text(
                f"CREATE ROLE {RUNTIME_ROLE} LOGIN PASSWORD {_literal(runtime_password)} "
                "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
            ))
        yield cluster
    finally:
        with engine.connect() as conn:
            conn.execute(text(f"DROP ROLE IF EXISTS {RUNTIME_ROLE}"))
            conn.execute(text(f"DROP ROLE IF EXISTS {MIGRATOR_ROLE}"))
        engine.dispose()


def _create_database_owned_by_migrator(cluster: RoleCluster) -> str:
    name = f"{TEST_DB_PREFIX}{uuid.uuid4().hex[:16]}"
    assert_disposable_database_name(name)
    engine = cluster.admin_engine()
    try:
        with engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}" OWNER {MIGRATOR_ROLE}'))
    finally:
        engine.dispose()
    return name


def _drop_database(cluster: RoleCluster, name: str) -> None:
    assert_disposable_database_name(name)
    engine = cluster.admin_engine()
    try:
        with engine.connect() as conn:
            conn.execute(text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :db AND pid <> pg_backend_pid()"
            ), {"db": name})
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
    finally:
        engine.dispose()


def provision_database_level_privileges(cluster: RoleCluster, database: str) -> None:
    """
    Database- and schema-level hardening, run by the database OWNER (the
    migrator) -- no superuser needed. This is exactly the step a per-database
    `pg_dump` does NOT carry, so recovery must re-run it (see the drill).
    """
    assert_disposable_database_name(database)
    engine = create_engine(cluster.migrator_url(database))
    try:
        with engine.begin() as conn:
            conn.execute(text(f'REVOKE ALL ON DATABASE "{database}" FROM PUBLIC'))
            conn.execute(text(f'GRANT CONNECT ON DATABASE "{database}" TO {RUNTIME_ROLE}'))
            conn.execute(text("REVOKE ALL ON SCHEMA public FROM PUBLIC"))
            conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {RUNTIME_ROLE}"))
    finally:
        engine.dispose()


def grant_runtime_read_access(cluster: RoleCluster, database: str) -> None:
    """Table-level grants, run by the migrator (the table owner) after migrations. Exactly the accepted read set; nothing else."""
    assert_disposable_database_name(database)
    engine = create_engine(cluster.migrator_url(database))
    try:
        with engine.begin() as conn:
            for table in sorted(REQUIRED_READ_TABLES):
                conn.execute(text(f'GRANT SELECT ON TABLE public."{table}" TO {RUNTIME_ROLE}'))
    finally:
        engine.dispose()


def migrate_as_migrator(cluster: RoleCluster, database: str) -> None:
    assert_disposable_database_name(database)
    cfg = alembic_config_for(escape_for_alembic(_as_string(cluster.migrator_url(database))))
    with _without_database_env():
        command.upgrade(cfg, "head")


def alembic_check_as_migrator(cluster: RoleCluster, database: str) -> None:
    assert_disposable_database_name(database)
    cfg = alembic_config_for(escape_for_alembic(_as_string(cluster.migrator_url(database))))
    with _without_database_env():
        command.check(cfg)


# deterministic seed ---------------------------------------------------------
CONFERENCE_ID = 910001
SEASON_ID = 2026
PLAYER_NAMES = ("Drill Player One", "Drill Player Two")
EXTRA_TEAM_ID = 910002
UNAFFILIATED_TEAM_ID = 910003


def seed_synthetic_records(cluster: RoleCluster, database: str) -> dict:
    """
    Deterministic, synthetic, relationship-bearing records written through the
    existing repositories as the migrator (never the runtime role, never
    admin). Duke uses the real canonical identity so the accepted
    `resolve_team` path is exercised end to end.
    """
    from src.db.allstats_repositories import PlayerStatSnapshotRepository
    from src.db.repositories import (
        ConferenceRepository, DataSourceRepository, PlayerAliasRepository, PlayerRepository,
        SeasonRepository, SourceArtifactRepository, TeamAliasRepository, TeamRepository,
    )
    from src.db.session import build_engine, build_session_factory, unit_of_work
    from src.identity.player_identity import build_player_identity
    from src.identity.team_identity import ensure_team_registry, resolve_team_identity

    ensure_team_registry()
    duke = resolve_team_identity("duke", source="test")
    engine = build_engine(_as_string(cluster.migrator_url(database)))
    factory = build_session_factory(engine)
    seeded: dict = {"duke_team_id": duke.team_id, "player_ids": []}
    try:
        with unit_of_work(factory) as s:
            ConferenceRepository(s).upsert(conference_id=CONFERENCE_ID, canonical_name="Drill Conference")
            TeamRepository(s).upsert(team_id=duke.team_id, display_name=duke.display_name, sr_slug="duke", conference_id=CONFERENCE_ID)
            TeamRepository(s).upsert(team_id=EXTRA_TEAM_ID, display_name="Drill Team Two", sr_slug="drill-team-two", conference_id=CONFERENCE_ID)
            TeamRepository(s).upsert(team_id=UNAFFILIATED_TEAM_ID, display_name="Drill Team Three", sr_slug="drill-team-three")
            for source, raw in (("sr", "Duke Blue Devils"), ("espn", "Duke"), ("odds", "Duke Blue Devils (Odds)")):
                TeamAliasRepository(s).upsert(team_id=duke.team_id, source=source, raw_value=raw, normalized_value=raw.lower())
            SeasonRepository(s).upsert(season_id=SEASON_ID, start_year=SEASON_ID - 1, end_year=SEASON_ID, label=f"{SEASON_ID - 1}-{SEASON_ID}")
            DataSourceRepository(s).upsert(source_key="sr_allstats", display_name="SR AllStats", acquisition_type="live_scraped_html", is_rescrapable=True)
        artifact_ids = []
        with unit_of_work(factory) as s:
            for suffix in ("a", "b"):
                artifact = SourceArtifactRepository(s).upsert(
                    source_key="sr_allstats", source_root_key="teamdump_cbb",
                    relative_path=f"duke/Roster_{SEASON_ID}_{suffix}.xlsx", original_filename=f"Roster_{SEASON_ID}.xlsx",
                    file_type="xlsx", content_hash=f"drill-hash-{suffix}", team_id=duke.team_id, season_id=SEASON_ID,
                    validation_status="valid", point_in_time_classification="historical_but_not_point_in_time",
                )
                artifact_ids.append(artifact.id)
        for name in PLAYER_NAMES:
            identity = build_player_identity(name, duke.team_id, SEASON_ID)
            seeded["player_ids"].append(identity.player_id)
            with unit_of_work(factory) as s:
                PlayerRepository(s).upsert(player_id=identity.player_id, display_name=identity.display_name, team_id=duke.team_id, season_id=SEASON_ID)
                PlayerAliasRepository(s).upsert(player_id=identity.player_id, source="espn", raw_value=name.upper(), normalized_value=name.lower())
        rows = (
            (0, "PerGame", {"pts": 16.1, "trb": 9.8, "ast": 3.0}),
            (0, "Totals", {"pts": 483.0, "trb": 294.0, "ast": 90.0}),
            (1, "PerGame", {"pts": 8.4, "trb": 4.2, "ast": 5.5}),
        )
        for player_index, stat_table, values in rows:
            with unit_of_work(factory) as s:
                PlayerStatSnapshotRepository(s).upsert(
                    team_id=duke.team_id, season_id=SEASON_ID, source_artifact_id=artifact_ids[player_index], stat_table=stat_table,
                    player_key=PLAYER_NAMES[player_index].lower(), source_player_name=PLAYER_NAMES[player_index],
                    validation_status="valid", player_id=seeded["player_ids"][player_index], **values,
                )
    finally:
        engine.dispose()
    return seeded


@dataclass
class SourceDatabase:
    name: str
    seeded: dict = field(default_factory=dict)


@pytest.fixture(scope="module")
def source_database(role_cluster):
    name = _create_database_owned_by_migrator(role_cluster)
    try:
        provision_database_level_privileges(role_cluster, name)
        migrate_as_migrator(role_cluster, name)
        grant_runtime_read_access(role_cluster, name)
        seeded = seed_synthetic_records(role_cluster, name)
        yield SourceDatabase(name=name, seeded=seeded)
    finally:
        _drop_database(role_cluster, name)


@pytest.fixture()
def runtime_engine(role_cluster, source_database):
    engine = create_engine(role_cluster.runtime_url(source_database.name))
    try:
        yield engine
    finally:
        engine.dispose()


# --------------------------------------------------------------------------
# catalog helpers (admin connection, read-only queries)
# --------------------------------------------------------------------------
# catalog helpers live in tests/postgres/pg_restore_compare.py (shared with the 9B-2 module)


# --------------------------------------------------------------------------
# the runtime boundary battery -- run against the source AND the restored DB
# --------------------------------------------------------------------------
_DENIED_STATEMENTS = (
    ("insert_teams", "INSERT INTO teams (team_id, display_name, sr_slug) VALUES (999999, 'x', 'x-denied')"),
    ("update_teams", "UPDATE teams SET display_name = 'tampered'"),
    ("delete_teams", "DELETE FROM teams"),
    ("truncate_teams", "TRUNCATE teams"),
    ("update_alembic_version", "UPDATE alembic_version SET version_num = 'tampered'"),
    ("insert_alembic_version", "INSERT INTO alembic_version (version_num) VALUES ('tampered')"),
    ("delete_alembic_version", "DELETE FROM alembic_version"),
    ("lock_table_exclusive", "LOCK TABLE teams IN ACCESS EXCLUSIVE MODE"),
    ("select_ungranted_table", "SELECT count(*) FROM source_artifacts"),
    ("select_ungranted_pipeline_runs", "SELECT count(*) FROM pipeline_runs"),
    ("create_table", "CREATE TABLE zz_denied (id integer)"),
    ("create_temp_table", "CREATE TEMP TABLE zz_denied_temp (id integer)"),
    ("create_schema", "CREATE SCHEMA zz_denied_schema"),
    ("create_function", "CREATE FUNCTION public.zz_denied() RETURNS integer LANGUAGE sql AS 'SELECT 1'"),
    ("alter_table_add_column", "ALTER TABLE teams ADD COLUMN zz_denied integer"),
    ("alter_table_owner", f"ALTER TABLE teams OWNER TO {RUNTIME_ROLE}"),
    ("create_index", "CREATE INDEX zz_denied_idx ON teams (display_name)"),
    ("drop_table", "DROP TABLE teams"),
    ("drop_alembic_version", "DROP TABLE alembic_version"),
    ("nextval_on_sequence", "SELECT nextval(pg_get_serial_sequence('team_aliases', 'id'))"),
    ("create_role", "CREATE ROLE zz_denied_role"),
    ("alter_role_superuser", f"ALTER ROLE {RUNTIME_ROLE} SUPERUSER"),
    ("alter_system", "ALTER SYSTEM SET work_mem = '64MB'"),
    ("set_role_migrator", f"SET ROLE {MIGRATOR_ROLE}"),
    ("set_session_authorization_migrator", f"SET SESSION AUTHORIZATION {MIGRATOR_ROLE}"),
    ("copy_to_program", "COPY teams TO PROGRAM 'true'"),
    ("read_server_file", "SELECT pg_read_file('/etc/hostname')"),
)


def exercise_accepted_read_paths(engine: Engine, seeded: dict) -> set[str]:
    """
    Runs every accepted database-backed read path through `engine` and returns
    the set of tables the SQL it issued referenced. Used both to prove the
    paths work under runtime credentials and to confirm the grant set.
    """
    from src.db.session import build_session_factory
    from src.services.health import check_readiness
    from src.services.player import get_player_raw_statistics
    from src.services.team import get_team, resolve_team

    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _capture(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        statements.append(statement)

    factory = build_session_factory(engine)
    readiness = check_readiness(engine)
    assert readiness.ready is True, readiness

    with factory() as session:
        resolved = resolve_team(session, "duke")
        assert resolved.outcome == "found" and resolved.team.canonical_name == "Duke"
        assert resolved.team.conference_name == "Drill Conference"  # forces the lazy `conferences` read
        by_id = get_team(session, seeded["duke_team_id"])
        assert by_id.outcome == "found" and by_id.team.canonical_slug == "duke"
        player = get_player_raw_statistics(session, seeded["player_ids"][0])
        assert player.outcome == "found" and len(player.stats) == 2
        assert sorted(s.pts for s in player.stats) == [16.1, 483.0]
        assert get_player_raw_statistics(session, "no-such-player").outcome == "not_found"

    event.remove(engine, "before_cursor_execute", _capture)
    tables: set[str] = set()
    for statement in statements:
        for match in re.finditer(r'\b(?:from|join)\s+"?([a-z_][a-z0-9_]*)"?', statement.lower()):
            tables.add(match.group(1))
    return tables - {"pg_catalog", "information_schema"}


def assert_runtime_boundary(cluster: RoleCluster, database: str) -> None:
    """The full permission-boundary battery. Same function for the source database and for the restored one."""
    admin = cluster.admin_engine(database)
    runtime = create_engine(cluster.runtime_url(database))
    try:
        baseline = _all_fingerprints(admin)

        # --- 1. genuine authentication, not role switching ------------------------
        with runtime.connect() as conn:
            current_user, session_user = conn.execute(text("SELECT current_user, session_user")).one()
            assert (current_user, session_user) == (RUNTIME_ROLE, RUNTIME_ROLE)
            assert _scalar(
                admin, "SELECT count(*) FROM pg_stat_activity WHERE usename = :u AND datname = :d", u=RUNTIME_ROLE, d=database
            ) >= 1, "no backend authenticated as the runtime role was visible to the admin"
        bad_password = create_engine(cluster.url(RUNTIME_ROLE, "definitely-not-the-password", database))
        try:
            with pytest.raises(OperationalError):
                bad_password.connect().close()
        finally:
            bad_password.dispose()

        # --- 2. role attributes and memberships cannot bypass the boundary --------
        attrs = _rows(
            admin,
            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolcanlogin FROM pg_roles WHERE rolname = :r",
            r=RUNTIME_ROLE,
        )[0]
        assert attrs == (False, False, False, False, False, True), f"runtime role attributes unexpected: {attrs}"
        assert _rows(admin, "SELECT 1 FROM pg_auth_members WHERE member = (SELECT oid FROM pg_roles WHERE rolname = :r)", r=RUNTIME_ROLE) == [], "runtime has role membership(s)"
        for role in (
            MIGRATOR_ROLE, "pg_read_all_data", "pg_write_all_data", "pg_execute_server_program", "pg_read_server_files",
            "pg_write_server_files", "pg_monitor", "pg_signal_backend", "pg_database_owner", cluster.admin_url.username,
        ):
            assert _scalar(admin, "SELECT pg_has_role(:r, :t, 'MEMBER')", r=RUNTIME_ROLE, t=role) is False, f"role membership: runtime is a member of {role}"
        assert _scalar(admin, "SELECT count(*) FROM pg_class WHERE relowner = (SELECT oid FROM pg_roles WHERE rolname = :r)", r=RUNTIME_ROLE) == 0, "runtime owns a relation"
        assert _scalar(admin, "SELECT count(*) FROM pg_namespace WHERE nspowner = (SELECT oid FROM pg_roles WHERE rolname = :r)", r=RUNTIME_ROLE) == 0, "runtime owns a schema"
        assert _scalar(admin, "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = :d", d=database) == MIGRATOR_ROLE, "the migrator must own the database"
        migrator_attrs = _rows(
            admin,
            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls FROM pg_roles WHERE rolname = :r",
            r=MIGRATOR_ROLE,
        )[0]
        assert migrator_attrs == (False, False, False, False, False), "the migrator must not be a superuser"

        # --- 3. ownership: the migrator owns everything; nothing is owned by admin/runtime
        owners = {o for (o,) in _rows(admin, "SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname = 'public'")}
        assert owners == {MIGRATOR_ROLE}, owners
        seq_owners = {o for (o,) in _rows(admin, "SELECT DISTINCT sequenceowner FROM pg_sequences WHERE schemaname = 'public'")}
        assert seq_owners <= {MIGRATOR_ROLE}, seq_owners

        # --- 4. grants on existing objects match the accepted read set exactly ----
        granted = {
            t for t in _public_tables(admin)
            if _scalar(admin, "SELECT has_table_privilege(:r, :t, 'SELECT')", r=RUNTIME_ROLE, t=f"public.{t}")
        }
        assert granted == REQUIRED_READ_TABLES, f"granted={sorted(granted)}"
        for table in _public_tables(admin):
            for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER", "SELECT WITH GRANT OPTION"):
                assert _scalar(admin, "SELECT has_table_privilege(:r, :t, :p)", r=RUNTIME_ROLE, t=f"public.{table}", p=privilege) is False, (table, privilege)
        for (sequence,) in _rows(admin, "SELECT sequencename FROM pg_sequences WHERE schemaname = 'public'"):
            for privilege in ("USAGE", "SELECT", "UPDATE"):
                assert _scalar(admin, "SELECT has_sequence_privilege(:r, :s, :p)", r=RUNTIME_ROLE, s=f"public.{sequence}", p=privilege) is False, f"runtime holds {privilege} on sequence {sequence}"
        assert _scalar(admin, "SELECT has_schema_privilege(:r, 'public', 'USAGE')", r=RUNTIME_ROLE) is True, "runtime lacks USAGE on schema public"
        assert _scalar(admin, "SELECT has_schema_privilege(:r, 'public', 'CREATE')", r=RUNTIME_ROLE) is False, "runtime can CREATE in schema public"
        assert _scalar(admin, "SELECT has_database_privilege(:r, :d, 'CONNECT')", r=RUNTIME_ROLE, d=database) is True, "runtime lacks CONNECT on the database"
        assert _scalar(admin, "SELECT has_database_privilege(:r, :d, 'CREATE')", r=RUNTIME_ROLE, d=database) is False, "runtime holds CREATE on database"
        assert _scalar(admin, "SELECT has_database_privilege(:r, :d, 'TEMPORARY')", r=RUNTIME_ROLE, d=database) is False, "runtime holds TEMPORARY on database"
        public_grants = _scalar(
            admin,
            "SELECT count(*) FROM pg_class c, aclexplode(c.relacl) a "
            "WHERE c.relnamespace = 'public'::regnamespace AND a.grantee = 0",
        )
        assert public_grants == 0, "PUBLIC holds privileges on a public-schema relation"
        assert _scalar(admin, "SELECT count(*) FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
                              "WHERE c.relnamespace = 'public'::regnamespace AND a.attacl IS NOT NULL") == 0, "unexpected column-level ACLs"

        # --- 5. default privileges for future migrator-created objects ------------
        runtime_oid_grants = _scalar(
            admin,
            "SELECT count(*) FROM pg_default_acl d, aclexplode(d.defaclacl) a WHERE a.grantee = (SELECT oid FROM pg_roles WHERE rolname = :r)",
            r=RUNTIME_ROLE,
        )
        assert runtime_oid_grants == 0, "a default ACL would auto-grant future objects to the runtime role"
        migrator = create_engine(cluster.migrator_url(database))
        try:
            with migrator.begin() as conn:
                conn.execute(text("CREATE TABLE zz_future_object (id integer)"))
            try:
                assert _scalar(admin, "SELECT has_table_privilege(:r, 'public.zz_future_object', 'SELECT')", r=RUNTIME_ROLE) is False
                acl = _scalar(admin, "SELECT relacl::text FROM pg_class WHERE oid = 'public.zz_future_object'::regclass")
                assert acl is None, f"a brand-new migrator-created table carries a non-default ACL: {acl}"
                with runtime.connect() as conn, pytest.raises(DBAPIError) as excinfo:
                    conn.execute(text("SELECT * FROM zz_future_object"))
                assert _sqlstate(excinfo) == INSUFFICIENT_PRIVILEGE
            finally:
                with migrator.begin() as conn:
                    conn.execute(text("DROP TABLE IF EXISTS zz_future_object"))
        finally:
            migrator.dispose()

        # --- 6. denied operations, each over a fresh runtime connection -----------
        for label, statement in _DENIED_STATEMENTS:
            # ALTER SYSTEM is rejected inside a transaction block (25001) before any privilege check.
            connection_options = {"isolation_level": "AUTOCOMMIT"} if label == "alter_system" else {}
            with runtime.connect() as conn:
                conn = conn.execution_options(**connection_options)
                with pytest.raises(DBAPIError) as excinfo:
                    conn.execute(text(statement))
                assert _sqlstate(excinfo) == INSUFFICIENT_PRIVILEGE, f"{label}: sqlstate={_sqlstate(excinfo)} ({excinfo.value.orig})"

        # --- 7. the attempts changed nothing --------------------------------------
        assert _all_fingerprints(admin) == baseline, "a denied statement nevertheless altered stored data"
        assert "zz_denied" not in {t for t in _public_tables(admin)}
    finally:
        runtime.dispose()
        admin.dispose()


# --------------------------------------------------------------------------
# permission tests on the source database
# --------------------------------------------------------------------------
def test_runtime_boundary_on_source_database(role_cluster, source_database):
    """Covers: genuine authentication, attributes, memberships, ownership, grants, default privileges, 27 denied operations (all SQLSTATE 42501), and no side effects."""
    assert_runtime_boundary(role_cluster, source_database.name)


def test_migrator_can_run_alembic_without_superuser_and_check_is_clean(role_cluster, source_database):
    alembic_check_as_migrator(role_cluster, source_database.name)
    admin = role_cluster.admin_engine(source_database.name)
    try:
        assert _scalar(admin, "SELECT version_num FROM alembic_version") == ACCEPTED_ALEMBIC_HEAD
    finally:
        admin.dispose()


def test_migrator_can_write_but_never_as_superuser(role_cluster, source_database):
    """Positive control: the migrator owns the data (so the runtime denials above are about the runtime role, not a broken database)."""
    engine = create_engine(role_cluster.migrator_url(source_database.name))
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT current_user, session_user")).one() == (MIGRATOR_ROLE, MIGRATOR_ROLE)
            assert conn.execute(text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")).scalar() is False
            assert conn.execute(text("SELECT count(*) FROM teams")).scalar() == 3
            with pytest.raises(DBAPIError) as excinfo:
                conn.execute(text("CREATE ROLE zz_migrator_must_not"))
            assert _sqlstate(excinfo) == INSUFFICIENT_PRIVILEGE
    finally:
        engine.dispose()


def test_accepted_read_paths_succeed_under_runtime_credentials(runtime_engine, source_database):
    tables = exercise_accepted_read_paths(runtime_engine, source_database.seeded)
    assert tables == REQUIRED_READ_TABLES, (
        f"grant set and the SQL the accepted paths actually issue differ: "
        f"missing_from_grants={sorted(tables - REQUIRED_READ_TABLES)} granted_but_unused={sorted(REQUIRED_READ_TABLES - tables)}"
    )


def test_api_readiness_and_team_and_player_routes_under_runtime_credentials(role_cluster, source_database, monkeypatch):
    import hashlib

    from fastapi.testclient import TestClient

    from src.api.main import create_app

    token = secrets.token_urlsafe(24)
    monkeypatch.setenv("NAPSEDGE_API_KEY_HASHES_JSON", f'{{"drill-client": "{hashlib.sha256(token.encode()).hexdigest()}"}}')
    monkeypatch.delenv("NAPSEDGE_DATABASE_URL", raising=False)
    app = create_app(database_url=_as_string(role_cluster.runtime_url(source_database.name)))
    headers = {"Authorization": f"Bearer {token}"}
    try:
        client = TestClient(app)
        assert client.get("/v1/ready").status_code == 200
        team = client.get("/v1/teams/resolve", params={"query": "duke"}, headers=headers)
        assert team.status_code == 200 and team.json()["canonical_name"] == "Duke"
        player = client.get(f"/v1/players/{source_database.seeded['player_ids'][0]}/raw-statistics", headers=headers)
        assert player.status_code == 200
    finally:
        app.state.engine.dispose()


def test_streamlit_controller_lookups_under_runtime_credentials(runtime_engine, source_database):
    from src.db.session import build_session_factory
    from src.streamlit_consumer_controller import run_player_raw_statistics_lookup, run_team_lookup

    factory = build_session_factory(runtime_engine)
    team = run_team_lookup("duke", session_factory=factory)
    assert team.status == "ok", team
    player = run_player_raw_statistics_lookup(source_database.seeded["player_ids"][0], session_factory=factory)
    assert player.status == "ok", player


# --------------------------------------------------------------------------
# the recovery drill
# --------------------------------------------------------------------------
@dataclass
class RecoveryReport:
    source: str
    restored: str
    dump_path: str
    dump: CommandResult
    listing: CommandResult
    dump_size: int
    restore: CommandResult
    carried_owners_equal: bool = False
    carried_acls_equal: bool = False
    db_temp_privilege_before_reprovision: bool | None = None
    db_connect_public_before_reprovision: bool | None = None
    fingerprint_diffs: dict = field(default_factory=dict)
    constraint_diff: dict = field(default_factory=dict)
    constraint_rendering_only: dict = field(default_factory=dict)
    constraint_genuine_diff: dict = field(default_factory=dict)
    index_diff: dict = field(default_factory=dict)
    sequence_source: list = field(default_factory=list)
    sequence_restored: list = field(default_factory=list)
    unvalidated_constraints: list = field(default_factory=list)
    restored_version: str | None = None
    source_version: str | None = None
    source_max_alias_id: int = 0

    def require_restore_succeeded(self) -> None:
        assert self.dump.returncode == 0, f"pg_dump failed: {self.dump.diagnostics()}"
        assert self.listing.returncode == 0, f"pg_restore --list failed: {self.listing.diagnostics()}"
        assert self.restore.returncode == 0, f"pg_restore failed: {self.restore.diagnostics()}"


def perform_recovery_drill(cluster: RoleCluster, source: SourceDatabase) -> RecoveryReport:
    dump_path = f"/tmp/napsedge_drill_{uuid.uuid4().hex[:12]}.dump"
    migrator, pw = MIGRATOR_ROLE, cluster.migrator_password
    dump = _run_in_container(
        cluster, ["pg_dump", "-h", "localhost", "-p", "5432", "-U", migrator, "-d", source.name, "-Fc", "--no-password", "-f", dump_path],
        user=migrator, password=pw,
    )
    size_result = _run_in_container(cluster, ["sh", "-c", f"wc -c < {dump_path}"], user=migrator, password=pw)
    listing = _run_in_container(cluster, ["pg_restore", "--list", dump_path], user=migrator, password=pw)
    dump_size = int(size_result.stdout.strip() or 0) if size_result.returncode == 0 else 0

    restored = _create_database_owned_by_migrator(cluster)
    restore = _run_in_container(
        cluster,
        ["pg_restore", "-h", "localhost", "-p", "5432", "-U", migrator, "-d", restored, "--no-password",
         "--exit-on-error", "--single-transaction", dump_path],
        user=migrator, password=pw,
    )
    report = RecoveryReport(source=source.name, restored=restored, dump_path=dump_path, dump=dump, listing=listing,
                            dump_size=dump_size, restore=restore)
    if not (dump.returncode == 0 and listing.returncode == 0 and restore.returncode == 0):
        return report

    src_admin, dst_admin = cluster.admin_engine(source.name), cluster.admin_engine(restored)
    try:
        # Recovery step that must be explicit: re-apply database-level privileges (see below for what the dump did NOT carry).
        report.db_temp_privilege_before_reprovision = _scalar(
            dst_admin, "SELECT has_database_privilege(:r, :d, 'TEMPORARY')", r=RUNTIME_ROLE, d=restored
        )
        report.db_connect_public_before_reprovision = _scalar(
            dst_admin, "SELECT count(*) FROM pg_database d, aclexplode(coalesce(d.datacl, acldefault('d', d.datdba))) a "
                       "WHERE d.datname = :d AND a.grantee = 0 AND a.privilege_type = 'CONNECT'", d=restored
        ) > 0
        provision_database_level_privileges(cluster, restored)

        comparison = compare_databases(src_admin, dst_admin)
        report.carried_owners_equal = comparison.owners_equal
        report.carried_acls_equal = comparison.acls_equal
        report.fingerprint_diffs = comparison.fingerprint_diffs
        report.constraint_diff = comparison.constraint_diff
        report.constraint_rendering_only = comparison.constraint_rendering_only
        report.constraint_genuine_diff = comparison.constraint_genuine_diff
        report.index_diff = comparison.index_diff
        report.sequence_source, report.sequence_restored = comparison.sequence_source, comparison.sequence_restored
        report.unvalidated_constraints = comparison.unvalidated_constraints
        report.source_version, report.restored_version = comparison.source_version, comparison.restored_version
        report.source_max_alias_id = comparison.source_max_alias_id
    finally:
        src_admin.dispose()
        dst_admin.dispose()
    return report


@pytest.fixture(scope="module")
def recovery(role_cluster, source_database):
    report = perform_recovery_drill(role_cluster, source_database)
    try:
        yield report
    finally:
        _drop_database(role_cluster, report.restored)
        _run_in_container(role_cluster, ["rm", "-f", report.dump_path], user=MIGRATOR_ROLE, password=role_cluster.migrator_password)


def test_backup_archive_is_created_listed_and_non_empty(recovery):
    assert recovery.dump.returncode == 0, recovery.dump.diagnostics()
    assert recovery.dump.stderr.strip() == "", recovery.dump.diagnostics()
    assert recovery.dump_size > 0
    assert recovery.listing.returncode == 0, recovery.listing.diagnostics()
    for table in sorted(REQUIRED_READ_TABLES - {"alembic_version"}):
        assert re.search(rf"TABLE public {table} ", recovery.listing.stdout), f"{table} missing from the archive table of contents"
    assert re.search(r"TABLE DATA public teams ", recovery.listing.stdout)
    assert re.search(r"SEQUENCE SET public team_aliases_id_seq", recovery.listing.stdout)


def test_restore_into_a_fresh_database_exited_cleanly(recovery):
    recovery.require_restore_succeeded()
    assert recovery.restore.stderr.strip() == "", recovery.restore.diagnostics()
    assert recovery.restored != recovery.source


def test_restored_counts_and_stored_values_match_for_every_table(recovery):
    recovery.require_restore_succeeded()
    assert recovery.fingerprint_diffs == {}, f"restored data differs: {recovery.fingerprint_diffs}"


def test_restored_data_includes_the_seeded_relationships(role_cluster, recovery):
    recovery.require_restore_succeeded()
    admin = role_cluster.admin_engine(recovery.restored)
    try:
        counts = {t: _table_fingerprint(admin, t)[0] for t in ("conferences", "teams", "team_aliases", "players", "player_aliases", "source_artifacts", "player_stat_snapshots")}
        assert counts == {"conferences": 1, "teams": 3, "team_aliases": 3, "players": 2, "player_aliases": 2, "source_artifacts": 2, "player_stat_snapshots": 3}
        assert _scalar(admin, "SELECT count(*) FROM teams t JOIN conferences c ON c.conference_id = t.conference_id") == 2
        assert _scalar(admin, "SELECT count(*) FROM teams WHERE conference_id IS NULL") == 1
        assert _scalar(admin, "SELECT count(*) FROM players p LEFT JOIN teams t ON t.team_id = p.team_id WHERE t.team_id IS NULL") == 0
        assert _scalar(admin, "SELECT count(*) FROM player_stat_snapshots s LEFT JOIN players p ON p.player_id = s.player_id WHERE p.player_id IS NULL") == 0
        assert _scalar(admin, "SELECT count(*) FROM player_stat_snapshots s LEFT JOIN source_artifacts a ON a.id = s.source_artifact_id WHERE a.id IS NULL") == 0
        assert sorted(r[0] for r in _rows(admin, "SELECT pts FROM player_stat_snapshots WHERE stat_table = 'PerGame'")) == [8.4, 16.1]
    finally:
        admin.dispose()


def test_restored_constraints_and_indexes_match_and_are_validated(recovery):
    recovery.require_restore_succeeded()
    # Strict: every constraint exists on both sides with the same table, name, type and validation state,
    # and only the documented varchar IN-list rendering family may differ textually -- and only in rendering.
    assert recovery.constraint_genuine_diff == {}, _describe_diff(recovery.constraint_genuine_diff)
    assert set(recovery.constraint_rendering_only) == set(recovery.constraint_diff)
    assert recovery.index_diff == {}, _describe_diff(recovery.index_diff)
    assert recovery.unvalidated_constraints == []


def test_check_constraint_rendering_difference_is_exactly_the_one_known_rewrite(recovery):
    """Pins the observed pg_dump/pg_restore re-rendering of varchar IN-list CHECKs: the restored text, rewritten by the one permitted rule, is byte-identical to the source text."""
    recovery.require_restore_succeeded()
    assert recovery.constraint_rendering_only, "expected the known rendering family to be present (it is what the comparator is built for)"
    for key, (src, dst) in recovery.constraint_rendering_only.items():
        assert src[4] != dst[4], key
        assert canonicalize_restored_check(dst[4]) == src[4], key


def test_comparator_detects_a_genuinely_tampered_check_constraint_on_a_live_database(role_cluster, source_database, mutation_database):
    """
    Live control for the shared comparator: take two real, migrated databases
    (identical schema, different data), tamper ONE CHECK on the second, and
    require the genuine-difference bucket to contain exactly that constraint
    and nothing else. Then revert and require it to be empty again, so the
    detection is caused by the tamper alone.
    """
    key = ("data_sources", "ck_data_sources_acquisition_type")
    src_admin, dst_admin = role_cluster.admin_engine(source_database.name), role_cluster.admin_engine(mutation_database)
    try:
        definition = _scalar(dst_admin, "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = 'data_sources'::regclass AND conname = :n", n=key[1])
        assert definition and "manual" in definition, f"unexpected definition for {key}: {definition!r}"
        before = compare_databases(src_admin, dst_admin)
        assert before.constraint_genuine_diff == {}, _describe_diff(before.constraint_genuine_diff)

        tampered = definition.replace(", 'manual'::character varying", "")
        assert tampered != definition
        with dst_admin.connect() as conn:
            conn.execute(text(f'ALTER TABLE public.data_sources DROP CONSTRAINT "{key[1]}"'))
            conn.execute(text(f'ALTER TABLE public.data_sources ADD CONSTRAINT "{key[1]}" {tampered}'))
        try:
            after = compare_databases(src_admin, dst_admin)
            assert set(after.constraint_genuine_diff) == {key}, _describe_diff(after.constraint_genuine_diff)
            assert after.constraint_rendering_only == {}, "a changed allowed-value set must never be classified as a rendering difference"
        finally:
            with dst_admin.connect() as conn:
                conn.execute(text(f'ALTER TABLE public.data_sources DROP CONSTRAINT "{key[1]}"'))
                conn.execute(text(f'ALTER TABLE public.data_sources ADD CONSTRAINT "{key[1]}" {definition}'))
        restored = compare_databases(src_admin, dst_admin)
        assert restored.constraint_genuine_diff == {}, _describe_diff(restored.constraint_genuine_diff)
    finally:
        src_admin.dispose()
        dst_admin.dispose()


def test_restored_constraints_are_actually_enforced(role_cluster, recovery):
    """Not merely present in the catalog: a violating write must still be rejected on the restored database."""
    recovery.require_restore_succeeded()
    engine = create_engine(role_cluster.migrator_url(recovery.restored))
    try:
        with engine.connect() as conn:
            with pytest.raises(DBAPIError) as fk:
                conn.execute(text("INSERT INTO players (player_id, display_name, team_id, season_id) VALUES ('orphan', 'Orphan', 424242, 2026)"))
            assert _sqlstate(fk) == "23503"
        with engine.connect() as conn:
            with pytest.raises(DBAPIError) as unique:
                conn.execute(text(
                    "INSERT INTO team_aliases (team_id, source, raw_value, normalized_value) "
                    "SELECT team_id, source, raw_value, normalized_value FROM team_aliases LIMIT 1"
                ))
            assert _sqlstate(unique) == "23505"
    finally:
        engine.dispose()


def test_restored_check_constraints_still_reject_invalid_values(role_cluster, recovery):
    recovery.require_restore_succeeded()
    engine = create_engine(role_cluster.migrator_url(recovery.restored))
    try:
        with engine.connect() as conn:
            with pytest.raises(DBAPIError) as bad:
                conn.execute(text(
                    "INSERT INTO data_sources (source_key, display_name, acquisition_type, is_rescrapable) "
                    "VALUES ('zz_bad_check', 'Bad', 'definitely_not_allowed', false)"
                ))
            assert _sqlstate(bad) == "23514"
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO data_sources (source_key, display_name, acquisition_type, is_rescrapable) "
                "VALUES ('zz_good_check', 'Good', 'manual', false)"
            ))
            conn.execute(text("DELETE FROM data_sources WHERE source_key = 'zz_good_check'"))
    finally:
        engine.dispose()


def test_restored_sequences_continue_past_existing_ids(role_cluster, recovery):
    recovery.require_restore_succeeded()
    assert recovery.sequence_source == recovery.sequence_restored, (recovery.sequence_source, recovery.sequence_restored)
    assert any(name == "team_aliases_id_seq" and last_value for name, last_value in recovery.sequence_restored)
    engine = create_engine(role_cluster.migrator_url(recovery.restored))
    try:
        with engine.begin() as conn:
            new_id = conn.execute(text(
                "INSERT INTO team_aliases (team_id, source, raw_value, normalized_value) "
                "SELECT team_id, 'drill', 'sequence probe', 'sequence probe' FROM teams LIMIT 1 RETURNING id"
            )).scalar()
        assert new_id > recovery.source_max_alias_id, (new_id, recovery.source_max_alias_id)
    finally:
        engine.dispose()


def test_restore_carried_ownership_and_acls_but_not_database_level_privileges(recovery):
    """
    Documents exactly what a per-database custom-format dump does and does not
    preserve, so recovery is never declared complete on faith:
    carried = object ownership and table/sequence ACLs (including the runtime
    role's grants); NOT carried = database-level ACLs (PUBLIC CONNECT/TEMPORARY
    revocation, the runtime CONNECT grant). Those are re-applied by the
    explicit `provision_database_level_privileges` step in the drill.
    """
    recovery.require_restore_succeeded()
    assert recovery.carried_owners_equal is True
    assert recovery.carried_acls_equal is True
    assert recovery.db_temp_privilege_before_reprovision is True, (
        "expected the restored database to show PUBLIC's default TEMPORARY privilege before re-provisioning"
    )
    assert recovery.db_connect_public_before_reprovision is True


def test_restored_database_alembic_head_check_and_readiness(role_cluster, recovery):
    recovery.require_restore_succeeded()
    from src.services.health import check_readiness

    with _without_database_env():
        expected_heads = set(ScriptDirectory.from_config(alembic_config_for("postgresql+psycopg://unused")).get_heads())
    assert expected_heads == {ACCEPTED_ALEMBIC_HEAD}
    assert recovery.source_version == recovery.restored_version == ACCEPTED_ALEMBIC_HEAD
    alembic_check_as_migrator(role_cluster, recovery.restored)
    runtime = create_engine(role_cluster.runtime_url(recovery.restored))
    try:
        assert check_readiness(runtime).ready is True
    finally:
        runtime.dispose()


def test_restored_database_passes_the_same_runtime_boundary_and_accepted_reads(role_cluster, source_database, recovery):
    recovery.require_restore_succeeded()
    assert_runtime_boundary(role_cluster, recovery.restored)
    runtime = create_engine(role_cluster.runtime_url(recovery.restored))
    try:
        tables = exercise_accepted_read_paths(runtime, source_database.seeded)
        assert tables == REQUIRED_READ_TABLES
    finally:
        runtime.dispose()


def test_restore_failures_are_not_swallowed(role_cluster, source_database, recovery):
    """Negative controls: the drill's exit-code handling genuinely fails on a bad restore, and a failed restore changes nothing."""
    recovery.require_restore_succeeded()
    migrator, pw = MIGRATOR_ROLE, role_cluster.migrator_password
    admin = role_cluster.admin_engine(recovery.restored)
    try:
        before = _all_fingerprints(admin)
        again = _run_in_container(
            role_cluster,
            ["pg_restore", "-h", "localhost", "-U", migrator, "-d", recovery.restored, "--no-password",
             "--exit-on-error", "--single-transaction", recovery.dump_path],
            user=migrator, password=pw,
        )
        assert again.returncode != 0, "restoring into an already-populated database must fail with --exit-on-error"
        assert "already exists" in again.stderr
        assert _all_fingerprints(admin) == before, "a failed single-transaction restore altered the populated database"
    finally:
        admin.dispose()

    truncated = f"{recovery.dump_path}.truncated"
    cut = _run_in_container(role_cluster, ["sh", "-c", f"head -c 200 {recovery.dump_path} > {truncated}"], user=migrator, password=pw)
    assert cut.returncode == 0
    scratch = _create_database_owned_by_migrator(role_cluster)
    try:
        bad = _run_in_container(
            role_cluster,
            ["pg_restore", "-h", "localhost", "-U", migrator, "-d", scratch, "--no-password", "--exit-on-error",
             "--single-transaction", truncated],
            user=migrator, password=pw,
        )
        assert bad.returncode != 0, "a truncated archive must not restore successfully"
        scratch_admin = role_cluster.admin_engine(scratch)
        try:
            assert _public_tables(scratch_admin) == [], "a failed single-transaction restore left objects behind"
        finally:
            scratch_admin.dispose()
    finally:
        _drop_database(role_cluster, scratch)
        _run_in_container(role_cluster, ["rm", "-f", truncated], user=migrator, password=pw)


def test_command_diagnostics_never_contain_generated_secrets(role_cluster, recovery):
    recovery.require_restore_succeeded()
    for result in (recovery.dump, recovery.listing, recovery.restore):
        blob = result.stdout + result.stderr + " ".join(result.argv)
        for secret in role_cluster.secrets_to_scrub():
            assert secret not in blob


# --------------------------------------------------------------------------
# mutation controls: the boundary battery must FAIL when the boundary is wrong
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def mutation_database(role_cluster):
    """A separate scratch database (same provisioning as the real one, no seed data) so injected misconfigurations never touch `source_database`."""
    name = _create_database_owned_by_migrator(role_cluster)
    try:
        provision_database_level_privileges(role_cluster, name)
        migrate_as_migrator(role_cluster, name)
        grant_runtime_read_access(role_cluster, name)
        yield name
    finally:
        _drop_database(role_cluster, name)


def _mutations(database: str) -> dict[str, tuple[str, str, str]]:
    """label -> (apply, revert, diagnostic fragment the battery must report), each applied by the superuser admin exactly as a real misconfiguration would be."""
    r, m = RUNTIME_ROLE, MIGRATOR_ROLE
    return {
        "extra_insert_grant": (f"GRANT INSERT ON TABLE public.teams TO {r}", f"REVOKE INSERT ON TABLE public.teams FROM {r}", "('teams', 'INSERT')"),
        "write_grant_on_alembic_version": (f"GRANT UPDATE ON TABLE public.alembic_version TO {r}", f"REVOKE UPDATE ON TABLE public.alembic_version FROM {r}", "('alembic_version', 'UPDATE')"),
        "extra_readable_table": (f"GRANT SELECT ON TABLE public.source_artifacts TO {r}", f"REVOKE SELECT ON TABLE public.source_artifacts FROM {r}", "source_artifacts"),
        "public_can_read": ("GRANT SELECT ON TABLE public.teams TO PUBLIC", "REVOKE SELECT ON TABLE public.teams FROM PUBLIC", "PUBLIC holds privileges"),
        "sequence_usage": (f"GRANT USAGE ON SEQUENCE public.team_aliases_id_seq TO {r}", f"REVOKE USAGE ON SEQUENCE public.team_aliases_id_seq FROM {r}", "on sequence team_aliases_id_seq"),
        "schema_create": (f"GRANT CREATE ON SCHEMA public TO {r}", f"REVOKE CREATE ON SCHEMA public FROM {r}", "can CREATE in schema public"),
        "database_temporary": (f'GRANT TEMPORARY ON DATABASE "{database}" TO {r}', f'REVOKE TEMPORARY ON DATABASE "{database}" FROM {r}', "TEMPORARY on database"),
        "member_of_migrator": (f"GRANT {m} TO {r}", f"REVOKE {m} FROM {r}", "role membership"),
        "member_of_pg_read_all_data": (f"GRANT pg_read_all_data TO {r}", f"REVOKE pg_read_all_data FROM {r}", "role membership"),
        "createdb_attribute": (f"ALTER ROLE {r} CREATEDB", f"ALTER ROLE {r} NOCREATEDB", "role attributes unexpected"),
        "default_privilege_autogrant": (
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {m} IN SCHEMA public GRANT SELECT ON TABLES TO {r}",
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {m} IN SCHEMA public REVOKE SELECT ON TABLES FROM {r}",
            "default ACL would auto-grant",
        ),
    }


_MUTATION_LABELS = sorted(_mutations("x"))


@pytest.mark.parametrize("label", _MUTATION_LABELS)
def test_boundary_battery_detects_injected_misconfiguration(role_cluster, mutation_database, label):
    apply_sql, revert_sql, expected = _mutations(mutation_database)[label]
    admin = role_cluster.admin_engine(mutation_database)
    try:
        with admin.connect() as conn:
            conn.execute(text(apply_sql))
        try:
            with pytest.raises((AssertionError, pytest.fail.Exception)) as excinfo:
                assert_runtime_boundary(role_cluster, mutation_database)
            assert expected in str(excinfo.value), f"{label}: caught, but not for the expected reason {expected!r}: {str(excinfo.value)[:300]}"
        finally:
            with admin.connect() as conn:
                conn.execute(text(revert_sql))
    finally:
        admin.dispose()


def test_battery_passes_again_after_every_injected_misconfiguration_was_reverted(role_cluster, mutation_database):
    """Proves the controls above clean up after themselves and that the failures were caused by the injected change alone."""
    assert_runtime_boundary(role_cluster, mutation_database)
