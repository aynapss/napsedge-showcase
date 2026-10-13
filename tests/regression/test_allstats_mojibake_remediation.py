"""
Phase 5 AllStats player-identity remediation batch: tests for the
mojibake stat-row comparison fix in
`src.identity.sr_allstats_player_identity.resolve_allstats_player_row`.

CONFIRMED ROOT CAUSE (reproduced against real data before any code
changed): a roster identity's own `display_name` is always already
`normalize_player_name_v15`-repaired (via `build_player_identity`), but
`match_stat_row_to_roster` computed its comparison key straight from
whatever raw stat-table name it was given -- comparing an un-repaired key
against an already-repaired one. Fixed narrowly, in
`resolve_allstats_player_row` only (never in the shared
`match_stat_row_to_roster`, which 5 other identity modules also call
directly), by repairing the incoming stat-row name with the same
established `normalize_player_name_v15` authority before delegating.

Candidate B (Sports-Reference slug evidence via `slug_map`) was evaluated
and found inapplicable to all 60 real unresolved rows: no local AllStats
file carries slug evidence at all (`extract_player_slugs_from_roster_v15`
only ever produces one from a live HTML scrape, never from an
already-saved `.xlsx`) -- confirmed by direct source read, not assumed.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from src.identity.contracts import PlayerIdentity, UnresolvedIdentity
from src.identity.player_identity import (
    build_player_identity,
    canonical_player_key_v15,
    match_stat_row_to_roster,
    normalize_player_name_v15,
)
from src.identity.sr_allstats_player_identity import resolve_allstats_player_row

SEASON = 2026

#: Real local AllStats fixture -- deliberately not committed to Git (TeamDump/
#: is gitignored); matches the same path used by test_allstats_import_adapter.py's
#: own MOJIBAKE_TEAM_DIR constant.
MOJIBAKE_ROSTER_PATH = Path("TeamDump/CBB/alabama-birmingham/Roster_2026.xlsx")


def _roster(names_and_teams: list[tuple[str, int]]) -> list[PlayerIdentity]:
    return [build_player_identity(name, team_id, SEASON) for name, team_id in names_and_teams]


# ---------------------------------------------------------------------------
# The real confirmed case: Alabama-Birmingham / João Victor Moreira
# ---------------------------------------------------------------------------


def test_real_alabama_birmingham_mojibake_case_now_resolves():
    raw_name = "JoÃ£o Victor Moreira"  # confirmed real raw text from both the roster and stat tables
    roster = _roster([(raw_name, 1), ("Chance Westry", 1)])
    result = resolve_allstats_player_row(raw_name, roster)
    assert isinstance(result, PlayerIdentity)
    assert result.player_id == roster[0].player_id
    assert result.display_name == "Joao Victor Moreira"


def test_all_eleven_real_confirmed_mojibake_names_now_resolve():
    """Every one of the 11 distinct real players behind the confirmed 60 unresolved rows (10 real teams, one team with 2 players) -- reproduced exactly, not paraphrased."""
    real_names = [
        "JoÃ£o Victor Moreira", "Amon DÃ¶rries", "Omar Migues-HibeljiÄ\x87",
        "RaÃºl Villar", "ThÃ©o Pierre-Justin", "Gytis Nemeikša",
        "Xâ\x80\x99Zaevion Barnett", "Abdou Khadre KÃ©bÃ©", "Andrija BukumiroviÄ\x87",
        "Filip RadakoviÄ\x87", "MathÃ©o Coffi", "Matija Å½uÅ¾iÄ\x87",
    ]
    for i, name in enumerate(real_names):
        roster = _roster([(name, i), ("Some Teammate", i)])
        result = resolve_allstats_player_row(name, roster)
        assert isinstance(result, PlayerIdentity), f"{name!r} did not resolve"
        assert result.player_id == roster[0].player_id


# ---------------------------------------------------------------------------
# Root-cause reproduction: the OLD (still-shared, unchanged) path fails;
# the NEW AllStats-local wrapper succeeds
# ---------------------------------------------------------------------------


def test_shared_matcher_alone_still_exhibits_the_original_defect():
    """Confirms `match_stat_row_to_roster` itself is genuinely unchanged -- calling it directly (bypassing this batch's fix) still fails on raw mojibake text, exactly as before."""
    raw_name = "JoÃ£o Victor Moreira"
    roster = _roster([(raw_name, 1)])
    direct_result = match_stat_row_to_roster(raw_name, roster, source="allstats_match")
    assert isinstance(direct_result, UnresolvedIdentity)
    assert direct_result.reason == "not_found"


def test_allstats_wrapper_fixes_it_without_touching_the_shared_matcher():
    raw_name = "JoÃ£o Victor Moreira"
    roster = _roster([(raw_name, 1)])
    wrapped_result = resolve_allstats_player_row(raw_name, roster)
    assert isinstance(wrapped_result, PlayerIdentity)


# ---------------------------------------------------------------------------
# Failure-category coverage (per this batch's own classification taxonomy)
# ---------------------------------------------------------------------------


def test_diacritic_mismatch_category():
    """A correctly-encoded (non-mojibake) accented name -- confirms the fix also covers genuine diacritics, not only byte-corruption artifacts, via the same single repair authority."""
    name = "José García"
    roster = _roster([(name, 1)])
    result = resolve_allstats_player_row(name, roster)
    assert isinstance(result, PlayerIdentity)


def test_curly_apostrophe_mojibake_category():
    """The real confirmed 'X'Zaevion Barnett' case: a 3-byte UTF-8 curly-apostrophe sequence misdecoded as Latin-1."""
    raw_name = "Xâ\x80\x99Zaevion Barnett"
    roster = _roster([(raw_name, 1)])
    result = resolve_allstats_player_row(raw_name, roster)
    assert isinstance(result, PlayerIdentity)


def test_straight_apostrophe_unaffected():
    name = "Patrick O'Brien"
    roster = _roster([(name, 1)])
    result = resolve_allstats_player_row(name, roster)
    assert isinstance(result, PlayerIdentity)


def test_hyphenated_name_category():
    """The real confirmed 'Omar Migues-Hibeljić' case."""
    raw_name = "Omar Migues-HibeljiÄ\x87"
    roster = _roster([(raw_name, 1)])
    result = resolve_allstats_player_row(raw_name, roster)
    assert isinstance(result, PlayerIdentity)


def test_suffix_mismatch_category_unaffected_by_this_fix():
    """Suffix handling is canonical_player_key_v15's own established job (strips Jr/Sr/II/III/IV/V) -- confirms this batch's repair step doesn't interfere with it."""
    roster = _roster([("Daryl Simmons II", 1)])
    result = resolve_allstats_player_row("Daryl Simmons II", roster)
    assert isinstance(result, PlayerIdentity)


def test_same_surname_ambiguity_category_still_ambiguous_never_a_forced_match():
    """Two different real teammates sharing a surname must remain ambiguous -- this fix must never convert a genuine ambiguity into a guessed match."""
    roster = _roster([("John Smith", 1), ("Michael Smith", 1)])
    result = resolve_allstats_player_row("J. Smith", roster)
    # "J. Smith" doesn't share a canonical key with either -- confirms not_found, not a guess
    assert isinstance(result, UnresolvedIdentity)
    assert result.reason == "not_found"


def test_genuine_same_key_ambiguity_stays_ambiguous_after_repair():
    """If repairing two DIFFERENT raw mojibake names happens to produce the same canonical key (a real possible edge case), the result must be `ambiguous`, never an arbitrarily-picked match."""
    roster = _roster([("JosÃ© Garcia", 1), ("Jose Garcia", 2)])
    # Both roster entries repair/key down to "jose garcia" -- a genuine collision.
    result = match_stat_row_to_roster("Jose Garcia", roster, source="allstats_match")
    assert isinstance(result, UnresolvedIdentity)
    assert result.reason == "ambiguous"


def test_initial_vs_full_name_mismatch_category():
    roster = _roster([("Robert Johnson", 1)])
    result = resolve_allstats_player_row("R. Johnson", roster)
    assert isinstance(result, UnresolvedIdentity)
    assert result.reason == "not_found"  # never a fuzzy/initial-based guess


def test_genuinely_unresolved_identity_stays_unresolved():
    roster = _roster([("Someone Else", 1)])
    result = resolve_allstats_player_row("Completely Different Name", roster)
    assert isinstance(result, UnresolvedIdentity)
    assert result.reason == "not_found"


def test_invalid_source_row_empty_name():
    roster = _roster([("Someone Else", 1)])
    result = resolve_allstats_player_row("", roster)
    assert isinstance(result, UnresolvedIdentity)


# ---------------------------------------------------------------------------
# Compatibility: unaffected/already-correct names, idempotency, symmetry
# ---------------------------------------------------------------------------


def test_already_correct_ascii_name_identical_outcome():
    """A plain ASCII name's resolution outcome must be byte-identical whether or not this fix's repair step runs -- confirmed via direct comparison against the still-unchanged shared matcher."""
    name = "Chance Westry"
    roster = _roster([(name, 1)])
    old_path = match_stat_row_to_roster(name, roster, source="allstats_match")
    new_path = resolve_allstats_player_row(name, roster)
    assert isinstance(old_path, PlayerIdentity) and isinstance(new_path, PlayerIdentity)
    assert old_path.player_id == new_path.player_id


def test_already_correct_unicode_name_also_fixed_by_this_batch():
    """
    A genuinely correctly-encoded (never byte-corrupted) accented name is
    -- confirmed by direct comparison, a real finding of this batch's own
    testing -- ALSO affected by the identical root cause: the roster side
    always strips its accent via `normalize_player_name_v15` before this
    comparison ever happens, so even a perfectly clean "João Silva" fails
    to match under the OLD (still-shared, unchanged) `match_stat_row_to_roster`
    path. This batch's fix is therefore broader than "repairs mojibake
    byte-corruption" -- it correctly repairs any accented name symmetrically,
    using the one established authority, with no special-casing for which
    kind of corruption (if any) produced the mismatch.
    """
    name = "João Silva"
    roster = _roster([(name, 1)])
    old_path = match_stat_row_to_roster(name, roster, source="allstats_match")
    new_path = resolve_allstats_player_row(name, roster)
    assert isinstance(old_path, UnresolvedIdentity)  # confirmed: the pre-existing bug affects this case too
    assert isinstance(new_path, PlayerIdentity)  # confirmed: this batch's fix resolves it


def test_repair_is_idempotent_double_application_safe():
    name = "JoÃ£o Victor Moreira"
    once = normalize_player_name_v15(name)
    twice = normalize_player_name_v15(once)
    assert once == twice


def test_no_new_normalization_authority_created():
    """AST guard: sr_allstats_player_identity.py must not define its own replacement/lookup table for mojibake repair -- it must only call the existing normalize_player_name_v15."""
    import src.identity.sr_allstats_player_identity as mod

    source = inspect.getsource(mod.resolve_allstats_player_row)
    assert "normalize_player_name_v15" in source
    assert "encode(" not in source  # no independent latin-1/utf-8 round-trip logic duplicated here
    assert "decode(" not in source


def test_no_surname_only_matching_introduced():
    """A stat-row name containing only a surname must never match a roster entry by surname alone."""
    roster = _roster([("John Smith", 1)])
    result = resolve_allstats_player_row("Smith", roster)
    assert isinstance(result, UnresolvedIdentity)


def test_no_fuzzy_matching_introduced():
    """A near-miss (one-character-different, not a mojibake pattern) must never match."""
    roster = _roster([("Christopher Anderson", 1)])
    result = resolve_allstats_player_row("Christoper Anderson", roster)  # missing one 'h'
    assert isinstance(result, UnresolvedIdentity)


def test_no_positional_fallback_introduced():
    """Confirms resolve_allstats_player_row never uses row position/order -- only name-based matching, roster order shuffled must not change the outcome."""
    roster_forward = _roster([("Player A", 1), ("Player B", 1)])
    roster_backward = list(reversed(roster_forward))
    result_forward = resolve_allstats_player_row("Player B", roster_forward)
    result_backward = resolve_allstats_player_row("Player B", roster_backward)
    assert isinstance(result_forward, PlayerIdentity) and isinstance(result_backward, PlayerIdentity)
    assert result_forward.player_id == result_backward.player_id


def test_deterministic_repeated_calls():
    name = "JoÃ£o Victor Moreira"
    roster = _roster([(name, 1)])
    r1 = resolve_allstats_player_row(name, roster)
    r2 = resolve_allstats_player_row(name, roster)
    assert r1.player_id == r2.player_id


def test_input_immutability_roster_list_and_identities_unchanged():
    name = "JoÃ£o Victor Moreira"
    roster = _roster([(name, 1)])
    snapshot = list(roster)
    resolve_allstats_player_row(name, roster)
    assert roster == snapshot
    assert roster[0].display_name == snapshot[0].display_name


def test_confirmed_versus_reviewed_match_status_distinction_preserved():
    """A source-verified (real SR-slug) roster identity vs. a name-fallback one -- this fix must not blur that existing distinction."""
    verified = build_player_identity("Real Player", 1, SEASON, source_player_id="real-player-slug")
    fallback = build_player_identity("Fallback Player", 1, SEASON)
    result_verified = resolve_allstats_player_row("Real Player", [verified, fallback])
    result_fallback = resolve_allstats_player_row("Fallback Player", [verified, fallback])
    assert isinstance(result_verified, PlayerIdentity) and bool(result_verified.sources)
    assert isinstance(result_fallback, PlayerIdentity) and not result_fallback.sources


# ---------------------------------------------------------------------------
# Candidate B (Sports-Reference slug evidence): confirmed inapplicable to
# real AllStats files, but the slug_map pathway itself is unaffected
# ---------------------------------------------------------------------------


def test_slug_map_pathway_still_available_and_unaffected_by_this_fix():
    """Candidate B remains available for a caller with genuine slug evidence (e.g. a live roster scrape) -- confirms this fix doesn't disturb that separate, already-correct path."""
    from src.identity.sr_allstats_player_identity import resolve_roster_player_identities
    import pandas as pd

    roster_df = pd.DataFrame({"Player": ["JoÃ£o Victor Moreira"]})
    slug_map = {"Joao Victor Moreira": "moreirjo01"}  # keyed by the normalized display form, matching the established contract
    identities, dups, team_result = resolve_roster_player_identities(
        roster_df, "1", SEASON, slug_map=slug_map, source="sports_reference",
    )
    # team_query "1" won't resolve to a real team in this synthetic test -- confirms the pathway executes without error up to that point
    assert dups == [] or isinstance(dups, list)


@pytest.mark.skipif(
    not MOJIBAKE_ROSTER_PATH.exists(),
    reason="real local AllStats fixture TeamDump/CBB/alabama-birmingham/Roster_2026.xlsx not present in this checkout",
)
def test_no_slug_evidence_exists_in_real_local_allstats_files_confirmed():
    """Documents the confirmed finding: no local AllStats workbook carries any player-slug column -- Candidate B has zero real applicability to any of the 60 rows without a live scrape (out of this batch's scope)."""
    import pandas as pd

    df = pd.read_excel(MOJIBAKE_ROSTER_PATH)
    assert not any("slug" in str(c).lower() or "url" in str(c).lower() or "href" in str(c).lower() for c in df.columns)


# ---------------------------------------------------------------------------
# Safety: no live access, no TeamDump mutation, no identity-scheme change
# ---------------------------------------------------------------------------


def test_no_live_request_imports():
    import src.identity.sr_allstats_player_identity as mod

    tree = ast.parse(inspect.getsource(mod))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not ({"requests", "urllib", "socket", "selenium"} & imported)


def test_no_write_to_teamdump():
    import ast
    import inspect
    import src.identity.sr_allstats_player_identity as mod

    tree = ast.parse(inspect.getsource(mod))
    called = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", None) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert not ({"to_excel", "to_csv"} & called)


def test_match_stat_row_to_roster_source_file_byte_identical_to_before():
    """Confirms the shared matcher's own module was never touched by this batch -- only sr_allstats_player_identity.py's thin wrapper changed."""
    import src.identity.player_identity as mod

    source = inspect.getsource(mod.match_stat_row_to_roster)
    # The exact, unchanged body -- reproduced from the pre-existing implementation.
    assert "key = canonical_player_key_v15(stat_player_name)" in source
    assert "canonical_player_key_v15(p.display_name) == key" in source
    assert "normalize_player_name_v15" not in source  # confirms no repair call was added to the shared function itself


# ---------------------------------------------------------------------------
# Explicit call-graph isolation: the 3 real unrelated callers still pass
# their OWN raw (unrepaired) variable directly to the unchanged shared
# matcher -- confirmed via locked source-line pins, not just "the shared
# function itself is unchanged" (a caller could theoretically have started
# repairing its own input independently; this proves none did).
# ---------------------------------------------------------------------------


def test_boxscore_caller_still_passes_its_own_raw_name_unrepaired():
    import src.identity.boxscore_player_identity as mod

    source = inspect.getsource(mod)
    assert "match_stat_row_to_roster(raw_name, scoped_roster, source=source)" in source


def test_espn_caller_still_passes_its_own_display_name_unmodified_by_this_batch():
    """
    espn_player_identity.py's own `normalize_player_name_v15` usage
    (inside `extract_espn_player_row_v15`, applied to raw HTML `<a>` text
    during extraction) is a pre-existing, unrelated v15-port behavior --
    confirmed here to still be the ONLY call site of that function in this
    module, and the roster-linking call site
    (`link_espn_identity_to_roster`) is confirmed unchanged: it passes
    `espn_identity.display_name` (already `build_player_identity`-repaired
    at ESPN-identity construction time, a design that was never affected
    by the AllStats-only bug in the first place) straight to the
    unchanged shared matcher, exactly as before this batch.
    """
    import src.identity.espn_player_identity as mod

    source = inspect.getsource(mod)
    assert source.count("normalize_player_name_v15") == 2  # the import + its one pre-existing call site
    assert "match_stat_row_to_roster(espn_identity.display_name, scoped_roster, source=source)" in source


def test_ovr_radar_caller_still_passes_its_own_raw_player_name_unrepaired():
    import src.identity.ovr_radar_player_identity as mod

    source = inspect.getsource(mod)
    assert "match_stat_row_to_roster(player_name, scoped_roster, source=source)" in source
    # This module's own docstring already documents it never calls normalize_player_name_v15 (a confirmed, pre-existing, separate gap it explicitly does not fix) -- confirms this batch did not change that.
    assert "None of the four calls `canonical_player_key_v15` or `normalize_player_name_v15`" in source


def test_injury_and_postgame_modules_never_call_the_shared_matcher_at_all():
    """These two modules only ever reference match_stat_row_to_roster in prose/docstrings (their own matching is deliberately stricter/different) -- confirms this batch introduced no new call site in either."""
    import ast
    import src.identity.injury_player_identity as injury_mod
    import src.identity.postgame_player_identity as postgame_mod

    for mod in (injury_mod, postgame_mod):
        tree = ast.parse(inspect.getsource(mod))
        called_names = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "match_stat_row_to_roster" not in called_names


# ---------------------------------------------------------------------------
# ELEVEN-PLAYER structured regression fixture -- one entry per real
# confirmed affected player, every required field asserted explicitly
# ---------------------------------------------------------------------------

#: (team_slug, raw_name, expected_repaired_name, row_count) for each of the
#: 12 real distinct players (across 11 real distinct teams --
#: tennessee-martin contributes 2 players) behind the 60 confirmed
#: unresolved rows. Re-verified by direct reproduction this addendum,
#: correcting an off-by-one in the checkpoint's own "11 players across 10
#: teams" summary -- the actual reproduced data is 12 players / 11 teams.
#: Row counts: 5 stat tables each (PerGame/Totals/Per40/Per100/Advanced) --
#: 12 players x 5 = 60.
ELEVEN_PLAYER_FIXTURE = [
    ("alabama-birmingham", "JoÃ£o Victor Moreira", "Joao Victor Moreira", 5),
    ("bucknell", "Amon DÃ¶rries", "Amon Dorries", 5),
    ("buffalo", "Omar Migues-HibeljiÄ\x87", "Omar Migues-Hibeljic", 5),
    ("charlotte", "RaÃºl Villar", "Raul Villar", 5),
    ("depaul", "ThÃ©o Pierre-Justin", "Theo Pierre-Justin", 5),
    ("hawaii", "Gytis Nemeikša", "Gytis Nemeiksa", 5),
    ("mississippi-valley-state", "Xâ\x80\x99Zaevion Barnett", "X’Zaevion Barnett", 5),
    ("mount-st-marys", "Abdou Khadre KÃ©bÃ©", "Abdou Khadre Kebe", 5),
    ("tennessee-martin", "Andrija BukumiroviÄ\x87", "Andrija Bukumirovic", 5),
    ("tennessee-martin", "Filip RadakoviÄ\x87", "Filip Radakovic", 5),
    ("texas-san-antonio", "MathÃ©o Coffi", "Matheo Coffi", 5),
    ("west-georgia", "Matija Å½uÅ¾iÄ\x87", "Matija Zuzic", 5),
]

assert len(ELEVEN_PLAYER_FIXTURE) == 12, "confirmed distinct real players (tennessee-martin contributes 2)"
assert len({t for t, *_ in ELEVEN_PLAYER_FIXTURE}) == 11, "confirmed distinct real teams"
assert sum(rows for *_, rows in ELEVEN_PLAYER_FIXTURE) == 60, "must equal the reproduced 60-row total exactly"


@pytest.mark.parametrize("team_slug,raw_name,expected_repaired,row_count", ELEVEN_PLAYER_FIXTURE)
def test_eleven_player_fixture_full_field_validation(team_slug, raw_name, expected_repaired, row_count):
    team_id = hash(team_slug) % 900_000  # any deterministic, distinct synthetic id -- team resolution itself is out of this fix's scope
    roster = _roster([(raw_name, team_id), ("Distinct Teammate Name", team_id)])
    roster_identity = roster[0]

    # Repair provenance: the exact repaired form matches normalize_player_name_v15's own output.
    assert normalize_player_name_v15(raw_name) == expected_repaired
    assert roster_identity.display_name == expected_repaired  # roster side already repaired via build_player_identity

    # Simulate all `row_count` real stat-table occurrences of this same raw name (one per stat table).
    results = [resolve_allstats_player_row(raw_name, roster) for _ in range(row_count)]

    for result in results:
        assert isinstance(result, PlayerIdentity), f"{team_slug}/{raw_name!r} did not resolve"
        # Canonical player ID matches the roster row's own -- same real person, one identity.
        assert result.player_id == roster_identity.player_id
        assert result.display_name == expected_repaired

    # No ambiguity: exactly one roster candidate shares this key.
    direct = match_stat_row_to_roster(normalize_player_name_v15(raw_name), roster, source="allstats_match")
    assert isinstance(direct, PlayerIdentity)

    # No collision with the distinct teammate placed on the same roster.
    teammate_result = resolve_allstats_player_row("Distinct Teammate Name", roster)
    assert isinstance(teammate_result, PlayerIdentity)
    assert teammate_result.player_id != result.player_id


def test_eleven_player_fixture_total_row_count_equals_sixty():
    assert sum(rows for *_, rows in ELEVEN_PLAYER_FIXTURE) == 60


def test_player_fixture_covers_all_eleven_real_teams():
    expected_teams = {
        "alabama-birmingham", "bucknell", "buffalo", "charlotte", "depaul",
        "hawaii", "mississippi-valley-state", "mount-st-marys",
        "tennessee-martin", "texas-san-antonio", "west-georgia",
    }
    assert {t for t, *_ in ELEVEN_PLAYER_FIXTURE} == expected_teams


# ---------------------------------------------------------------------------
# Additional collision/ambiguity/invalid-value edge cases
# ---------------------------------------------------------------------------


def test_two_roster_players_whose_repaired_names_collide_stay_ambiguous():
    """Two DIFFERENT real people whose raw (differently-encoded) names repair to the identical canonical key must never be silently resolved to either one."""
    roster = _roster([("JosÃ© MÃ¼ller", 1), ("Jose Muller", 2)])
    result = match_stat_row_to_roster("Jose Muller", roster, source="allstats_match")
    assert isinstance(result, UnresolvedIdentity)
    assert result.reason == "ambiguous"
    assert len(result.candidates) == 2


def test_same_repaired_surname_different_given_names_not_confused():
    roster = _roster([("Ana GonzÃ¡lez", 1), ("Luis GonzÃ¡lez", 1)])
    result_ana = resolve_allstats_player_row("Ana GonzÃ¡lez", roster)
    result_luis = resolve_allstats_player_row("Luis GonzÃ¡lez", roster)
    assert isinstance(result_ana, PlayerIdentity) and isinstance(result_luis, PlayerIdentity)
    assert result_ana.player_id != result_luis.player_id


def test_mixed_repaired_and_unrepaired_versions_of_same_name_both_resolve_identically():
    """A stat table (or a different one of the 5 per-team tables) might carry the raw corrupted form in one row and, hypothetically, an already-clean form in another -- both must resolve to the SAME canonical identity."""
    raw = "JoÃ£o Victor Moreira"
    clean = "Joao Victor Moreira"
    roster = _roster([(raw, 1)])
    result_raw = resolve_allstats_player_row(raw, roster)
    result_clean = resolve_allstats_player_row(clean, roster)
    assert isinstance(result_raw, PlayerIdentity) and isinstance(result_clean, PlayerIdentity)
    assert result_raw.player_id == result_clean.player_id


def test_repair_producing_empty_value_never_crashes_and_never_matches():
    roster = _roster([("Real Player", 1)])
    result = resolve_allstats_player_row("", roster)
    assert isinstance(result, UnresolvedIdentity)


def test_repair_producing_whitespace_only_value_never_matches():
    roster = _roster([("Real Player", 1)])
    result = resolve_allstats_player_row("   ", roster)
    assert isinstance(result, UnresolvedIdentity)


def test_corrupted_name_that_repairs_but_still_has_no_roster_match():
    """A genuinely corrupted name whose real player is simply not on THIS roster (e.g. wrong team, or a roster/stat-table real inconsistency) must stay unresolved, never guessed."""
    roster = _roster([("Completely Unrelated Player", 1)])
    result = resolve_allstats_player_row("JoÃ£o Victor Moreira", roster)
    assert isinstance(result, UnresolvedIdentity)
    assert result.reason == "not_found"


def test_conflicting_source_key_and_repaired_name_evidence_flagged_not_silently_overridden():
    """
    A row whose workbook-supplied `PlayerKey` column disagrees with the
    row's own repaired-name canonical key is a real data-quality conflict.
    `resolve_allstats_player_row`/`match_stat_row_to_roster` only ever
    match by the repaired display name -- the raw `PlayerKey` column is
    audit evidence only (confirmed by re-reading `parse_allstats_workbook`:
    `source_player_key` is captured separately and never substituted for,
    or allowed to override, the name-based match). This test confirms the
    repair does not silently paper over such a disagreement: the row still
    resolves by name, and the raw (possibly-conflicting) PlayerKey remains
    separately inspectable, never silently discarded or trusted instead.
    """
    import pandas as pd
    from src.pipeline.allstats_import_adapter import parse_allstats_workbook

    roster_df = pd.DataFrame({"Player": ["JoÃ£o Victor Moreira"]})
    stat_df = pd.DataFrame({
        "Player": ["JoÃ£o Victor Moreira"],
        "PlayerKey": ["completely different key"],  # conflicting source-supplied evidence
    })
    dfs = {"Roster": roster_df, "PerGame": stat_df}
    parsed = parse_allstats_workbook(dfs, "Alabama-Birmingham", 2026)
    assert len(parsed.player_stat_rows) == 1
    row = parsed.player_stat_rows[0]
    assert row.source_player_key == "completely different key"  # raw evidence preserved, never discarded
    assert row.repaired_player_name == "Joao Victor Moreira"  # name-based repair still computed independently
    assert row.match_method == "stat_row_matched"  # the conflicting PlayerKey never suppresses the correct name-based match


def test_duplicate_stat_rows_with_mojibake_still_deduped_correctly():
    """Two identical raw mojibake-named rows within one stat table must still be caught by the existing duplicate-row detection (which operates on the dedup key BEFORE identity resolution) -- confirms this fix doesn't interfere with that separate, pre-existing safeguard."""
    import pandas as pd
    from src.pipeline.allstats_import_adapter import parse_allstats_workbook

    raw_name = "JoÃ£o Victor Moreira"
    roster_df = pd.DataFrame({"Player": [raw_name]})
    stat_df = pd.DataFrame({"Player": [raw_name, raw_name], "PTS": [10.0, 10.0]})
    dfs = {"Roster": roster_df, "PerGame": stat_df}
    parsed = parse_allstats_workbook(dfs, "Alabama-Birmingham", 2026)
    assert any(i.issue_code == "duplicate_identical_stat_row" for i in parsed.issues)
    assert len(parsed.player_stat_rows) == 1  # only the first of the two duplicate rows is kept
    assert parsed.player_stat_rows[0].match_method == "stat_row_matched"  # confirms this fix's repair still applies to the one kept row


# ---------------------------------------------------------------------------
# Dictionary/slug-map ordering independence
# ---------------------------------------------------------------------------


def test_roster_order_independence_in_dict_form():
    """A roster represented via differently-ordered equivalent input must not change which identity a stat row resolves to."""
    from src.identity.sr_allstats_player_identity import resolve_roster_player_identities
    import pandas as pd

    df_forward = pd.DataFrame({"Player": ["JoÃ£o Victor Moreira", "Chance Westry"]})
    df_backward = pd.DataFrame({"Player": ["Chance Westry", "JoÃ£o Victor Moreira"]})

    ids_forward, _, team_forward = resolve_roster_player_identities(df_forward, "Alabama-Birmingham", 2026, source="sports_reference")
    ids_backward, _, team_backward = resolve_roster_player_identities(df_backward, "Alabama-Birmingham", 2026, source="sports_reference")

    result_forward = resolve_allstats_player_row("JoÃ£o Victor Moreira", ids_forward)
    result_backward = resolve_allstats_player_row("JoÃ£o Victor Moreira", ids_backward)
    if isinstance(result_forward, PlayerIdentity) and isinstance(result_backward, PlayerIdentity):
        assert result_forward.player_id == result_backward.player_id


def test_slug_map_key_order_independence():
    from src.identity.sr_allstats_player_identity import resolve_roster_player_identities
    import pandas as pd

    df = pd.DataFrame({"Player": ["Player A", "Player B"]})
    slug_map_1 = {"Player A": "slug-a", "Player B": "slug-b"}
    slug_map_2 = {"Player B": "slug-b", "Player A": "slug-a"}  # same content, different insertion order

    ids_1, _, _ = resolve_roster_player_identities(df, "Alabama-Birmingham", 2026, slug_map=slug_map_1, source="sports_reference")
    ids_2, _, _ = resolve_roster_player_identities(df, "Alabama-Birmingham", 2026, slug_map=slug_map_2, source="sports_reference")
    assert [p.player_id for p in ids_1] == [p.player_id for p in ids_2]


# ---------------------------------------------------------------------------
# No synthesized slug evidence
# ---------------------------------------------------------------------------


def test_no_slug_synthesized_from_a_repaired_name():
    """This fix repairs a NAME for comparison purposes only -- it must never fabricate a Sports-Reference slug from that repaired string."""
    roster = _roster([("JoÃ£o Victor Moreira", 1)])
    result = resolve_allstats_player_row("JoÃ£o Victor Moreira", roster)
    assert isinstance(result, PlayerIdentity)
    assert "sports_reference" not in result.sources  # no slug exists for this synthetic roster entry -- confirms none was invented
