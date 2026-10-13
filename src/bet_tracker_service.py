import json
import pandas as pd
import streamlit as st

from src.paths import BET_LOG_PATH, PENDING_BETS_PATH


@st.cache_data
def load_bet_log() -> pd.DataFrame:
    """
    Load completed or logged bets from bet_log.parquet.
    """
    if not BET_LOG_PATH.exists():
        return pd.DataFrame()

    return pd.read_parquet(BET_LOG_PATH)


@st.cache_data
def load_pending_bets() -> pd.DataFrame:
    """
    Load pending bets from pending.json.
    """
    if not PENDING_BETS_PATH.exists():
        return pd.DataFrame()

    with open(PENDING_BETS_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        return pd.DataFrame(data)

    if isinstance(data, dict):
        return pd.DataFrame([data])

    return pd.DataFrame()
