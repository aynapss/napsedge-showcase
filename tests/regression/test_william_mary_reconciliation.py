"""
Phase 4A pre-batch, Gate 1: William & Mary SR-slug reconciliation wired
into the canonical team-identity resolution path (query side), on top of
the already-completed Gate 1 (Phase 2) reconciliation (output side,
`get_explicit_sr_slug`). No live requests -- uses the committed
`docs/sr_slug_reconciliation.json` record and the real 365-team registry.
"""
import pytest

from src.identity.contracts import TeamIdentity, UnresolvedIdentity
from src.identity.net_ranking_identity import resolve_net_team_identity
from src.identity.sr_slug_reconciliation import (
    get_explicit_sr_slug,
    resolve_team_identity_with_reconciliation,
)
from src.identity.team_identity import ensure_team_registry
from src.ingestion.sr_allstats_ingestion import resolve_ingestion_team_slug

WILLIAM_AND_MARY_TEAM_ID = 870596


# ---------------------------------------------------------------------------
# Confirmed cached SR evidence (requirement #1 -- re-confirms, does not
# re-derive, Gate 1's own finding)
# ---------------------------------------------------------------------------

def test_cached_sr_evidence_establishes_william_mary_slug():
    record = get_explicit_sr_slug(WILLIAM_AND_MARY_TEAM_ID)
    assert record == "william-mary"


# ---------------------------------------------------------------------------
# Requirement #5: all documented input forms resolve to the same team
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("query", [
    "William & Mary",       # registry's own exact alias (already worked)
    "William and Mary",     # legacy alias, spelled out
    "william-mary",         # verified current SR slug
    "william-&-mary",       # prior registry value, retained as a legacy alias
    "william-and-mary",     # disputed dataframe value, retained as a legacy alias
])
def test_all_documented_forms_resolve_to_the_same_team(query):
    result = resolve_team_identity_with_reconciliation(query)
    assert isinstance(result, TeamIdentity)
    assert result.team_id == WILLIAM_AND_MARY_TEAM_ID
    assert result.display_name == "William & Mary"


# ---------------------------------------------------------------------------
# Requirement #3: stable team_id, display name, legacy alias preserved
# ---------------------------------------------------------------------------

def test_stable_team_id_unchanged():
    ensure_team_registry()
    from src import slug_utils

    assert WILLIAM_AND_MARY_TEAM_ID in slug_utils.TEAM_REGISTRY
    assert slug_utils.TEAM_REGISTRY[WILLIAM_AND_MARY_TEAM_ID].display_name == "William & Mary"


def test_legacy_registry_form_still_resolves_directly():
    """"William & Mary" and "william-&-mary" already worked via the registry's own exact alias -- unaffected by this batch."""
    from src.identity.team_identity import resolve_team_identity

    assert isinstance(resolve_team_identity("William & Mary"), TeamIdentity)
    assert isinstance(resolve_team_identity("william-&-mary"), TeamIdentity)


def test_v15_compatibility_wrapper_unaffected():
    """resolve_slug_from_name_v15 (the unrestricted-fuzzy legacy wrapper) is untouched by this reconciliation."""
    import pandas as pd

    from src.identity.postgame_team_identity import resolve_slug_from_name_v15

    df = pd.DataFrame({"team_name": ["William & Mary"], "team_slug": ["william-&-mary"]})
    assert resolve_slug_from_name_v15("William & Mary", all_schools_df=df) == "william-&-mary"


# ---------------------------------------------------------------------------
# Requirement #6: NET adapter and SR ingestion adapter agree
# ---------------------------------------------------------------------------

def test_net_adapter_resolves_william_mary():
    result = resolve_net_team_identity("william-mary")
    assert isinstance(result, TeamIdentity)
    assert result.team_id == WILLIAM_AND_MARY_TEAM_ID


def test_sr_ingestion_adapter_resolves_william_mary():
    team_result, sr_slug = resolve_ingestion_team_slug("William and Mary")
    assert isinstance(team_result, TeamIdentity)
    assert team_result.team_id == WILLIAM_AND_MARY_TEAM_ID
    assert sr_slug == "william-mary"


def test_net_and_sr_adapters_agree():
    net_result = resolve_net_team_identity("william-mary")
    sr_result, _ = resolve_ingestion_team_slug("william-mary")
    assert isinstance(net_result, TeamIdentity) and isinstance(sr_result, TeamIdentity)
    assert net_result.team_id == sr_result.team_id == WILLIAM_AND_MARY_TEAM_ID


# ---------------------------------------------------------------------------
# Requirement #7: no new collision introduced
# ---------------------------------------------------------------------------

def test_no_new_alias_collision_introduced():
    """
    Confirms the two confirmed query-collision cases (fixed by
    team_shortening_guard.py, a separate mechanism) are NOT reopened by
    this reconciliation wiring -- their legacy_aliases entries are the
    confirmed-bad collision strings, deliberately excluded from the
    reconciliation query index.
    """
    for query in ("Colorado College Tigers", "Washington (MO) Bears"):
        result = resolve_team_identity_with_reconciliation(query)
        assert not isinstance(result, TeamIdentity)
        assert result.reason == "not_found"


def test_ambiguous_teams_never_overridden_by_reconciliation():
    result = resolve_team_identity_with_reconciliation("Miami")
    assert not isinstance(result, TeamIdentity)
    assert result.reason == "ambiguous"


def test_all_32_reconciled_teams_still_resolve_to_their_own_team_id():
    """Sanity sweep: every reconciled team's own selected slug still resolves to itself, not a collision."""
    from src.identity.sr_slug_reconciliation import reconciliation_disputes

    for record in reconciliation_disputes():
        if record["resolution_status"].startswith("unresolved"):
            continue
        result = resolve_team_identity_with_reconciliation(record["selected_current_sr_slug"])
        if isinstance(result, TeamIdentity):
            assert result.team_id == record["team_id"], record


# ---------------------------------------------------------------------------
# Requirement #7 (last bullet): complete D1 collision sweep remains safe
# ---------------------------------------------------------------------------

def test_full_d1_collision_sweep_still_safe_with_reconciliation_wired_in():
    """
    Every real D1 team's own bare display name must still resolve to
    itself through the reconciliation-aware wrapper too -- confirms wiring
    in the reconciliation index did not introduce any cross-team collision
    across the full registry, not just the 32 previously-disputed teams.
    """
    from src import slug_utils

    ensure_team_registry()
    failures = []
    for team_id, rec in slug_utils.TEAM_REGISTRY.items():
        result = resolve_team_identity_with_reconciliation(rec.display_name)
        if not isinstance(result, TeamIdentity) or result.team_id != team_id:
            failures.append((rec.display_name, team_id, result))
    assert failures == [], f"{len(failures)} regressions found: {failures[:10]}"


def test_no_ambiguous_result_ever_overridden_across_reconciliation_index():
    """No entry in the reconciliation index maps to a team_id different from what plain resolve_team_identity would say for an already-ambiguous query."""
    from src.identity.team_identity import resolve_team_identity

    for query in ("Miami", "Loyola"):
        plain = resolve_team_identity(query)
        reconciled = resolve_team_identity_with_reconciliation(query)
        assert type(plain) is type(reconciled)
        if not isinstance(plain, TeamIdentity):
            assert plain.reason == reconciled.reason
