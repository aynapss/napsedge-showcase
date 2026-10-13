"""
Phase 7, Slice 3A: read-only Player raw-statistics page.

No service, session, cache, or filesystem call happens anywhere in this
script except inside the `if submitted:` branch below -- every widget
above it (the player-ID box, the optional season/stat-table filters)
only ever reads or writes `st.session_state`. Changing a widget without
pressing Submit triggers a rerun that re-executes this whole script, but
the `if submitted:` guard means that rerun still performs zero
application work.
"""
from __future__ import annotations

import streamlit as st

from src.db.enums import ALLSTATS_STAT_TABLES
from src.streamlit_consumer_controller import default_dependencies, run_player_raw_statistics_lookup
from src.streamlit_consumer_renderer import render_consumer_presentation

#: Matches the existing HTTP path-parameter bound
#: (`Path(..., min_length=1, max_length=200)` in `src/api/routes/players.py`)
#: -- reusing an already-established boundary rather than inventing a new one.
_MAX_PLAYER_ID_LENGTH = 200
_STAT_TABLE_UNFILTERED = "(all)"
_STAT_TABLE_OPTIONS = (_STAT_TABLE_UNFILTERED,) + ALLSTATS_STAT_TABLES

st.set_page_config(page_title="Player Raw Statistics", layout="wide")

st.title("Player Raw Statistics")

st.info(
    "Internal, local-use only. This page shows persisted, raw AllStats-"
    "sourced statistics exactly as stored in the local NapsEdge database "
    "-- it is not a computed player rating, OVR score, recommendation, "
    "or live feed, and it is not authenticated, public, or intended for "
    "multi-user or hosted use."
)

if "player_stats_result" not in st.session_state:
    st.session_state["player_stats_result"] = None

player_id = st.text_input("Canonical player ID", key="player_stats_player_id")
season_id_input = st.text_input("Season ID (optional)", key="player_stats_season_id")
stat_table = st.selectbox("Stat table (optional)", _STAT_TABLE_OPTIONS, key="player_stats_stat_table")

submit_col, clear_col = st.columns([1, 1])
submitted = submit_col.button("Submit", key="player_stats_submit")
cleared = clear_col.button("Clear", key="player_stats_clear")

if cleared:
    st.session_state["player_stats_result"] = None

if submitted:
    cleaned_player_id = (player_id or "").strip()
    cleaned_season_id_input = (season_id_input or "").strip()

    if not cleaned_player_id or len(cleaned_player_id) > _MAX_PLAYER_ID_LENGTH:
        st.session_state["player_stats_result"] = None
        st.warning("Please provide a non-empty player ID (max 200 characters).")
    elif cleaned_season_id_input and not cleaned_season_id_input.isdigit():
        st.session_state["player_stats_result"] = None
        st.warning("Season ID must be a whole number, or left blank.")
    else:
        season_id = int(cleaned_season_id_input) if cleaned_season_id_input else None
        selected_stat_table = None if stat_table == _STAT_TABLE_UNFILTERED else stat_table
        deps = default_dependencies()
        presentation = run_player_raw_statistics_lookup(
            cleaned_player_id,
            session_factory=deps.session_factory,
            season_id=season_id,
            stat_table=selected_stat_table,
        )
        st.session_state["player_stats_result"] = presentation

if st.session_state["player_stats_result"] is not None:
    render_consumer_presentation(st.session_state["player_stats_result"])
