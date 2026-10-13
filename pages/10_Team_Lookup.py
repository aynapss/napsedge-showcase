"""
Phase 7, Slice 2B: read-only Team / NET Ranking / Injury lookup page.

No service, session, cache, or filesystem call happens anywhere in this
script except inside the `if submitted:` branch below -- every widget
above it (the operation selector, the query box) only ever reads or
writes `st.session_state`, which Streamlit already does for free.
Changing a widget without pressing Submit triggers a rerun that
re-executes this whole script, but the `if submitted:` guard means that
rerun still performs zero application work.
"""
from __future__ import annotations

import streamlit as st

from src.streamlit_consumer_controller import default_dependencies, run_injury_lookup, run_rankings_lookup, run_team_lookup
from src.streamlit_consumer_renderer import render_consumer_presentation

_MAX_QUERY_LENGTH = 100
_OPERATIONS = ("Team overview", "NET Ranking", "Injury report")

st.set_page_config(page_title="Team Lookup", layout="wide")

st.title("Team / NET Ranking / Injury Lookup")

st.info(
    "Internal, local-use only. This page runs directly against the local "
    "NapsEdge database and cache on this machine -- it is not "
    "authenticated, not public, and not intended for multi-user or "
    "hosted use."
)

if "team_lookup_result" not in st.session_state:
    st.session_state["team_lookup_result"] = None

operation = st.radio("Operation", _OPERATIONS, key="team_lookup_operation")
query = st.text_input("Team name, slug, or alias", key="team_lookup_query")

submit_col, clear_col = st.columns([1, 1])
submitted = submit_col.button("Submit", key="team_lookup_submit")
cleared = clear_col.button("Clear", key="team_lookup_clear")

if cleared:
    st.session_state["team_lookup_result"] = None

if submitted:
    cleaned_query = (query or "").strip()
    if not cleaned_query or len(cleaned_query) > _MAX_QUERY_LENGTH:
        st.session_state["team_lookup_result"] = None
        st.warning("Please provide a non-empty team name (max 100 characters).")
    else:
        deps = default_dependencies()
        if operation == "Team overview":
            presentation = run_team_lookup(cleaned_query, session_factory=deps.session_factory)
        elif operation == "NET Ranking":
            presentation = run_rankings_lookup(
                cleaned_query,
                session_factory=deps.session_factory,
                rankings_config_provider=deps.rankings_config_provider,
            )
        else:
            presentation = run_injury_lookup(cleaned_query, session_factory=deps.session_factory)
        st.session_state["team_lookup_result"] = presentation

if st.session_state["team_lookup_result"] is not None:
    render_consumer_presentation(st.session_state["team_lookup_result"])
