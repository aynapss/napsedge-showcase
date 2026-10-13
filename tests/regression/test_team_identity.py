"""
Regression tests for src/identity/team_identity.py (Phase 2A).

resolve_team_identity() is a facade over src.slug_utils's existing registry
(built from the real, authoritative 33-conference/365-team D1 universe
parsed from cbb_teamScrape_v15_TEST.ipynb -- see load_conferences() -- not a
hand-rolled test-only list). No network calls: everything here is local
notebook-JSON parsing plus in-memory dict lookups.
"""
import pytest

from src.identity.team_identity import check_team_alias_collisions, resolve_team_identity


# Each group: known alias variants that must all resolve to the SAME program.
KNOWN_ALIAS_GROUPS = [
    ("St. Bonaventure", ["St. Bonaventure", "St Bonaventure", "St Bonas", "Bonnies"]),
    ("Saint Joseph's", ["Saint Joseph's", "St. Joseph's", "St Joes"]),
    ("Saint Louis", ["Saint Louis", "St. Louis"]),
    ("La Salle", ["La Salle", "Lasalle"]),
    ("George Washington", ["George Washington", "GW"]),
    ("Rhode Island", ["Rhode Island", "URI"]),
]


@pytest.mark.parametrize("canonical_name,variants", KNOWN_ALIAS_GROUPS, ids=[g[0] for g in KNOWN_ALIAS_GROUPS])
def test_known_alias_variants_resolve_to_the_same_program(canonical_name, variants):
    resolved = [resolve_team_identity(v, source="test") for v in variants]
    for variant, result in zip(variants, resolved):
        assert hasattr(result, "team_id"), (
            f"{variant!r} should have resolved but got UnresolvedIdentity"
            f"({getattr(result, 'reason', '?')})"
        )
    team_ids = {r.team_id for r in resolved}
    assert len(team_ids) == 1, (
        f"variants of {canonical_name!r} resolved to different team_ids: "
        f"{[(v, r.team_id) for v, r in zip(variants, resolved)]}"
    )


def test_punctuation_and_apostrophe_variants_agree():
    a = resolve_team_identity("Saint Joseph's")
    b = resolve_team_identity("Saint Josephs")
    assert hasattr(a, "team_id") and hasattr(b, "team_id")
    assert a.team_id == b.team_id


def test_directional_and_state_school_distinction_not_collapsed():
    # George Washington and Washington State are different programs -- a
    # canonicalizer that stripped "state" globally would risk conflating
    # abbreviation-derived queries here. GW must resolve to George
    # Washington specifically, not to a "Washington"-anything program.
    gw = resolve_team_identity("GW")
    assert hasattr(gw, "team_id")
    assert gw.display_name == "George Washington"


def test_source_specific_slugs_present_for_a_known_team():
    sbu = resolve_team_identity("St. Bonaventure")
    assert hasattr(sbu, "team_id")
    assert "sports_reference" in sbu.sources
    assert sbu.sources["sports_reference"].raw_value  # non-empty SR slug


def test_unknown_team_returns_structured_unresolved_result():
    result = resolve_team_identity("Zzzzznotarealteam99")
    assert not hasattr(result, "team_id")
    assert result.reason == "not_found"


@pytest.mark.parametrize("query", ["Miami", "Loyola"])
def test_ambiguous_bare_query_is_not_silently_resolved(query):
    """
    "Miami" and "Loyola" each collapse to the same naive slug for two
    different real D1 programs (Miami (OH)/Miami (FL), Loyola (MD)/Loyola
    Chicago). The old src.slug_utils.resolve_team() would silently pick
    one via its alias index (whichever was registered last) -- the whole
    point of this new service is that it must not do that.
    """
    result = resolve_team_identity(query, source="test")
    assert not hasattr(result, "team_id"), (
        f"{query!r} should be flagged ambiguous, not silently resolved to "
        f"{getattr(result, 'display_name', None)!r}"
    )
    assert result.reason == "ambiguous"
    assert len(result.candidates) >= 2
    display_names = {c.display_name for c in result.candidates}
    assert len(display_names) >= 2  # genuinely distinct programs, not dupes


@pytest.mark.parametrize("query", ["Miami (OH)", "Miami (FL)"])
def test_disambiguated_full_name_still_resolves_cleanly(query):
    # The ambiguity check must not become so aggressive that it flags a
    # query that already includes the disambiguating detail.
    result = resolve_team_identity(query, source="test")
    assert hasattr(result, "team_id"), f"{query!r} should resolve, got {result}"
    assert result.display_name == query


def test_repeated_resolution_is_deterministic_and_case_insensitive():
    ids = {resolve_team_identity(v).team_id for v in ["st. bonaventure", "ST. BONAVENTURE", "St. Bonaventure"]}
    assert len(ids) == 1


@pytest.mark.parametrize("straight,curly", [
    ("Saint Joseph's", "Saint Joseph’s"),
    ("St. Joseph's", "St. Joseph’s"),
    ("La Salle's", "La Salle’s"),  # possessive form, not a real alias, but must fold consistently
])
def test_curly_and_straight_apostrophes_resolve_equivalently(straight, curly):
    """
    2026-08-27 decision: the canonical identity layer treats curly (’)
    and straight (') apostrophes as equivalent. This is deliberately scoped
    to src.identity.team_identity only -- see test_legacy_compat.py for the
    preserved, unmodified notebook-exact behavior.
    """
    a = resolve_team_identity(straight)
    b = resolve_team_identity(curly)
    a_ok, b_ok = hasattr(a, "team_id"), hasattr(b, "team_id")
    assert a_ok == b_ok, f"straight resolved={a_ok}, curly resolved={b_ok} -- should agree"
    if a_ok:
        assert a.team_id == b.team_id


def test_check_team_alias_collisions_finds_known_real_collisions():
    collisions = check_team_alias_collisions()
    flagged_keys = {c["normalized_key"] for c in collisions}
    # These are real, previously-undetected collisions in the existing
    # canonicalizers (src.slug_utils.canonical_team_slug / normalize_team_name)
    # found by this Phase 2A collision check -- see docs/migration_state.md.
    assert "miami" in flagged_keys
    assert "loyola" in flagged_keys
    for collision in collisions:
        assert len(collision["team_ids"]) > 1
        assert len(set(collision["display_names"])) == len(collision["team_ids"])


# ---------------------------------------------------------------------------
# Protected team-slug remediation batch: the real local TeamDump/CBB
# directory-slug spellings for "miami-(oh)" and "saint-joseph's" -- confirmed
# by Phase 5B5's own player-game-log identity audit to fail resolution
# entirely (never a roster-absence problem; the team_identity itself never
# resolved). Fixed via two narrow, literal-string _SR_OVERRIDES entries in
# src/slug_utils.py, mirroring the exact, already-proven-safe pattern the
# file already uses for "st-thomas-(mn)" and "miami-(fl)". No fuzzy
# matching, no canonical_team_slug()/normalize_team_name() change, no new
# identity path.
# ---------------------------------------------------------------------------


def test_documented_directory_slug_miami_oh_resolves_to_correct_canonical_team():
    result = resolve_team_identity("miami-(oh)", source="sports_reference")
    assert hasattr(result, "team_id"), f"expected a resolved TeamIdentity, got {result!r}"
    assert result.display_name == "Miami (OH)"


def test_documented_directory_slug_saint_josephs_resolves_to_correct_canonical_team():
    result = resolve_team_identity("saint-joseph's", source="sports_reference")
    assert hasattr(result, "team_id"), f"expected a resolved TeamIdentity, got {result!r}"
    assert result.display_name == "Saint Joseph's"


def test_miami_oh_directory_slug_never_collides_with_miami_fl():
    """The exact bug this batch fixes was previously masked as a collision
    (canonical_team_slug("miami-(oh)") == canonical_team_slug("miami-(fl)") == "miami"),
    reported as UnresolvedIdentity(reason="ambiguous") before this correction. The new
    literal-string override is matched at Step 1 (exact alias), before the lossy
    canonical-slug collision path is ever reached -- confirmed here that the two remain
    fully distinct, real, different teams."""
    oh = resolve_team_identity("miami-(oh)", source="sports_reference")
    fl = resolve_team_identity("miami-(fl)", source="sports_reference")
    assert hasattr(oh, "team_id") and hasattr(fl, "team_id")
    assert oh.team_id != fl.team_id
    assert oh.display_name == "Miami (OH)" and fl.display_name == "Miami (FL)"


def test_saint_josephs_directory_slug_never_collides_with_other_saint_schools():
    joes = resolve_team_identity("saint-joseph's", source="sports_reference")
    louis = resolve_team_identity("Saint Louis", source="sports_reference")
    peters = resolve_team_identity("St. Peter's", source="sports_reference")
    francis = resolve_team_identity("Saint Francis (PA)", source="sports_reference")
    assert hasattr(joes, "team_id")
    assert joes.team_id not in {louis.team_id, peters.team_id, francis.team_id}


def test_existing_miami_and_saint_joseph_alias_forms_remain_unchanged():
    """Confirms this batch's two new literal-string entries are purely additive --
    every already-working alias form for these two teams (and the already-registered
    st-thomas-(mn)/miami-(fl) siblings the new entries are modeled on) still resolves
    to the exact same team_id as before this change."""
    oh_forms = ["Miami (OH)", "Miami OH", "miami (oh)"]
    fl_forms = ["Miami (FL)", "Miami FL", "miami-(fl)", "miami-fl"]
    joes_forms = ["Saint Joseph's", "St. Joseph's"]

    oh_ids = {resolve_team_identity(v, source="sports_reference").team_id for v in oh_forms}
    fl_ids = {resolve_team_identity(v, source="sports_reference").team_id for v in fl_forms}
    joes_ids = {resolve_team_identity(v, source="sports_reference").team_id for v in joes_forms}

    assert len(oh_ids) == 1 and len(fl_ids) == 1 and len(joes_ids) == 1
    assert resolve_team_identity("st-thomas-(mn)", source="sports_reference").team_id is not None


def test_unknown_slugs_still_remain_unresolved_no_broadened_matching():
    """Confirms this batch did not introduce any general punctuation-stripping or
    fuzzy behavior -- a made-up slug in the exact same shape as the two real fixes
    must still fail, never silently guessed."""
    from src.identity.contracts import UnresolvedIdentity

    for fake in ["miami-(zz)", "notreal-university's", "saint-nobodys"]:
        result = resolve_team_identity(fake, source="sports_reference")
        assert isinstance(result, UnresolvedIdentity), f"{fake!r} unexpectedly resolved: {result!r}"


def test_miami_oh_and_saint_josephs_resolution_is_deterministic_across_repeats():
    oh_ids = {resolve_team_identity("miami-(oh)", source="sports_reference").team_id for _ in range(5)}
    joes_ids = {resolve_team_identity("saint-joseph's", source="sports_reference").team_id for _ in range(5)}
    assert len(oh_ids) == 1
    assert len(joes_ids) == 1


def test_miami_oh_and_saint_josephs_resolution_order_independent():
    """Resolving in either order (or interleaved with other queries) must not change the outcome -- confirms no shared mutable state leaks between calls."""
    resolve_team_identity("Miami (FL)", source="sports_reference")
    first = resolve_team_identity("miami-(oh)", source="sports_reference").team_id
    resolve_team_identity("Saint Louis", source="sports_reference")
    second = resolve_team_identity("saint-joseph's", source="sports_reference").team_id
    third = resolve_team_identity("miami-(oh)", source="sports_reference").team_id
    assert first == third
    assert second is not None


# ---------------------------------------------------------------------------
# Phase 5C4A protected team-slug remediation: the real local TeamDump/CBB
# directory-slug spellings for "bowling-green-state" and "william-mary" --
# confirmed by a Phase 5C4 complete-source inventory to be two AllStats
# workbooks (each a complete, well-formed 6-file 2026 workbook) whose team
# identity never resolved. Fixed via two narrow, literal-string
# _SR_OVERRIDES entries in src/slug_utils.py, mirroring the exact,
# already-proven-safe pattern this file already uses for "miami-(oh)" and
# "saint-joseph's". No fuzzy matching, no canonical_team_slug()/
# normalize_team_name() change, no new identity path. Distinct from the
# separate, not-yet-wired-in sr_slug_reconciliation.json/
# resolve_team_identity_with_reconciliation path, which already resolves
# "william-mary" through a different mechanism not used by the bulk-import
# adapters this override serves (see tests/regression/test_william_mary_reconciliation.py).
# ---------------------------------------------------------------------------


def test_documented_directory_slug_bowling_green_state_resolves_to_correct_canonical_team():
    result = resolve_team_identity("bowling-green-state", source="sports_reference")
    assert hasattr(result, "team_id"), f"expected a resolved TeamIdentity, got {result!r}"
    assert result.display_name == "Bowling Green"


def test_documented_directory_slug_william_mary_resolves_to_correct_canonical_team():
    result = resolve_team_identity("william-mary", source="sports_reference")
    assert hasattr(result, "team_id"), f"expected a resolved TeamIdentity, got {result!r}"
    assert result.display_name == "William & Mary"


def test_bowling_green_state_resolves_to_same_team_id_as_canonical_bowling_green_forms():
    state_form = resolve_team_identity("bowling-green-state", source="sports_reference")
    canonical = resolve_team_identity("Bowling Green", source="sports_reference")
    bgsu = resolve_team_identity("BGSU", source="sports_reference")
    assert hasattr(state_form, "team_id") and hasattr(canonical, "team_id") and hasattr(bgsu, "team_id")
    assert state_form.team_id == canonical.team_id == bgsu.team_id


def test_william_mary_resolves_to_same_team_id_as_canonical_william_mary_forms():
    directory_form = resolve_team_identity("william-mary", source="sports_reference")
    ampersand_form = resolve_team_identity("William & Mary", source="sports_reference")
    legacy_registry_form = resolve_team_identity("william-&-mary", source="sports_reference")
    assert hasattr(directory_form, "team_id") and hasattr(ampersand_form, "team_id") and hasattr(legacy_registry_form, "team_id")
    assert directory_form.team_id == ampersand_form.team_id == legacy_registry_form.team_id


def test_existing_bowling_green_and_william_mary_alias_forms_remain_unchanged():
    """Confirms this batch's two new literal-string entries are purely additive --
    every already-working alias form for these two teams still resolves to the
    exact same team_id as before this change."""
    bg_forms = ["Bowling Green", "BGSU"]
    wm_forms = ["William & Mary", "william-&-mary"]

    bg_ids = {resolve_team_identity(v, source="sports_reference").team_id for v in bg_forms}
    wm_ids = {resolve_team_identity(v, source="sports_reference").team_id for v in wm_forms}

    assert len(bg_ids) == 1 and len(wm_ids) == 1


def test_unrelated_team_resolution_remains_unchanged_after_bowling_green_and_william_mary_fix():
    """Sanity spot-check that unrelated teams (including the other already-fixed
    protected slugs) still resolve exactly as before this batch's own addition."""
    for slug, expected_display_name in [
        ("miami-(oh)", "Miami (OH)"),
        ("saint-joseph's", "Saint Joseph's"),
        ("st-thomas-(mn)", "St. Thomas (MN)"),
        ("st-bonaventure", "St. Bonaventure"),
        ("michigan-state", "Michigan State"),
    ]:
        result = resolve_team_identity(slug, source="sports_reference")
        assert hasattr(result, "team_id"), f"{slug!r} unexpectedly failed to resolve: {result!r}"
        assert result.display_name == expected_display_name


def test_unknown_slugs_in_the_same_shape_still_remain_unresolved_after_bowling_green_and_william_mary_fix():
    """Confirms this batch did not introduce any general punctuation-stripping or
    fuzzy behavior -- a made-up slug in the exact same shape as the two real fixes
    must still fail, never silently guessed."""
    from src.identity.contracts import UnresolvedIdentity

    for fake in ["bowling-green-fake", "william-notmary", "mary-william"]:
        result = resolve_team_identity(fake, source="sports_reference")
        assert isinstance(result, UnresolvedIdentity), f"{fake!r} unexpectedly resolved: {result!r}"


def test_bowling_green_state_and_william_mary_resolution_is_deterministic_across_repeats():
    bg_ids = {resolve_team_identity("bowling-green-state", source="sports_reference").team_id for _ in range(5)}
    wm_ids = {resolve_team_identity("william-mary", source="sports_reference").team_id for _ in range(5)}
    assert len(bg_ids) == 1
    assert len(wm_ids) == 1


def test_bowling_green_state_and_william_mary_resolution_order_independent():
    """Resolving in either order (or interleaved with other queries) must not change the outcome -- confirms no shared mutable state leaks between calls."""
    resolve_team_identity("Miami (FL)", source="sports_reference")
    first = resolve_team_identity("bowling-green-state", source="sports_reference").team_id
    resolve_team_identity("Saint Louis", source="sports_reference")
    second = resolve_team_identity("william-mary", source="sports_reference").team_id
    third = resolve_team_identity("bowling-green-state", source="sports_reference").team_id
    assert first == third
    assert second is not None
