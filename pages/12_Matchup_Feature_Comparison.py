"""
Phase 7, Slice 3C: descriptive, explicit-feature Matchup comparison page.

Unlike every other page in this app, this page makes no database, file,
cache, or network call at all, at any point --
`src.services.matchup.compare_team_matchup_features` has no such
dependency of its own to call. No service call happens anywhere in this
script except inside the `if submitted:` branch below -- every widget
above it (the two team-label inputs, the fourteen optional feature-value
inputs) only ever reads or writes `st.session_state`. Every value this
page ever displays for either team originates from what the user
themselves typed into this page's own form.

Each feature value is a plain `st.text_input`, not `st.number_input`:
confirmed by direct testing that `st.number_input` silently converts a
programmatically-supplied `Infinity`/`-Infinity` back to `None` before
this page's own script ever runs (while passing a `NaN` through
unchanged) -- an inconsistent, unreliable foundation for the "reject
NaN/Infinity, never silently convert to None" requirement this page
must satisfy. A `text_input` plus the small private parser below
(`_parse_feature_value`) gives this page full, explicit, symmetric
control over all three outcomes (blank -> `None`, a valid finite decimal
-> that `float`, anything else -> rejected with a visible warning) at
this page's own UI boundary -- mirroring `TeamFeatureValuesRequest`'s
already-approved `allow_inf_nan=False` policy, not inventing a new one.
"""
from __future__ import annotations

import math

import streamlit as st

from src.features.matchup_features import MATCHUP_FEATURE_LABELS
from src.streamlit_consumer_controller import run_matchup_feature_comparison
from src.streamlit_consumer_renderer import render_consumer_presentation

#: Matches the existing HTTP request bound
#: (`Field(..., min_length=1, max_length=100)` in
#: `src/api/schemas.py::MatchupFeatureComparisonRequest`) -- reusing an
#: already-established boundary rather than inventing a new one.
_MAX_TEAM_LABEL_LENGTH = 100

_RESULT_KEY = "matchup_comparison_result"
_TEAM_A_LABEL_KEY = "matchup_team_a_label"
_TEAM_B_LABEL_KEY = "matchup_team_b_label"


class _InvalidFeatureValue(ValueError):
    """Raised by `_parse_feature_value` for anything that is neither blank nor a finite decimal number."""


def _parse_feature_value(raw: str) -> float | None:
    """Blank (after stripping) -> `None`. A valid finite decimal -> that `float`, at full typed precision -- no rounding. Non-numeric text, or a numeric-looking but non-finite value (`nan`/`inf`/`-inf`/`infinity`, all of which Python's own `float()` otherwise parses without error) -> `_InvalidFeatureValue`, never silently coerced to `None` or `0.0`."""
    cleaned = (raw or "").strip()
    if not cleaned:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        raise _InvalidFeatureValue(cleaned) from None
    if not math.isfinite(value):
        raise _InvalidFeatureValue(cleaned)
    return value


def _team_a_key(feature_key: str) -> str:
    return f"matchup_team_a_{feature_key}"


def _team_b_key(feature_key: str) -> str:
    return f"matchup_team_b_{feature_key}"


def _clear_form() -> None:
    """
    An `on_click` callback, not inline script logic -- Streamlit runs a
    button's `on_click` callback *before* the rerun that re-instantiates
    every widget below, so resetting each widget's own `session_state`
    key here (rather than after the widgets are already created) is the
    only way to actually clear their displayed values, not just the
    stored presentation.
    """
    st.session_state[_RESULT_KEY] = None
    st.session_state[_TEAM_A_LABEL_KEY] = ""
    st.session_state[_TEAM_B_LABEL_KEY] = ""
    for feature_key in MATCHUP_FEATURE_LABELS:
        st.session_state[_team_a_key(feature_key)] = ""
        st.session_state[_team_b_key(feature_key)] = ""


st.set_page_config(page_title="Matchup Feature Comparison", layout="wide")

st.title("Matchup Feature Comparison")

st.info(
    "Descriptive feature comparison. Values are supplied by the user. "
    "No live team data is retrieved. Results are not a prediction. "
    "Results are not betting advice."
)

if _RESULT_KEY not in st.session_state:
    st.session_state[_RESULT_KEY] = None

team_a_label = st.text_input("Team A label", key=_TEAM_A_LABEL_KEY)
team_b_label = st.text_input("Team B label", key=_TEAM_B_LABEL_KEY)

team_a_raw: dict[str, str] = {}
team_b_raw: dict[str, str] = {}

team_a_col, team_b_col = st.columns(2)
with team_a_col:
    st.subheader("Team A values")
    for feature_key, feature_label in MATCHUP_FEATURE_LABELS.items():
        team_a_raw[feature_key] = st.text_input(feature_label, key=_team_a_key(feature_key))
with team_b_col:
    st.subheader("Team B values")
    for feature_key, feature_label in MATCHUP_FEATURE_LABELS.items():
        team_b_raw[feature_key] = st.text_input(feature_label, key=_team_b_key(feature_key))

submit_col, clear_col = st.columns([1, 1])
submitted = submit_col.button("Submit", key="matchup_submit")
clear_col.button("Clear", key="matchup_clear", on_click=_clear_form)

if submitted:
    cleaned_team_a = (team_a_label or "").strip()
    cleaned_team_b = (team_b_label or "").strip()

    team_a_values: dict[str, float | None] = {}
    team_b_values: dict[str, float | None] = {}
    has_invalid_value = False
    for feature_key in MATCHUP_FEATURE_LABELS:
        for raw_values, parsed_values in ((team_a_raw, team_a_values), (team_b_raw, team_b_values)):
            try:
                parsed_values[feature_key] = _parse_feature_value(raw_values[feature_key])
            except _InvalidFeatureValue:
                has_invalid_value = True

    if not cleaned_team_a or len(cleaned_team_a) > _MAX_TEAM_LABEL_LENGTH:
        st.session_state[_RESULT_KEY] = None
        st.warning("Please provide a non-empty Team A label (max 100 characters).")
    elif not cleaned_team_b or len(cleaned_team_b) > _MAX_TEAM_LABEL_LENGTH:
        st.session_state[_RESULT_KEY] = None
        st.warning("Please provide a non-empty Team B label (max 100 characters).")
    elif has_invalid_value:
        st.session_state[_RESULT_KEY] = None
        st.warning("Each feature value must be left blank or be a finite number (no text, no NaN/Infinity).")
    else:
        presentation = run_matchup_feature_comparison(cleaned_team_a, cleaned_team_b, team_a_values, team_b_values)
        st.session_state[_RESULT_KEY] = presentation

if st.session_state[_RESULT_KEY] is not None:
    render_consumer_presentation(st.session_state[_RESULT_KEY])
