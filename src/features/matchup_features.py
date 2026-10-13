"""
Public showcase stand-in. The real module (matchup feature engineering and the
projection model) is proprietary and is not included in this repository. The
display labels are kept so the API, Streamlit and consumer layers import; every
computation raises `NotImplementedError`.
"""
from __future__ import annotations

from typing import Any

MATCHUP_FEATURE_LABELS: dict[str, str] = {
    "avg_margin": "season scoring margin",
    "recent_avg_margin": "recent scoring margin",
    "avg_net_rtg": "season net rating",
    "recent_avg_net_rtg": "recent net rating",
    "player_value_index": "player value index",
    "off_impact": "offensive impact",
    "def_impact": "defensive impact",
}


def _omitted(*_args: Any, **_kwargs: Any) -> Any:
    raise NotImplementedError("proprietary component not included in the public showcase")


build_matchup_feature_comparison = _omitted
build_matchup_features = _omitted
