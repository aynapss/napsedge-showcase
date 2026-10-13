import json
import pandas as pd
import streamlit as st

from src.paths import POSTMORTEM_PATH, LINE_SNAPSHOTS_PATH


@st.cache_data
def load_postmortems() -> pd.DataFrame:
    """
    Load postmortem results from TeamDump/postmortem_log.jsonl.
    """
    if not POSTMORTEM_PATH.exists():
        return pd.DataFrame()

    rows = []

    with open(POSTMORTEM_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()

            if not line:
                continue

            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    return pd.DataFrame(rows)


@st.cache_data
def load_line_snapshots() -> pd.DataFrame:
    """
    Load line snapshots from TeamDump/line_snapshots.json.
    """
    if not LINE_SNAPSHOTS_PATH.exists():
        return pd.DataFrame()

    with open(LINE_SNAPSHOTS_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        return pd.DataFrame(data)

    if isinstance(data, dict):
        rows = []

        for key, value in data.items():
            if isinstance(value, dict):
                row = {"snapshot_key": key}
                row.update(value)
                rows.append(row)
            else:
                rows.append({"snapshot_key": key, "value": value})

        return pd.DataFrame(rows)

    return pd.DataFrame()
