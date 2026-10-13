import os
from pathlib import Path


def _resolve_root_dir() -> Path:
    """
    Repo root, overridable via the ``NAPSEDGE_ROOT_DIR`` environment variable.

    Every path below is derived from this one directory, so tests can
    redirect the *entire* pipeline's file I/O (caches, PlayerCards output,
    bet tracker, etc.) at a throwaway ``tmp_path`` by setting this single
    env var, without touching production data or any individual constant.
    Unset (the default), behavior is unchanged from before this override
    existed.
    """
    override = os.environ.get("NAPSEDGE_ROOT_DIR")
    if override:
        return Path(override).resolve()
    return Path(__file__).resolve().parents[1]


ROOT_DIR = _resolve_root_dir()
NOTEBOOK_PATH = ROOT_DIR / "cbb_teamScrape_v15_TEST.ipynb"

TEAMDUMP_DIR = ROOT_DIR / "TeamDump"
CBB_DIR = TEAMDUMP_DIR / "CBB"
ESPN_DIR = TEAMDUMP_DIR / "ESPN"

BET_TRACKER_DIR = ROOT_DIR / "bet_tracker"
LIVE_SCOREBOARD_DIR = ROOT_DIR / "Live Scoreboard"
IMAGES_DIR = ROOT_DIR / "Images"

LINE_SNAPSHOTS_PATH = TEAMDUMP_DIR / "line_snapshots.json"
POSTMORTEM_PATH = TEAMDUMP_DIR / "postmortem_log.jsonl"

BET_LOG_PATH = BET_TRACKER_DIR / "bet_log.parquet"
PENDING_BETS_PATH = BET_TRACKER_DIR / "pending.json"

PLAYER_CARDS_DIR = ROOT_DIR / "PlayerCards"
PLAYER_RADARS_DIR = ROOT_DIR / "NCAA-Player-Radars"
CONFERENCE_RATINGS_DIR = ROOT_DIR / "Conference_Ratings"
GAMEDAY_COMPS_DIR = ROOT_DIR / "Gameday Comps"
TEAM_LOGOS_DIR = IMAGES_DIR / "Team_Logos_NCAAB_D1_Canonical"
CONFERENCE_LOGOS_DIR = IMAGES_DIR / "Conference Logos"
