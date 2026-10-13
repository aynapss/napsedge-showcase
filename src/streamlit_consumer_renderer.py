"""
Phase 7, Slices 2B/3A/3C: pure Streamlit renderer for
`src.consumers.contracts.ConsumerPresentation`.

This renderer is intentionally generic -- it knows nothing about any
specific domain (Team/Rankings/Injury/Player/Matchup), only the shared
`ConsumerPresentation` shape. Player raw-statistics (Slice 3A) required
no change here at all: player-level metadata is already presented as
top-level `fields` (rendered as labeled text, separate from any table),
while each `PlayerStatRecord` row (including `validation_status`, always
plainly visible as an ordinary column) is already presented as one
`PresentationRow` and rendered via the existing `st.dataframe` path --
the same mechanism Rankings-conflict and Injury rows already use.

The descriptive Matchup feature comparison (Slice 3C) likewise required
no change here at all: the two team labels are already presented as
top-level `fields`, and the fixed 7-feature comparison table (feature
label, each side's own value, and the signed difference -- confirmed by
direct test to render correctly, including `None`-as-"(unavailable)" and
a genuine `0.0` never hidden) is already exactly one `PresentationRow`
per feature, rendered via the same unmodified `st.dataframe` path every
other domain's tabular output already uses.

This module knows Streamlit's own presentation primitives -- it must
never know a service contract. It imports only `streamlit` and
`src.consumers.contracts` (a plain, framework-independent dataclass
module); it performs no network access, no database/filesystem access,
and calls no Phase 6 service.

Field and row *values* -- the only content that can ultimately trace
back to an externally-sourced cache (Injury entries scraped from
Covers/ESPN) -- are rendered with `st.text`/`st.dataframe`, which never
interpret Markdown or HTML, rather than `st.write`/`st.markdown`. Title,
message, warning, and note strings are always static, app-authored text
from the Phase 7 Slice 1 adapters (never derived from raw external
content), so rendering them with Streamlit's normal (Markdown-capable)
calls carries no injection risk; `unsafe_allow_html` is never passed
anywhere in this module.
"""
from __future__ import annotations

import streamlit as st

from src.consumers.contracts import ConsumerPresentation

_UNAVAILABLE_PLACEHOLDER = "(unavailable)"

_STATUS_NOTICE = {
    "ok": st.success,
    "not_found": st.warning,
    "conflict": st.warning,
    "unavailable": st.error,
    "error": st.error,
}


def _field_value(value: str | None) -> str:
    return _UNAVAILABLE_PLACEHOLDER if value is None else value


def render_consumer_presentation(presentation: ConsumerPresentation) -> None:
    """
    Renders one `ConsumerPresentation` using native Streamlit calls, in
    the exact field/row order the presentation already carries -- no
    reordering, no filtering, no silent truncation. Deterministic:
    identical input always produces the same sequence of Streamlit
    calls.
    """
    st.subheader(presentation.title)

    notice = _STATUS_NOTICE.get(presentation.status, st.info)
    if presentation.message is not None:
        notice(presentation.message)

    if presentation.summary is not None:
        st.text(presentation.summary)

    for field in presentation.fields:
        st.text(f"{field.label}: {_field_value(field.value)}")

    if presentation.rows:
        table_rows = [
            {field.label: _field_value(field.value) for field in row} for row in presentation.rows
        ]
        st.dataframe(table_rows, hide_index=True, use_container_width=True)

    for warning in presentation.warnings:
        st.warning(warning)

    for note in presentation.notes:
        st.caption(note)
