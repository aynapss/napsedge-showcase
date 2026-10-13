from pathlib import Path

import streamlit as st

st.set_page_config(
    page_title="NCAAB Analytics",
    layout="wide",
)

_APP_DIR = Path(__file__).resolve().parent

# (page file, link label). A link -- and its "Working now" bullet below --
# renders only when the page file exists next to this script. A deployment
# image ships only the tracked pages 10-12, and st.page_link raises
# StreamlitPageNotFoundError (rendering a traceback that includes server
# paths) for any page that is absent; with every page present the output is
# identical to the original unconditional list.
_DATA_PAGES = (
    ("pages/01_Team_Explorer.py", "Team Explorer"),
    ("pages/02_Bet_Tracker.py", "Bet Tracker"),
    ("pages/03_Historical_Results.py", "Historical Results"),
    ("pages/10_Team_Lookup.py", "Team / NET Ranking / Injury Lookup"),
    ("pages/11_Player_Raw_Statistics.py", "Player Raw Statistics"),
    ("pages/12_Matchup_Feature_Comparison.py", "Matchup Feature Comparison"),
)

_VISUAL_PAGES = (
    ("pages/04_Player_Cards.py", "Player Cards"),
    ("pages/05_Team_Overview.py", "Team Overview"),
    ("pages/06_Conference_Dashboard.py", "Conference Dashboard"),
    ("pages/07_Matchup_Graphics.py", "Team-v-Team Graphics"),
    ("pages/08_Pipeline_Architecture.py", "Pipeline Architecture"),
    ("pages/09_AI_Research_Control_Room.py", "AI Research Control Room"),
)

_WORKING_NOW = (
    ("pages/01_Team_Explorer.py", "Team data browser"),
    ("pages/02_Bet_Tracker.py", "Bet tracker browser"),
    ("pages/03_Historical_Results.py", "Historical results browser"),
    ("pages/04_Player_Cards.py", "Player card image viewer"),
    ("pages/05_Team_Overview.py", "Team overview graphic viewer"),
    ("pages/06_Conference_Dashboard.py", "Conference dashboard viewer"),
    ("pages/07_Matchup_Graphics.py", "Team-v-team matchup graphic viewer"),
    ("pages/08_Pipeline_Architecture.py", "Pipeline architecture and dependency status page"),
    ("pages/09_AI_Research_Control_Room.py", "AI research control room for matchup pipeline + bot outputs"),
    ("pages/10_Team_Lookup.py", "Team / NET Ranking / Injury lookup (internal, local-use only)"),
    ("pages/11_Player_Raw_Statistics.py", "Player raw statistics lookup (internal, local-use only)"),
    ("pages/12_Matchup_Feature_Comparison.py", "Matchup feature comparison: descriptive, user-supplied values only (internal, local-use only)"),
)


def _present(pages):
    return [(path, label) for path, label in pages if (_APP_DIR / path).is_file()]


st.title("NCAAB Analytics Dashboard")

st.write(
    "This is the Streamlit front end for your NCAA basketball analytics pipeline."
)

data_pages = _present(_DATA_PAGES)
if data_pages:
    st.subheader("Data Pages")
    for path, label in data_pages:
        st.page_link(path, label=label)

visual_pages = _present(_VISUAL_PAGES)
if visual_pages:
    st.subheader("Visual Pages")
    for path, label in visual_pages:
        st.page_link(path, label=label)

st.divider()

st.subheader("Current Build Status")

working_now = [f"- {label}" for _, label in _present(_WORKING_NOW)]
if working_now:
    st.markdown("**Working now:**\n\n" + "\n".join(working_now))

st.markdown(
    """
    **Coming later:**

    - Full notebook-grade player card generation directly inside Streamlit
    - Regenerate team overview graphics directly inside Streamlit
    - Refresh odds and Sports Reference data safely
    """
)
