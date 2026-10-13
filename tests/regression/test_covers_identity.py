"""
Regression tests for src/identity/covers_identity.py (Phase 2A, Covers/injury adapter).

Builds a real covers_slug_map via the actual, already-verified
src.scraper.build_covers_ncaab_slug_map() -- not a reimplementation of its
key-derivation logic -- by injecting a mock HTTP session that serves
crafted fixture HTML shaped like covers.com's real teams-index page. Zero
network calls; src/scraper.py itself is not modified.
"""
import pytest

from src.identity.contracts import TeamIdentity
from src.identity.covers_identity import (
    resolve_covers_slug_via_registry,
    resolve_covers_team_identity,
)
from src.scraper import build_covers_ncaab_slug_map

_FIXTURE_HTML = """
<html><body>
<a href="/sport/basketball/ncaab/teams/main/saint-joseph-s-hawks/injuries">Saint Joseph's Hawks</a>
<a href="/sport/basketball/ncaab/teams/main/miami-fl-hurricanes/injuries">Miami (FL) Hurricanes</a>
<a href="/sport/basketball/ncaab/teams/main/miami-oh-redhawks/injuries">Miami (OH) RedHawks</a>
<a href="/sport/basketball/ncaab/teams/main/vcu-rams/injuries">VCU Rams</a>
<a href="/sport/basketball/ncaab/teams/main/uab-blazers/injuries">UAB Blazers</a>
<a href="/sport/basketball/ncaab/teams/main/st-bonaventure-bonnies/injuries">St. Bonaventure Bonnies</a>
<a href="/sport/basketball/ncaab/teams/main/loyola-il-ramblers/injuries">Loyola Chicago Ramblers</a>
<a href="/sport/basketball/ncaab/teams/main/loyola-md-greyhounds/injuries">Loyola Maryland Greyhounds</a>
</body></html>
"""


class _FakeResponse:
    def __init__(self, text):
        self.text = text
        self.status_code = 200

    def raise_for_status(self):
        pass


class _FakeSession:
    def get(self, url, headers=None, timeout=None):
        return _FakeResponse(_FIXTURE_HTML)


@pytest.fixture
def covers_slug_map(tmp_path):
    """A real covers_slug_map built by the actual (unmodified) src.scraper
    function, via a mocked session -- no network call, no shared/production
    cache file touched (uses a throwaway cache_path under tmp_path)."""
    cache_path = str(tmp_path / "covers_slug_cache_test.json")
    return build_covers_ncaab_slug_map(
        force_refresh=True, cache_path=cache_path, session=_FakeSession()
    )


# ---------------------------------------------------------------------------
# Inbound: Covers slug/name -> canonical team
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("covers_input,expected_display_name", [
    ("saint-joseph-s-hawks", "Saint Joseph's"),
    ("Saint Joseph's Hawks", "Saint Joseph's"),
    ("st-bonaventure-bonnies", "St. Bonaventure"),
    ("vcu-rams", "Virginia Commonwealth"),
    ("uab-blazers", "Alabama-Birmingham"),
    ("miami-fl-hurricanes", "Miami (FL)"),
    ("miami-oh-redhawks", "Miami (OH)"),
    ("loyola-il-ramblers", "Loyola Chicago"),
    ("loyola-md-greyhounds", "Loyola (MD)"),
])
def test_covers_slug_or_name_resolves_to_correct_canonical_team(covers_input, expected_display_name):
    result = resolve_covers_team_identity(covers_input)
    assert isinstance(result, TeamIdentity), f"{covers_input!r} should resolve, got {result}"
    assert result.display_name == expected_display_name


def test_covers_apostrophe_encoding_is_correctly_reversed():
    # "-s-" in a Covers slug means an apostrophe, not a literal word "s" --
    # this is the crux of why covers_slug_map.get(sr_slug) fails for
    # apostrophe schools (sr_slug is "saint-josephs", no embedded hyphen).
    result = resolve_covers_team_identity("saint-joseph-s-hawks")
    assert isinstance(result, TeamIdentity)
    assert result.display_name == "Saint Joseph's"


def test_unknown_covers_team_returns_unresolved():
    result = resolve_covers_team_identity("not-a-real-team-xyz")
    assert not isinstance(result, TeamIdentity)
    assert result.reason == "not_found"


def test_directional_and_abbreviation_covers_names_resolve_correctly():
    # UAB (abbreviation-branded on Covers) must resolve to its
    # full-legal-name SR-slug program, not fail the way
    # covers_slug_map.get(sr_slug) would.
    result = resolve_covers_team_identity("uab-blazers")
    assert isinstance(result, TeamIdentity)
    assert result.display_name == "Alabama-Birmingham"


def test_repeated_resolution_is_deterministic():
    ids = {resolve_covers_team_identity("vcu-rams").team_id for _ in range(3)}
    assert len(ids) == 1


# ---------------------------------------------------------------------------
# Outbound: canonical team -> Covers slug (given a real, built covers_slug_map)
# ---------------------------------------------------------------------------

def test_outbound_resolution_finds_slug_for_apostrophe_school(covers_slug_map):
    # This is the case that PROVES covers_slug_map.get(sr_slug) was broken:
    # sr_slug for Saint Joseph's is "saint-josephs", which is never a key
    # in covers_slug_map (see docs/migration_state.md for the full proof).
    assert "saint-josephs" not in covers_slug_map  # confirms the original assumption fails
    slug = resolve_covers_slug_via_registry("Saint Joseph's", covers_slug_map)
    assert slug == "saint-joseph-s-hawks"


def test_outbound_resolution_finds_slug_for_abbreviation_branded_school(covers_slug_map):
    # sr_slug for VCU is "virginia-commonwealth" -- also never a key in
    # covers_slug_map (Covers' own slug is the short "vcu-rams", too short
    # for the mascot-stripping key to ever produce the full name).
    assert "virginia-commonwealth" not in covers_slug_map
    slug = resolve_covers_slug_via_registry("VCU", covers_slug_map)
    assert slug == "vcu-rams"


@pytest.mark.parametrize("query,expected_slug", [
    ("Miami (FL)", "miami-fl-hurricanes"),
    ("Miami (OH)", "miami-oh-redhawks"),
    ("St. Bonaventure", "st-bonaventure-bonnies"),
    ("Loyola Chicago", "loyola-il-ramblers"),
    ("Loyola (MD)", "loyola-md-greyhounds"),
])
def test_outbound_resolution_disambiguates_miami_and_loyola(covers_slug_map, query, expected_slug):
    # The classic ambiguity pair: bare "miami" is a key in the real map for
    # BOTH teams (first-write-wins), but here we resolve the full query
    # first and search by resolved team_id, never touching the collided key.
    slug = resolve_covers_slug_via_registry(query, covers_slug_map)
    assert slug == expected_slug


def test_miami_bare_key_collision_exists_in_the_built_map(covers_slug_map):
    # Documents the exact collision found during investigation: both Miami
    # teams' mascot-stripped key derivation produces bare "miami", and
    # build_covers_ncaab_slug_map's own `not in slug_map` guard makes this
    # silent first-write-wins -- not something this adapter can fix without
    # modifying that (unchanged, verified-faithful) function.
    assert "miami" in covers_slug_map
    assert covers_slug_map["miami"] in {"miami-fl-hurricanes", "miami-oh-redhawks"}


def test_outbound_resolution_returns_none_for_team_not_in_the_map(covers_slug_map):
    # Davidson is a real, resolvable canonical team but was never in the
    # fixture HTML, so it must not be in the map -- and the function must
    # say so honestly (None), not guess via nearest-string match.
    slug = resolve_covers_slug_via_registry("Davidson", covers_slug_map)
    assert slug is None


def test_sr_and_covers_slug_namespaces_are_not_treated_as_interchangeable(covers_slug_map):
    """
    Direct confirmation of the primary investigation question: SR slugs and
    Covers slugs are different strings for the same team in the general
    case, and resolve_covers_slug_via_registry does not assume otherwise --
    it never does map.get(sr_slug) internally.
    """
    from src.identity.team_identity import resolve_team_identity
    from src import slug_utils

    for query in ["Saint Joseph's", "VCU", "Miami (FL)"]:
        team = resolve_team_identity(query)
        sr_slug = slug_utils.TEAM_REGISTRY[team.team_id].sr_slug
        covers_slug = resolve_covers_slug_via_registry(query, covers_slug_map)
        assert covers_slug is not None, f"{query!r} should have a Covers slug in the fixture map"
        if sr_slug != covers_slug:
            # This is the expected/common case (Saint Joseph's, VCU) --
            # explicitly not an error.
            continue
    # At least one of the three must actually differ, proving the two
    # namespaces are genuinely distinct for at least part of this fixture.
    mismatches = [
        q for q in ["Saint Joseph's", "VCU"]
        if slug_utils.TEAM_REGISTRY[resolve_team_identity(q).team_id].sr_slug
        != resolve_covers_slug_via_registry(q, covers_slug_map)
    ]
    assert mismatches, "expected at least one SR-slug/Covers-slug mismatch in this fixture"
