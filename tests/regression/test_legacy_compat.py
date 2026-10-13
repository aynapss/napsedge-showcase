"""
Locks in the documented normalize_team_name divergence between:
  - src.slug_utils.normalize_team_name          (matches notebook cell 10, STALE)
  - src.identity.legacy_compat.normalize_team_name_v15_active  (matches cell 43, ACTIVE)
  - src.identity.team_identity.resolve_team_identity's internal apostrophe folding (NEW, canonical-layer-only)

See docs/migration_state.md "Phase 2A -- apostrophe decision" for the full
writeup. These tests exist so a future edit can't silently collapse the
three behaviors into one without a test failing first.
"""
from src.identity.legacy_compat import normalize_team_name_v15_active
from src.slug_utils import normalize_team_name


def test_legacy_v15_active_does_not_strip_curly_apostrophe():
    # This is the notebook's OWN currently-active behavior (cell 43) --
    # not a bug we're introducing, just preserving it verbatim.
    assert normalize_team_name_v15_active("Saint Joseph’s") == "saint joseph’s"


def test_legacy_v15_active_does_strip_straight_apostrophe():
    assert normalize_team_name_v15_active("Saint Joseph's") == "saint josephs"


def test_slug_utils_normalize_team_name_unchanged_still_strips_curly():
    # src.slug_utils.normalize_team_name was NOT modified by the apostrophe
    # decision -- it still matches its own (stale, cell-10) notebook source.
    assert normalize_team_name("Saint Joseph’s") == "saint josephs"
    assert normalize_team_name("Saint Joseph's") == "saint josephs"


def test_legacy_and_slug_utils_versions_genuinely_diverge_on_curly_input():
    # Documents *why* a compatibility wrapper was needed at all: these two
    # pre-existing functions disagree with each other on curly apostrophe
    # input, and neither was changed to match the other.
    curly = "Saint Joseph’s"
    assert normalize_team_name_v15_active(curly) != normalize_team_name(curly)
