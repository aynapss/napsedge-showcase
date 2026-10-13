"""
Phase 2, Gate 1 tests: SR-slug reconciliation. No live requests -- reads
the committed `docs/sr_slug_reconciliation.json` record and exercises the
accessor functions against it.
"""
import json
from pathlib import Path

import pytest

from src.identity.contracts import TeamIdentity, UnresolvedIdentity
from src.identity.sr_slug_reconciliation import (
    _fallback_sr_slug_guess_v15,
    get_explicit_sr_slug,
    get_reconciliation_record,
    list_unresolved_reconciliations,
    reconciliation_disputes,
)
from src.identity.postgame_team_identity import resolve_postgame_sr_slug

RECONCILIATION_PATH = Path("docs/sr_slug_reconciliation.json")

REQUIRED_FIELDS = {
    "team_id", "canonical_display_name", "registry_slug", "dataframe_slug",
    "observed_sr_hyperlink_or_page_url", "legacy_aliases",
    "evidence_classification", "selected_current_sr_slug", "resolution_status",
}


# ---------------------------------------------------------------------------
# Record structure and completeness
# ---------------------------------------------------------------------------

def test_reconciliation_file_exists_and_is_valid_json():
    assert RECONCILIATION_PATH.exists()
    with open(RECONCILIATION_PATH, encoding="utf-8") as f:
        data = json.load(f)
    assert "disputes" in data


def test_all_32_disputes_present():
    disputes = reconciliation_disputes()
    assert len(disputes) == 32


def test_every_dispute_has_required_fields_and_is_evidence_classified():
    for rec in reconciliation_disputes():
        assert REQUIRED_FIELDS <= set(rec.keys())
        assert rec["evidence_classification"]  # non-empty
        assert rec["resolution_status"]  # non-empty


def test_no_team_id_duplicated_across_disputes():
    ids = [r["team_id"] for r in reconciliation_disputes()]
    assert len(ids) == len(set(ids))


def test_legacy_aliases_preserve_the_disputed_dataframe_slug():
    for rec in reconciliation_disputes():
        assert rec["dataframe_slug"] in rec["legacy_aliases"]


# ---------------------------------------------------------------------------
# Root-cause proof: the fallback guess reproduces every non-flagged dispute
# ---------------------------------------------------------------------------

def test_fallback_guess_v15_reproduces_confirmed_generated_disputes():
    """
    Locks in the local-evidence proof: every dispute classified as a
    confirmed generated/non-authoritative artifact is byte-reproducible by
    re-running the notebook's own (verbatim-ported) guess function against
    its query name -- no live request required to reach this conclusion.
    """
    for rec in reconciliation_disputes():
        if rec["evidence_classification"] == "generated_non_authoritative_confirmed_via_fallback_guess_reproduction":
            guess = _fallback_sr_slug_guess_v15(rec["query_name_used"] if "query_name_used" in rec else rec["canonical_display_name"])
            assert guess == rec["dataframe_slug"], rec


def test_fallback_guess_v15_known_examples():
    assert _fallback_sr_slug_guess_v15("Texas A&M") == "texas-aandm"
    assert _fallback_sr_slug_guess_v15("UC Davis") == "uc-davis"
    assert _fallback_sr_slug_guess_v15("William & Mary") == "william-and-mary"


# ---------------------------------------------------------------------------
# get_reconciliation_record / get_explicit_sr_slug
# ---------------------------------------------------------------------------

def test_get_reconciliation_record_known_team():
    # Texas A&M
    rec = get_reconciliation_record(217951)
    assert rec is not None
    assert rec["canonical_display_name"] == "Texas A&M"


def test_get_reconciliation_record_unknown_team_returns_none():
    assert get_reconciliation_record(999999999) is None


def test_get_explicit_sr_slug_returns_registry_value_for_disputed_team():
    assert get_explicit_sr_slug(217951) == "texas-am"  # Texas A&M
    assert get_explicit_sr_slug(871708) == "california-davis"  # UC Davis
    assert get_explicit_sr_slug(937456) == "texas-el-paso"  # UTEP


def test_get_explicit_sr_slug_falls_back_for_undisputed_team():
    assert get_explicit_sr_slug(905884, fallback_slug="st-bonaventure") == "st-bonaventure"


def test_get_explicit_sr_slug_falls_back_when_no_fallback_and_no_record():
    assert get_explicit_sr_slug(999999999) is None


def test_get_explicit_sr_slug_deterministic_repeated():
    values = {get_explicit_sr_slug(217951) for _ in range(3)}
    assert values == {"texas-am"}


# ---------------------------------------------------------------------------
# Correction 2 (Phase 3 pre-batch): William & Mary SR slug, verified via a
# real locally-cached SR page (no live scraping)
# ---------------------------------------------------------------------------

def test_william_and_mary_slug_verified_via_local_cache():
    rec = get_reconciliation_record(870596)
    assert rec["resolution_status"] == "resolved_verified_via_cached_sr_page"
    assert rec["selected_current_sr_slug"] == "william-mary"


def test_william_and_mary_legacy_aliases_retained():
    rec = get_reconciliation_record(870596)
    assert "william-&-mary" in rec["legacy_aliases"]  # prior registry value
    assert "william-and-mary" in rec["legacy_aliases"]  # disputed dataframe value


def test_get_explicit_sr_slug_returns_verified_william_and_mary_value():
    assert get_explicit_sr_slug(870596) == "william-mary"


def test_william_and_mary_team_id_and_display_name_unchanged():
    from src import slug_utils
    from src.identity.team_identity import ensure_team_registry

    ensure_team_registry()
    rec = slug_utils.TEAM_REGISTRY[870596]
    assert rec.display_name == "William & Mary"


def test_query_collision_disputes_do_not_block_their_own_teams_slug():
    """
    Colorado and Washington's own team_ids are unaffected by the
    query-string collision finding for a DIFFERENT program's name --
    get_explicit_sr_slug for the real team_id must still resolve cleanly.

    UPDATED (Phase 3 pre-batch identity correction 1): the underlying
    query-collision defect itself is now fixed by the meaningful-token
    shortening guard (src/identity/team_shortening_guard.py) -- "Colorado
    College Tigers"/"Washington (MO) Bears" no longer resolve to these
    team_ids at all, so `canonical_query_collision_warning` is False here
    (resolved, not merely flagged). Either way, these two real team_ids'
    own SR slugs were never affected.
    """
    colorado_rec = get_reconciliation_record(952264)
    washington_rec = get_reconciliation_record(217860)
    assert colorado_rec["resolution_status"] == "resolved_query_collision_fixed"
    assert washington_rec["resolution_status"] == "resolved_query_collision_fixed"
    assert get_explicit_sr_slug(952264) == "colorado"
    assert get_explicit_sr_slug(217860) == "washington"


def test_no_dispute_is_left_genuinely_unresolved():
    """
    Gate 1's revalidation condition requires every dispute be
    evidence-classified with no team_id change -- confirmed here that the
    unresolved-status list is empty (the two collision cases are flagged,
    not left blocking their own team_id).
    """
    assert list_unresolved_reconciliations() == []


# ---------------------------------------------------------------------------
# resolve_postgame_sr_slug: canonical postgame path retrieves the EXPLICIT
# SR slug, distinct from canonical/pipeline slugs
# ---------------------------------------------------------------------------

def test_resolve_postgame_sr_slug_uses_reconciled_value_for_disputed_team():
    assert resolve_postgame_sr_slug("Texas A&M") == "texas-am"
    assert resolve_postgame_sr_slug("UC Davis") == "california-davis"


def test_resolve_postgame_sr_slug_for_undisputed_team():
    assert resolve_postgame_sr_slug("St. Bonaventure") == "st-bonaventure"


def test_resolve_postgame_sr_slug_unknown_team_is_not_found():
    result = resolve_postgame_sr_slug("Definitely Not A Real School Xyzzy123")
    assert isinstance(result, UnresolvedIdentity)
    assert result.reason == "not_found"


def test_resolve_postgame_sr_slug_ambiguous_team_is_ambiguous():
    result = resolve_postgame_sr_slug("Miami")
    assert isinstance(result, UnresolvedIdentity)
    assert result.reason == "ambiguous"


def test_resolve_postgame_sr_slug_deterministic():
    values = {resolve_postgame_sr_slug("Texas A&M") for _ in range(3)}
    assert values == {"texas-am"}


# ---------------------------------------------------------------------------
# No fuzzy matching in this module's canonical path
# ---------------------------------------------------------------------------

def test_sr_slug_reconciliation_module_uses_no_fuzzy_matching():
    import ast
    import inspect

    import src.identity.sr_slug_reconciliation as mod

    tree = ast.parse(inspect.getsource(mod))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    assert "difflib" not in imported
    assert not any("ovr_model" in name for name in imported)


def test_reconciliation_module_does_not_mutate_team_registry():
    from src import slug_utils

    before_size = len(slug_utils.TEAM_REGISTRY)
    get_explicit_sr_slug(217951)
    resolve_postgame_sr_slug("Texas A&M")
    assert len(slug_utils.TEAM_REGISTRY) == before_size
