"""
Public demo seed: loads the fictional demo teams (from the demo team registry) and synthetic players and per-game
statistics into the demo PostgreSQL database. Idempotent (repository upserts), and it refuses any database other
than the compose demo database.
"""
import os
import random

from src.db.allstats_repositories import PlayerStatSnapshotRepository
from src.db.repositories import ConferenceRepository, DataSourceRepository, PlayerRepository, SeasonRepository, SourceArtifactRepository, TeamRepository
from src.db.session import build_engine, build_session_factory, unit_of_work
from src.identity.player_identity import build_player_identity
from src.identity.stable_id import stable_namespaced_id
from src.identity.team_identity import resolve_team_identity

SEASON = 2026
TEAMS = ["Harbor State", "Pine Ridge", "Ironwood", "Lakeshore Tech", "Summit Valley", "Copper Canyon", "Riverbend", "North Meadow"]
POSITIONS = ["G", "G", "F", "F", "C"]

url = os.environ["NAPSEDGE_DATABASE_URL"]
if os.environ.get("NAPSEDGE_DEMO") != "1" or "@demo-db:5432/napsedge_demo" not in url:
    raise SystemExit("refusing to seed: this script only writes to the compose demo database (NAPSEDGE_DEMO=1, host demo-db)")

factory = build_session_factory(build_engine(url))
rng = random.Random(2026)  # fixed seed: every demo run shows the same synthetic numbers
players = 0
for display_name in TEAMS:
    team = resolve_team_identity(display_name, source="api")
    with unit_of_work(factory) as s:
        conference_id = stable_namespaced_id("conference", team.conference)  # same keying as src.identity.conference_identity
        ConferenceRepository(s).upsert(conference_id=conference_id, canonical_name=team.conference)
        TeamRepository(s).upsert(team_id=team.team_id, display_name=display_name, sr_slug=team.display_name, conference_id=conference_id)
        SeasonRepository(s).upsert(season_id=SEASON, start_year=SEASON - 1, end_year=SEASON, label=f"{SEASON - 1}-{SEASON}")
        DataSourceRepository(s).upsert(source_key="sr_allstats", display_name="SR AllStats (demo)", acquisition_type="live_scraped_html", is_rescrapable=True)
        artifact = SourceArtifactRepository(s).upsert(
            source_key="sr_allstats", source_root_key="teamdump_cbb", relative_path=f"demo/{team.display_name}/Roster_{SEASON}.xlsx",
            original_filename=f"Roster_{SEASON}.xlsx", file_type="xlsx", content_hash=f"demo-{team.team_id}",
            team_id=team.team_id, season_id=SEASON, validation_status="valid",
            point_in_time_classification="historical_but_not_point_in_time")
        for n, position in enumerate(POSITIONS, start=1):
            player = build_player_identity(f"{display_name} Demo Player {n}", team.team_id, SEASON)
            PlayerRepository(s).upsert(player_id=player.player_id, display_name=player.display_name, team_id=team.team_id, season_id=SEASON)
            games = rng.randint(24, 32)
            PlayerStatSnapshotRepository(s).upsert(
                team_id=team.team_id, season_id=SEASON, source_artifact_id=artifact.id, stat_table="PerGame",
                player_key=player.display_name.lower(), source_player_name=player.display_name, validation_status="valid",
                player_id=player.player_id, source_position=position, games_played=games, games_started=games,
                minutes_played=round(rng.uniform(18, 34), 1), pts=round(rng.uniform(5, 19), 1), trb=round(rng.uniform(2, 9), 1),
                ast=round(rng.uniform(0.5, 6), 1), fg_pct=round(rng.uniform(0.40, 0.55), 3), three_p_pct=round(rng.uniform(0.28, 0.41), 3))
            players += 1
print(f"demo seed complete: {len(TEAMS)} teams, {players} players, season {SEASON}")
