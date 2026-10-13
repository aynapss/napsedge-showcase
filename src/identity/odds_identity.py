"""
Odds/sportsbook team-identity adapter (Phase 2A, adapter batch 4).

ACTIVE V15 DEFINITIONS AND CALL SITES

`normalize_for_odds` is defined three times (cells 78, 443, 462, all
`SHADOWED_IN_NOTEBOOK`). Classified this session:

  - cell 78:  ``normalize_name(name)`` + manual " state"->" st" replace.
              No confirmed direct callers found beyond its own definition.
  - cell 443: ``return canonical_odds_team(name)``. Comment claims
              "Single source of truth used by find_game_odds()" -- **false**
              at actual notebook execution order: `find_game_odds` (cell 473)
              never calls `normalize_for_odds` at all; it calls
              ``canonical_odds_team`` directly (see below). This comment was
              already stale before this session found it.
  - cell 462: ``canonicalize_team(name)`` (src/scraper.py's simple version)
              + ``ODDS_TEAM_ALIASES`` lookup. Last-defined, so technically
              "active" if anything calls the bare name `normalize_for_odds`
              -- but no such caller was found either.

**Correction to a finding from the ESPN/Logo adapter batch**: that batch
concluded `canonical_odds_team` was "dead code" because it isn't reached
through `normalize_for_odds`. That was too narrow -- `canonical_odds_team`
has extensive DIRECT call sites of its own, independent of
`normalize_for_odds`: `find_game_odds` (cell 473, line ``normed =
canonical_odds_team(stripped)``, the actual production matcher used by the
live betting pipeline at cells 480/492/549), `same_team()` (cell 477),
and repeated debug/print call sites (cells 493, 525, 541). `find_game_odds`
is therefore the real, heavily-used odds-matching entry point, and
`canonical_odds_team` is genuinely active via it -- not shadowed out.

`canonical_school` (cell 464) has exactly one call site found (cell 494,
directly beside a `canonical_odds_team` debug-print at cell 493) -- reads
as the notebook author comparing the two approaches side by side, not a
real dependency anywhere downstream. Not used by this adapter's canonical
path; its unconditional ``"state"->"st"`` global replacement is exactly
the anti-pattern instruction #8 forbids, so it is deliberately not reused.

``normalize_odds_team`` does not exist as a function anywhere in
cbb_teamScrape_v15_TEST.ipynb (checked in an earlier session too) --
included in the task's function list but not a real notebook definition.

WHAT'S GENUINELY SPORTSBOOK-SPECIFIC (kept isolated to this module, per
instruction #7 -- never merged into ``src.slug_utils``'s general tables):
``ODDS_TEAM_ALIASES``/``ODDS_ALIAS_LOOKUP`` (cells 454/458) and
`find_game_odds`'s own inline ``_FIND_GAME_ALIASES`` (cell 473) -- both
explicitly reverse-engineered from real observed odds-feed team-name
strings (per the notebook's own comments, e.g. "Explicitly seen in your
odds dump" on the MASCOTS list at cell 16).
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from src.identity.contracts import TeamIdentity, UnresolvedIdentity
from src.identity.team_identity import resolve_team_identity_by_shortening
from src.logging_config import get_logger
from src import slug_utils

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Sportsbook-specific data, ported verbatim from the notebook. Kept local to
# this module -- never merged into slug_utils' general-purpose tables.
# ---------------------------------------------------------------------------

#: Verbatim from cell 454. Canonical slug -> set of odds-feed spellings that
#: share little/no textual overlap with the canonical form (state-school
#: "st"/"state" abbreviation pairs, "&"-vs-"and" forms, regional abbreviations).
ODDS_TEAM_ALIASES: dict[str, set[str]] = {
    "st-johns-ny": {
        "st-johns", "st-johns-red-storm", "st-johns-ny", "saint-johns", "saint-johns-ny",
    },
    "northern-kentucky": {
        "northern kentucky", "northern-kentucky", "northern ky", "northern ky.", "nky", "nku",
    },
    "milwaukee": {
        "milwaukee", "wisconsin milwaukee", "wi milwaukee", "milwaukee panthers",
        "uw-milwaukee", "wisconsin-milwaukee",
    },
    "texas-am": {"texas a&m", "texas am"},
    "utah-state": {"utah st", "utah st."},
    "oklahoma-state": {"okla st", "oklahoma st"},
    "colorado-state": {"colo st", "colorado st"},
    "mississippi-state": {"miss st", "mississippi st"},
    "washington-state": {"wash st", "washington st"},
    "boise-state": {"boise st", "boise st."},
    "san-diego-state": {"san diego st", "sdsu"},
    "cal-state-fullerton": {"cs fullerton", "cal st fullerton"},
    "north-carolina-aandt": {
        "north carolina aandt", "north carolina a and t", "north-carolina-a-and-t",
        "north carolina at", "north-carolina-at", "nc aandt", "nc a and t",
    },
}

#: Verbatim from cell 458: flatten alias sets into a direct lookup.
ODDS_ALIAS_LOOKUP: dict[str, str] = {
    alias.replace(" ", "-"): canonical
    for canonical, aliases in ODDS_TEAM_ALIASES.items()
    for alias in aliases
}

#: Verbatim from `find_game_odds`'s own inline dict (cell 473). Handles odds
#: feed abbreviations that share zero tokens with the full school name.
FIND_GAME_ALIASES: dict[str, str] = {
    "long island university": "liu",
    "long-island-university": "liu",
    "long island": "liu",
    "maryland baltimore county": "umbc",
    "maryland-baltimore-county": "umbc",
    "illinois chicago": "uic",
    "illinois-chicago": "uic",
    "siu edwardsville": "siue",
    "southern illinois edwardsville": "siue",
    "massachusetts lowell": "umass-lowell",
    "texas el paso": "utep",
    "texas-el-paso": "utep",
    "texas san antonio": "utsa",
    "alabama birmingham": "uab",
    "alabama-birmingham": "uab",
    "central florida": "ucf",
    "virginia commonwealth": "vcu",
    "nevada las vegas": "unlv",
    "brigham young": "byu",
    "texas christian": "tcu",
    "southern methodist": "smu",
    "louisiana state": "lsu",
    "connecticut": "uconn",
    "pittsburgh": "pitt",
    "saint louis billikens": "saint-louis",
    "saint louis": "saint-louis",
    "michigan wolverines": "michigan",
    "michigan st spartans": "michigan-state",
    "michigan state spartans": "michigan-state",
    "michigan st": "michigan-state",
    "duke blue": "duke",
    "duke blue devils": "duke",
    "tcu horned frogs": "texas-christian",
    "houston cougars": "houston",
    "texas am aggies": "texas-am",
    "texas a&m aggies": "texas-am",
    "gonzaga bulldogs": "gonzaga",
    "gonzaga": "gonzaga",
    "illinois fighting illini": "illinois",
    "illinois fighting": "illinois",
    "iowa st cyclones": "iowa-state",
    "iowa state cyclones": "iowa-state",
    "kansas jayhawks": "kansas",
    "kansas state wildcats": "kansas-state",
    "st johns red storm": "st-johns-ny",
    "st john's red storm": "st-johns-ny",
    "auburn tigers": "auburn",
    "uconn huskies": "connecticut",
    "ucla bruins": "ucla",
    "alabama crimson tide": "alabama",
    "texas tech red raiders": "texas-tech",
    "wake forest demon deacons": "wake-forest",
    "wake forest": "wake-forest",
    "gw revolutionaries": "george-washington",
    "oklahoma st cowboys": "oklahoma-state",
    "oklahoma st": "oklahoma-state",
    "wichita st shockers": "wichita-state",
    "wichita st": "wichita-state",
    "saint josephs hawks": "saint-josephs",
    "saint joseph's hawks": "saint-josephs",
    "seattle redhawks": "seattle",
    "seattle u redhawks": "seattle",
    "utah st aggies": "utah-state",
    "utah state aggies": "utah-state",
    "utah st": "utah-state",
    "illinois st redbirds": "illinois-state",
    "illinois st": "illinois-state",
    "unc wilmington seahawks": "north-carolina-wilmington",
    "unc wilmington": "north-carolina-wilmington",
    "tulsa golden hurricane": "tulsa",
}

#: Verbatim from cell 16 (225 entries) + cell 23's `_MULTI_WORD_MASCOTS_TO_ADD`
#: (19 entries, 19 of which are genuinely new -- cell 442 dedupes on merge)
#: extracted programmatically via `ast.literal_eval` against the real
#: notebook cells, not hand-transcribed. Combined count: 228.
MASCOTS: list[str] = [
    "bulldogs", "salukis", "pioneers", "gaels", "dukes", "golden eagles", "terriers",
    "bison", "midshipmen", "greyhounds", "raiders", "bearkats", "blue hens", "hornets",
    "hawks", "golden hurricane", "pirates", "panthers", "skyhawks", "blue raiders",
    "golden panthers", "crusaders", "mountain hawks", "sycamores", "beacons", "flames",
    "green wave", "owls", "wildcats", "rattlers", "tigers", "eagles", "bears",
    "delta devils", "scarlet knights", "spartans", "hilltoppers", "purple aces",
    "jaguars", "red wolves", "jaspers", "stags", "wolverines", "thunderbirds",
    "golden lions", "braves", "gamecocks", "aggies", "miners", "bulls", "waves",
    "pilots", "matadors", "roadrunners", "lions", "redhawks", "bobcats", "jackrabbits",
    "tommies", "toreros", "anteaters", "mustangs", "tritons", "titans", "49ers",
    "highlanders", "rainbow warriors", "lancers", "hawkeyes", "terrapins", "red storm",
    "cardinals", "hoyas", "friars", "billikens", "bonnies", "flyers", "ramblers",
    "spiders", "rams", "colonials", "explorers", "falcons", "mavericks", "texans",
    "grizzlies", "lumberjacks", "penguins", "zips", "rockets", "phoenix", "sun devils",
    "buffaloes", "cyclones", "jayhawks", "longhorns", "horned frogs", "red raiders",
    "mountaineers", "utes", "cougars", "ducks", "beavers", "huskies", "trojans",
    "bruins", "badgers", "gophers", "cornhuskers", "boilermakers", "hoosiers", "illini",
    "nittany lions", "buckeyes", "seminoles", "gators", "volunteers", "razorbacks",
    "crimson tide", "rebels", "blue devils", "tar heels", "demon deacons", "cavaliers",
    "hokies", "wolfpack", "yellow jackets", "hurricanes", "golden griffins", "vikings",
    "goldhawks", "purple eagles", "red foxes", "saints", "warriors", "peacocks",
    "norse", "patriots", "commodores", "bluejays", "cardinal", "cowboys", "blue jays",
    "rainbow wahine", "antelopes", "warhawks", "sea hawks", "running eagles",
    "leathernecks", "purple knights", "colonels", "privateers", "monarchs",
    "chanticleers", "ospreys", "stetson hatters", "hatters", "racers", "knights",
    "redbirds", "pride", "seawolves", "seahawks", "great danes", "river hawks",
    "retrievers", "sharks", "red flash", "dragons", "fighting irish", "orange",
    "golden bears", "musketeers", "blue demons", "mean green", "blazers", "shockers",
    "thundering herd", "cajuns", "wolves", "vandals", "bengals", "camels",
    "fighting camels", "buccaneers", "blue hose", "paladins", "keydets", "catamounts",
    "mocs", "broncos", "chippewas", "golden flashes", "golden grizzlies", "mastodons",
    "broncs", "lobos", "aztecs", "sooners", "leopards", "quakers", "crimson",
    "big red", "big green", "minutemen", "dolphins", "royals", "demons", "islanders",
    "vaqueros", "trailblazers", "kangaroos", "coyotes", "dons", "governors", "lakers",
    "bearcats", "wolf pack", "herd", "golden hawks", "red hawks", "fighting hawks",
]

#: NOT from the notebook -- a small, additive supplement used only by
#: :func:`resolve_odds_team_identity` (the new canonical path), never by
#: :func:`find_game_odds_v15_verbatim` (which stays byte-faithful to v15,
#: gap included). Found this session: canonical_odds_team_v15's own
#: dependency, slug_utils._DISPLAY_NAME_ALIASES (= TEAM_NAME_CORRECTIONS at
#: notebook runtime), is missing "murray st" -> Murray State, even though
#: FIND_GAME_ALIASES above already covers the same "<School> St" pattern
#: for Iowa/Oklahoma/Utah/Wichita/Illinois State. Confirmed by running the
#: notebook's own cell-461 self-test values through this port: 2 of 3
#: "expected" values (Tenn-Martin, SE Missouri St) still resolve correctly
#: via the live registry; only "Murray St Racers" -> "murray-state" fails
#: today, meaning that specific self-test expectation is now stale evidence
#: of a real, if minor, gap in the live TEAM_NAME_CORRECTIONS table -- not a
#: bug in this port. Not fixed in slug_utils.py itself per this batch's
#: "stop for approval before changing established output" rule; adding it
#: here is purely additive and touches nothing else.
_SUPPLEMENTARY_ODDS_ALIASES: dict[str, str] = {
    "murray st": "murray-state",
}

MASCOT_RX = re.compile(
    r"\b(" + "|".join(re.escape(m) for m in sorted(MASCOTS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Legacy compatibility wrappers -- byte-faithful to the active v15 behavior
# ---------------------------------------------------------------------------


def canonical_odds_team_v15(name: str) -> str:
    """
    Verbatim port of notebook cell 460's ``canonical_odds_team`` -- the
    function `find_game_odds` (the real production matcher) actually calls
    internally. ``TEAM_NAME_CORRECTIONS`` here is
    ``src.slug_utils._DISPLAY_NAME_ALIASES`` (confirmed live equivalent,
    same as the Sports-Reference/DataFrame batch's finding); the notebook's
    own local ``MASCOT_RX``/``ODDS_ALIAS_LOOKUP`` are this module's module-
    level constants above.
    """
    if not name:
        return ""
    s = name.lower().strip()

    s = s.replace("’", "'").replace("‘", "'")
    s = s.replace("&", "and")

    s = s.replace("miami (oh)", "miami-oh")
    s = s.replace("miami oh", "miami-oh")
    s = s.replace("miami (fl)", "miami-fl")
    s = s.replace("miami fl", "miami-fl")

    s = re.sub(r"\(.*?\)", " ", s)

    s = s.replace(".", " ").replace("_", " ")
    s = re.sub(r"\s+", " ", s).strip()

    s = MASCOT_RX.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()

    s_lookup = s.replace("-", " ").strip()
    corrected = slug_utils._DISPLAY_NAME_ALIASES.get(s_lookup) or slug_utils._DISPLAY_NAME_ALIASES.get(s)
    if corrected:
        s = corrected.lower().strip()
        s = re.sub(r"\(.*?\)", " ", s)
        s = re.sub(r"\s+", " ", s).strip()

    s = re.sub(r"[^a-z0-9 -]", "", s)
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")

    s = ODDS_ALIAS_LOOKUP.get(s, s)

    return s


def find_game_odds_v15_verbatim(
    team_a: str,
    team_b: str,
    odds_data: list,
    *,
    fuzzy_cutoff: float = 0.75,
    partial_cutoff: float = 0.65,
) -> dict | None:
    """
    Byte-faithful port of notebook cell 473's ``find_game_odds`` -- the real
    production 3-pass matcher (exact token match, then fuzzy
    ``SequenceMatcher``, then partial token overlap), silent-best-guess
    fallback included. Preserved for callers that need exact v15 output;
    new code should use :func:`find_game_odds_via_registry` instead, which
    never guesses.
    """

    def _prep(raw: str) -> tuple[str, set]:
        stripped = MASCOT_RX.sub("", raw).strip()
        lowered = stripped.lower().replace("-", " ")
        if lowered in FIND_GAME_ALIASES:
            stripped = FIND_GAME_ALIASES[lowered]
        normed = canonical_odds_team_v15(stripped)
        tok = set(normed.replace("-", " ").split()) - {"", "st", "the", "at"}
        return normed, tok

    processed = []
    for g in odds_data:
        a_raw = g.get("away_team", "")
        h_raw = g.get("home_team", "")
        a_norm, a_tok = _prep(a_raw)
        h_norm, h_tok = _prep(h_raw)
        processed.append((g, a_norm, a_tok, h_norm, h_tok))

    qa_norm, qa_tok = _prep(team_a)
    qb_norm, qb_tok = _prep(team_b)

    for g, a_norm, a_tok, h_norm, h_tok in processed:
        if (qa_tok == a_tok and qb_tok == h_tok) or (qa_tok == h_tok and qb_tok == a_tok):
            return g

    def _sim(x, y):
        return SequenceMatcher(None, x, y).ratio()

    best_score = 0.0
    best_game = None
    for g, a_norm, a_tok, h_norm, h_tok in processed:
        for qx, qy in [(qa_norm, qb_norm), (qb_norm, qa_norm)]:
            score = (_sim(qx, a_norm) + _sim(qy, h_norm)) / 2
            if score > best_score:
                best_score = score
                best_game = g

    if best_score >= fuzzy_cutoff:
        return best_game

    def _token_overlap(q_tok: set, o_tok: set) -> float:
        if not q_tok or not o_tok:
            return 0.0
        shared = len(q_tok & o_tok)
        return shared / max(len(q_tok), len(o_tok))

    best_p3_score = 0.0
    best_p3_game = None
    for g, a_norm, a_tok, h_norm, h_tok in processed:
        for qx_tok, qy_tok in [(qa_tok, qb_tok), (qb_tok, qa_tok)]:
            ov_a = _token_overlap(qx_tok, a_tok)
            ov_b = _token_overlap(qy_tok, h_tok)
            if ov_a > 0 and ov_b > 0:
                score = (ov_a + ov_b) / 2
                if score > best_p3_score:
                    best_p3_score = score
                    best_p3_game = g

    if best_p3_score >= partial_cutoff:
        return best_p3_game

    return None


# ---------------------------------------------------------------------------
# New canonical path -- no fuzzy matching, structured ambiguous/unresolved
# ---------------------------------------------------------------------------


def resolve_odds_team_identity(
    raw_name: str, *, source: str = "odds"
) -> TeamIdentity | UnresolvedIdentity:
    """
    Resolve an odds-feed team-name string (often "<school> <mascot>" or a
    sportsbook-specific abbreviation, e.g. "Utah St Aggies", "NKU", "Texas
    A&M") to a canonical :class:`TeamIdentity`.

    Deterministic steps only, in order:
      1. Strip a known mascot via ``MASCOT_RX`` (curated, not a guess).
      2. Check ``FIND_GAME_ALIASES`` then ``ODDS_ALIAS_LOOKUP`` for a direct,
         sportsbook-specific hit (both curated from real observed odds-feed
         strings -- this is where sportsbook abbreviations like "utah st"
         live, isolated to this module, never merged into
         ``src.slug_utils``'s general tables).
      3. Fall through to the same token-shortening + canonical-registry
         resolution used by the ESPN and Covers adapters.

    No ``SequenceMatcher``/``difflib`` fuzzy step anywhere in this function
    -- that's the entire reason it exists alongside
    :func:`find_game_odds_v15_verbatim`.
    """
    if not raw_name:
        return UnresolvedIdentity(query=str(raw_name), source=source, reason="not_found")

    stripped = MASCOT_RX.sub("", raw_name).strip()
    lowered_spaced = stripped.lower().replace("-", " ")
    lowered_hyphen = stripped.lower().replace(" ", "-")

    alias_hit = (
        FIND_GAME_ALIASES.get(lowered_spaced)
        or ODDS_ALIAS_LOOKUP.get(lowered_hyphen)
        or ODDS_ALIAS_LOOKUP.get(lowered_spaced)
        or _SUPPLEMENTARY_ODDS_ALIASES.get(lowered_spaced)
    )
    if alias_hit:
        log.info("odds alias table matched %r -> %r", raw_name, alias_hit)
        result = resolve_team_identity_by_shortening(alias_hit.replace("-", " "), source=source)
        if isinstance(result, TeamIdentity):
            return result
        # Alias pointed somewhere the registry doesn't recognize -- fall
        # through to try the mascot-stripped original instead of giving up.

    return resolve_team_identity_by_shortening(stripped or raw_name, source=source)


def find_game_odds_via_registry(
    team_a: str, team_b: str, odds_data: list, *, source: str = "odds"
) -> dict | None:
    """
    Canonical replacement for ``find_game_odds``: resolves ``team_a``/
    ``team_b`` and every game's ``home_team``/``away_team`` through
    :func:`resolve_odds_team_identity`, then returns the first game whose
    resolved team_ids match (in either order). No fuzzy scoring anywhere --
    a game that can't be matched by clean identity resolution is not
    matched, not guessed at. Preserves the original ``dict | None`` return
    contract for compatibility with existing callers.
    """
    query_a = resolve_odds_team_identity(team_a, source=source)
    query_b = resolve_odds_team_identity(team_b, source=source)
    if not isinstance(query_a, TeamIdentity) or not isinstance(query_b, TeamIdentity):
        log.info(
            "find_game_odds_via_registry: could not resolve query teams %r / %r",
            team_a, team_b,
        )
        return None

    for game in odds_data:
        away = resolve_odds_team_identity(game.get("away_team", ""), source=source)
        home = resolve_odds_team_identity(game.get("home_team", ""), source=source)
        if not isinstance(away, TeamIdentity) or not isinstance(home, TeamIdentity):
            continue
        if (away.team_id == query_a.team_id and home.team_id == query_b.team_id) or (
            away.team_id == query_b.team_id and home.team_id == query_a.team_id
        ):
            return game

    log.info("find_game_odds_via_registry: no game in odds_data matched %r vs %r", team_a, team_b)
    return None
