"""
NET-ranking team-identity adapter (Phase 2A, adapter batch 5).

ACTIVE V15 DEFINITIONS AND CALL SITES

`build_net_rankings_from_cbs = build_net_rankings_from_ncaa` (end of cell
386) -- a live backward-compat alias, the exact same pattern found for
`MANUAL_SLUG_OVERRIDES`/`TEAM_NAME_CORRECTIONS` two batches ago. The
function used to scrape CBS Sports (hence `clean_cbs_net_team`,
`CBS_NET_ALIASES_TO_SR`, `resolve_net_team_sr_slug`'s docstring literally
saying "Resolve CBS NET team string...", and comments like "Far fewer
entries needed vs CBS since NCAA uses full names") and was migrated to
scrape NCAA.com instead, with the old name kept for existing callers.

Unshadowed, real definitions (all `defined_in_notebook_count == 1`):
`attach_net_rank` (cell 345), `ensure_sr_slug_column` (cell 375),
`build_sr_to_team_slug_crosswalk` (cell 376), `resolve_net_team_sr_slug`
(cell 383), `build_net_rankings_from_ncaa` (cell 386). Real call sites for
`attach_net_rank(..., slug_overrides=MANUAL_SLUG_OVERRIDES)`: cells
329-330, 474-475 (both attach NET rank to team profiles right after
`resolve_team_profile()`, before matchup feature building).

KEY-SPACE FINDING (objective #3)

Four distinct namespaces are in play, and `build_net_rankings_from_ncaa`'s
own machinery exists specifically to bridge them:

  - **CBS/NCAA display name**: raw scraped text, e.g. "Miami FL 24-8" or
    "St. John's (NY)" -- needs record-stripping and slugification before
    it's comparable to anything.
  - **Sports-Reference slug**: `all_schools_df["team_slug"]` from
    `fetch_all_schools()` (verified ground truth per the Sports-Reference/
    DataFrame batch) -- sometimes abbreviated (`tcu`, `ucf`, `uab`),
    sometimes spelled out, per that batch's 31-mismatch finding.
  - **Pipeline `team_slug`**: whatever the rest of the model/profile code
    uses as its own join key. When `build_net_rankings_from_ncaa` is
    called with `all_schools_df` (the DataFrame mode), this pipeline slug
    IS `all_schools_df["team_slug"]` too -- `ensure_sr_slug_column` just
    copies that same column into a second `sr_slug` column (applying
    `TEAM_SLUG_TO_SR_PATCH`), and `build_sr_to_team_slug_crosswalk` maps
    it back to itself. **In this mode there is no genuinely separate
    pipeline-slug namespace** -- it's the same data, re-derived through
    two columns for join-code readability, not because SR-slug and
    pipeline-slug actually differ here.
  - **Canonical `team_id`**: `src.slug_utils.TEAM_REGISTRY`, independent
    of all three of the above.

The real, cached `Reference Py Files/net_rankings_2026.csv` fixture (365
rows, `team_slug,net_rank`) confirms the SR-slug column really is
heterogeneous in practice: `southern-methodist` (spelled out, matches this
codebase's registry convention) sits next to `tcu`/`ucf`/`uab`
(abbreviated, matching real SR convention) in the same file -- exactly the
mix the Sports-Reference/DataFrame batch already found and chose not to
"fix" for the same reason it isn't fixed here: doing so would change
established output for other callers.

Not corrected in this batch either (kept as a known Phase 3 data-quality
item, per instruction): `fetch_all_schools()`'s repeated-header-row bug
found in the previous batch. This adapter's data flows never touch that
function directly, so it is not exercised here, just re-flagged.

CBS-ERA HARDCODED DECISION, PRESERVED BUT NOT ADOPTED

`CBS_NET_ALIASES_TO_SR` hardcodes bare `"miami"` -> `"miami-fl"`, with an
explicit author comment: *"Ambiguous 'Miami' on CBS is almost always
Miami (FL) in NET context. If you want to be stricter, you can remove this
and rely on slug_index membership."* This is a deliberate legacy business
call, not an oversight. It is preserved verbatim in
:func:`resolve_net_team_sr_slug_v15` (the compatibility wrapper) but **not**
carried into :func:`resolve_net_team_identity` (the canonical path), which
stays consistent with every other adapter in this migration: a bare
"Miami" is `ambiguous`, never silently resolved.
"""
from __future__ import annotations

import difflib
import io
import re
from typing import Callable, Union

import pandas as pd

from src.exceptions import ValidationError
from src.identity.contracts import TeamIdentity, UnresolvedIdentity
from src.identity.sr_slug_reconciliation import resolve_team_identity_with_reconciliation
from src.identity.team_identity import resolve_team_identity
from src.logging_config import get_logger
from src.scraper import TEAM_SLUG_TO_SR_PATCH

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# NET-specific data, ported verbatim (ast.literal_eval-extracted from the
# real notebook cells, not hand-transcribed). Kept local to this module.
# ---------------------------------------------------------------------------

#: Cell 345. Pipeline team_slug -> list of NET-rankings-index slug variants
#: to try. ("Whatever scrape_net_rankings stored" per the notebook comment.)
NET_SLUG_BRIDGE: dict[str, list[str]] = {
    "north-carolina-wilmington": ["unc-wilmington", "uncw", "north-carolina-wilmington"],
    "north-carolina-greensboro": ["unc-greensboro", "uncg", "north-carolina-greensboro"],
    "north-carolina-asheville": ["unc-asheville", "unc-ash", "north-carolina-asheville"],
    "texas-el-paso": ["utep", "texas-el-paso"],
    "texas-san-antonio": ["utsa", "texas-san-antonio"],
    "california-irvine": ["uc-irvine", "california-irvine"],
    "california-santa-barbara": ["ucsb", "california-santa-barbara"],
    "california-davis": ["uc-davis", "california-davis"],
    "california-riverside": ["uc-riverside", "california-riverside"],
    "california-san-diego": ["uc-san-diego", "california-san-diego"],
    "southern-california": ["usc", "southern-california"],
    "southern-mississippi": ["so-miss", "southern-miss", "southern-mississippi"],
    "nebraska-omaha": ["omaha", "nebraska-omaha"],
    "missouri-kansas-city": ["kansas-city", "missouri-kansas-city"],
    "illinois-chicago": ["uic", "illinois-chicago"],
    "virginia-commonwealth": ["vcu", "virginia-commonwealth"],
    "connecticut": ["uconn", "connecticut"],
    "nevada-las-vegas": ["unlv", "nevada-las-vegas"],
    "brigham-young": ["byu", "brigham-young"],
    "louisiana-state": ["lsu", "louisiana-state"],
    "texas-christian": ["tcu", "texas-christian"],
    "central-florida": ["ucf", "central-florida"],
    "alabama-birmingham": ["uab", "alabama-birmingham"],
    "florida-atlantic": ["fau", "florida-atlantic"],
    "florida-international": ["fiu", "florida-international"],
    "florida-gulf-coast": ["fgcu", "florida-gulf-coast"],
    "east-tennessee-state": ["etsu", "east-tennessee-state"],
    "loyola-marymount": ["lmu", "loyola-marymount"],
    "fairleigh-dickinson": ["fdu", "fairleigh-dickinson"],
    "maryland-baltimore-county": ["umbc", "maryland-baltimore-county"],
    "sam-houston-state": ["sam-houston", "sam-houston-state"],
    "arkansas-little-rock": ["little-rock", "arkansas-little-rock"],
    "college-of-charleston": ["charleston", "college-of-charleston"],
    "ipfw": ["purdue-fort-wayne", "pfw", "ipfw"],
    "iupui": ["iui", "iu-indianapolis", "iupui"],
    "southeast-missouri-state": ["semo", "southeast-missouri-state"],
    "southern-illinois-edwardsville": ["siue", "siu-edwardsville"],
    "mississippi-valley-state": ["mvsu", "mississippi-valley-state"],
    "north-carolina-central": ["nccu", "north-carolina-central"],
    "north-carolina-at": ["ncat", "north-carolina-at"],
    "cal-state-bakersfield": ["csub", "cal-state-bakersfield"],
    "cal-state-northridge": ["csun", "cal-state-northridge"],
    "cal-state-fullerton": ["csuf", "cal-state-fullerton"],
    "san-diego-state": ["sdsu", "san-diego-state"],
    "north-dakota-state": ["ndsu", "north-dakota-state"],
    "oral-roberts": ["oru", "oral-roberts"],
}

#: Cell 371. CBS-era abbreviation -> spelled-out slug.
NET_ABBREVIATIONS: dict[str, str] = {
    "uconn": "connecticut", "ucla": "california-los-angeles", "usc": "southern-california",
    "byu": "brigham-young", "lsu": "louisiana-state", "ucf": "central-florida",
    "smu": "southern-methodist", "tcu": "texas-christian", "uab": "alabama-birmingham",
    "ole-miss": "mississippi", "vcu": "virginia-commonwealth", "uri": "rhode-island",
    "slu": "saint-louis", "sju": "saint-johns", "isu": "iowa-state", "ksu": "kansas-state",
    "msu": "michigan-state", "unc": "north-carolina", "ecu": "east-carolina",
    "okla-st": "oklahoma-state", "miss-st": "mississippi-state", "colo-st": "colorado-state",
    "wash-st": "washington-state", "app-st": "appalachian-state", "clev-st": "cleveland-state",
    "miss-valley-st": "mississippi-valley-state", "uc-irvine": "california-irvine",
    "ucsb": "california-santa-barbara", "uc-davis": "california-davis",
    "uc-riverside": "california-riverside", "ut-rio-grande-valley": "texas-rio-grande-valley",
    "tx-aandm-cc": "texas-am-corpus-christi", "liu": "long-island-university",
    "fdu": "fairleigh-dickinson", "ccsu": "central-connecticut-state",
    "siue": "southern-illinois-edwardsville", "umbc": "maryland-baltimore-county",
    "fau": "florida-atlantic", "fgcu": "florida-gulf-coast", "etsu": "east-tennessee-state",
    "uncg": "north-carolina-greensboro", "fiu": "florida-international",
    "lmu": "loyola-marymount", "penn": "pennsylvania", "boston-u": "boston-university",
    "kansas-city": "missouri-kansas-city", "st-bona": "st-bonaventure",
}

#: Cell 371.
DIRECTIONAL_EXPANSIONS: dict[str, str] = {
    "n": "north", "s": "south", "e": "east", "w": "west", "se": "southeastern",
    "sw": "southwestern", "ne": "northeastern", "nw": "northwestern", "va": "virginia",
    "ga": "georgia", "la": "louisiana", "tx": "texas", "nc": "north carolina", "sc": "south carolina",
}

#: Cell 371.
CANONICAL_OVERRIDES: dict[str, str] = {
    "mcneese": "mcneese-state", "sf-austin": "stephen-f-austin", "charleston": "college-of-charleston",
    "omaha": "nebraska-omaha", "detroit": "detroit-mercy", "nicholls": "nicholls-state",
    "vmi": "virginia-military-institute", "cal-baker": "cal-state-bakersfield",
    "ut-rio-grande-valley": "texas-rio-grande-valley", "texas-aandm-cc": "texas-am-corpus-christi",
    "long-island": "long-island-university", "nj-tech": "new-jersey-institute-of-technology",
    "umass-lowell": "massachusetts-lowell", "jax-state": "jacksonville-state",
    "little-rock": "arkansas-little-rock", "utah-tech": "dixie-state",
    "md-east-shore": "maryland-eastern-shore", "liu": "long-island-university",
}

#: Cell 382. CBS-era alias -> SR slug. Includes the hardcoded Miami
#: disambiguation described in the module docstring -- preserved verbatim,
#: not adopted by the canonical path.
CBS_NET_ALIASES_TO_SR: dict[str, str] = {
    "miami": "miami-fl",
    "tcu": "texas-christian", "ucf": "central-florida", "uab": "alabama-birmingham",
    "uic": "illinois-chicago", "george-wash": "george-washington",
    "george-washington": "george-washington", "uncw": "north-carolina-wilmington",
    "uncg": "north-carolina-greensboro", "unc-ash": "north-carolina-asheville",
    "uc-irvine": "california-irvine", "uc-riverside": "california-riverside",
    "ucsb": "california-santa-barbara", "utep": "texas-el-paso", "iui": "iupui",
    "kansas-city": "missouri-kansas-city", "san-fran": "san-francisco",
    "new-hamp": "new-hampshire", "ualbany": "albany-ny", "cal-baptist": "california-baptist",
    "csnorthridge": "cal-state-northridge", "cs-fullerton": "cal-state-fullerton",
    "n-mex-st": "new-mexico-state", "c-carolina": "coastal-carolina",
    "abilene-chr": "abilene-christian", "houston-chr": "houston-christian",
    "tenn-tech": "tennessee-tech", "texas-so": "texas-southern",
}

#: Cell 386, base dict + its own `.update()` call merged (274 unique keys,
#: extracted via ast.literal_eval against both, not hand-merged). NCAA
#: display-name slug -> pipeline/SR slug. Comment worth preserving: one key,
#: `'sfа'` (`'sfа'`), contains a Cyrillic 'а' (U+0430) instead of ASCII
#: 'a' -- an apparent copy-paste artifact in the original notebook data,
#: making that specific key permanently unreachable by any real ASCII
#: "sfa" input. Kept verbatim rather than silently corrected, since this is
#: compatibility data, not new code.
NCAA_NET_TO_TEAM_SLUG_FALLBACK: dict[str, str] = {
    "unc-wilmington": "unc-wilmington", "uc-santa-barbara": "california-santa-barbara",
    "unc-asheville": "north-carolina-asheville", "unc-greensboro": "north-carolina-greensboro",
    "iu-indianapolis": "iu-indianapolis", "iu-indy": "iu-indianapolis",
    "florida-intl": "florida-intl", "fla-atlantic": "florida-atlantic",
    "florida-atlantic": "florida-atlantic", "virginia-commonwealth": "virginia-commonwealth",
    "central-florida": "central-florida", "illinois-chicago": "illinois-chicago",
    "siu-edwardsville": "siu-edwardsville", "loyola-marymount": "loyola-marymount",
    "lmu-ca": "loyola-marymount", "brigham-young": "brigham-young",
    "louisiana-state": "louisiana-state", "purdue-fort-wayne": "purdue-fort-wayne",
    "virginia-military-institute": "virginia-military-institute",
    "east-tennessee-state": "east-tennessee-state", "chattanooga": "chattanooga",
    "southeast-missouri-state": "southeast-missouri-state", "southeast-mo-st": "southeast-missouri-state",
    "sam-houston-state": "sam-houston-state", "sam-houston": "sam-houston-state",
    "ut-arlington": "texas-arlington", "ut-rio-grande-valley": "ut-rio-grande-valley",
    "incarnate-word": "incarnate-word", "maryland-eastern-shore": "maryland-eastern-shore",
    "north-carolina-central": "north-carolina-central", "nc-central": "north-carolina-central",
    "north-carolina-at": "north-carolina-at", "nc-at": "north-carolina-at",
    "cal-state-bakersfield": "cal-state-bakersfield", "csu-bakersfield": "cal-state-bakersfield",
    "cal-state-northridge": "cal-state-northridge", "cal-state-fullerton": "cal-state-fullerton",
    "cal-st-fullerton": "cal-state-fullerton", "uc-san-diego": "california-san-diego",
    "uc-riverside": "california-riverside", "uc-irvine": "california-irvine",
    "san-diego-state": "san-diego-state", "oral-roberts": "oral-roberts",
    "north-dakota-state": "north-dakota-state", "nebraska-omaha": "nebraska-omaha",
    "omaha": "nebraska-omaha", "missouri-kansas-city": "missouri-kansas-city",
    "kansas-city": "missouri-kansas-city", "grand-canyon": "grand-canyon",
    "utah-valley": "utah-valley", "southern-utah": "southern-utah",
    "northern-arizona": "northern-arizona", "western-carolina": "western-carolina",
    "eastern-illinois": "eastern-illinois", "western-illinois": "western-illinois",
    "eastern-kentucky": "eastern-kentucky", "north-alabama": "north-alabama",
    "southern-indiana": "southern-indiana", "abilene-christian": "abilene-christian",
    "california-baptist": "california-baptist", "high-point": "high-point",
    "mississippi-valley-state": "mississippi-valley-state", "mississippi-val": "mississippi-valley-state",
    "arkansas-pine-bluff": "arkansas-pine-bluff", "ark-pine-bluff": "arkansas-pine-bluff",
    "bethune-cookman": "bethune-cookman", "col-of-charleston": "college-of-charleston",
    "college-of-charleston": "college-of-charleston", "saint-mary-s-ca": "saint-marys-ca",
    "saint-mary-s": "saint-marys-ca", "st-john-s-ny": "st-johns-ny", "st-john-s": "st-johns-ny",
    "miami-fl": "miami-fl", "miami-oh": "miami-oh", "app-state": "appalachian-state",
    "appalachian-state": "appalachian-state", "ga-southern": "georgia-southern",
    "georgia-southern": "georgia-southern", "a-m-corpus-christi": "texas-am-corpus-christi",
    "texas-am-corpus-christi": "texas-am-corpus-christi", "east-texas-a-m": "east-texas-am",
    "fla-am": "florida-am", "florida-am": "florida-am", "florida-a-m": "florida-am",
    "sfа": "stephen-f-austin", "sfa": "stephen-f-austin", "stephen-f-austin": "stephen-f-austin",
    "mcneese": "mcneese-state", "mcneese-state": "mcneese-state", "liu": "liu",
    "north-dakota": "north-dakota", "south-dakota": "south-dakota", "unlv": "nevada-las-vegas",
    "uab": "alabama-birmingham", "utep": "texas-el-paso", "utsa": "texas-san-antonio",
    "umbc": "maryland-baltimore-county", "njit": "njit", "vcu": "virginia-commonwealth",
    "vmi": "virginia-military-institute", "etsu": "east-tennessee-state",
    "semo": "southeast-missouri-state", "shsu": "sam-houston-state", "fiu": "florida-intl",
    "fau": "florida-atlantic", "uta": "ut-arlington", "utrgv": "texas-rio-grande-valley",
    "uiw": "incarnate-word", "umes": "maryland-eastern-shore", "famu": "florida-am",
    "nccu": "north-carolina-central", "ncat": "north-carolina-at", "sdsu": "san-diego-state",
    "ndsu": "north-dakota-state", "uno": "nebraska-omaha", "umkc": "missouri-kansas-city",
    "gcu": "grand-canyon", "uvu": "utah-valley", "suu": "southern-utah", "nau": "northern-arizona",
    "wcu": "western-carolina", "eiu": "eastern-illinois", "wiu": "western-illinois",
    "eku": "eastern-kentucky", "una": "north-alabama", "usi": "southern-indiana",
    "acu": "abilene-christian", "cbu": "california-baptist", "hpu": "high-point",
    "mvsu": "mississippi-valley-state", "uapb": "arkansas-pine-bluff", "b-cu": "bethune-cookman",
    "pfw": "purdue-fort-wayne", "ucf": "central-florida", "uic": "illinois-chicago",
    "siue": "siu-edwardsville", "siu": "southern-illinois", "lmu": "loyola-marymount",
    "tcu": "texas-christian", "smu": "southern-methodist", "byu": "brigham-young",
    "lsu": "louisiana-state", "unc": "north-carolina", "ucsd": "uc-san-diego",
    "ucr": "uc-riverside", "uci": "uc-irvine", "oru": "oral-roberts", "und": "north-dakota",
    "usd": "south-dakota", "uncw": "unc-wilmington", "ucsb": "uc-santa-barbara",
    "uncg": "unc-greensboro", "iui": "iu-indianapolis", "iupui": "iu-indianapolis",
    "uni": "northern-iowa", "western-ky": "western-kentucky", "csun": "cal-state-northridge",
    "northern-ky": "northern-kentucky", "queens-nc": "queens", "lamar-university": "lamar",
    "central-conn-st": "central-connecticut-state", "eastern-ky": "eastern-kentucky",
    "usc-upstate": "south-carolina-upstate", "loyola-chicago": "loyola-il",
    "loyola-maryland": "loyola-md", "niu": "northern-illinois", "army-west-point": "army",
    "alcorn": "alcorn-state", "ulm": "louisiana-monroe", "southern-miss": "southern-mississippi",
    "southern-miss-": "southern-mississippi", "a-and-m-corpus-christi": "texas-am-corpus-christi",
    "alabama-a-and-m": "alabama-am", "alabama-st": "alabama-state", "arizona-st": "arizona-state",
    "arkansas-st": "arkansas-state", "ball-st": "ball-state", "boise-st": "boise-state",
    "boston-u": "boston-university", "cal-poly": "cal-poly-slo", "central-ark": "central-arkansas",
    "central-mich": "central-michigan", "charleston-so": "charleston-southern",
    "chicago-st": "chicago-state", "cleveland-st": "cleveland-state",
    "colorado-st": "colorado-state", "coppin-st": "coppin-state",
    "delaware-st": "delaware-state", "east-texas-a-and-m": "east-texas-am",
    "eastern-ill": "eastern-illinois", "eastern-mich": "eastern-michigan",
    "eastern-wash": "eastern-washington", "fdu": "fairleigh-dickinson",
    "fgcu": "florida-gulf-coast", "florida-a-and-m": "florida-am", "florida-st": "florida-state",
    "fresno-st": "fresno-state", "georgia-st": "georgia-state", "grambling": "grambling-state",
    "green-bay": "wisconsin-green-bay", "houston-christian": "houston-baptist",
    "idaho-st": "idaho-state", "illinois-st": "illinois-state", "indiana-st": "indiana-state",
    "iowa-st": "iowa-state", "jackson-st": "jackson-state", "jacksonville-st": "jacksonville-state",
    "kansas-st": "kansas-state", "kennesaw-st": "kennesaw-state", "kent-st": "kent-state",
    "little-rock": "arkansas-little-rock", "long-beach-st": "long-beach-state",
    "louisiana": "louisiana-lafayette", "michigan-st": "michigan-state",
    "middle-tenn": "middle-tennessee", "milwaukee": "wisconsin-milwaukee",
    "mississippi-st": "mississippi-state", "missouri-st": "missouri-state",
    "montana-st": "montana-state", "morehead-st": "morehead-state", "morgan-st": "morgan-state",
    "murray-st": "murray-state", "n-c-a-and-t": "north-carolina-at",
    "n-c-central": "north-carolina-central", "nc-state": "north-carolina-state",
    "new-mexico-st": "new-mexico-state", "nicholls": "nicholls-state",
    "norfolk-st": "norfolk-state", "north-ala": "north-alabama",
    "north-dakota-st": "north-dakota-state", "northern-ariz": "northern-arizona",
    "northern-colo": "northern-colorado", "northwestern-st": "northwestern-state",
    "ohio-st": "ohio-state", "oklahoma-st": "oklahoma-state", "ole-miss": "mississippi",
    "oregon-st": "oregon-state", "penn": "pennsylvania", "penn-st": "penn-state",
    "portland-st": "portland-state", "prairie-view": "prairie-view-am",
    "sacramento-st": "sacramento-state", "saint-francis": "saint-francis-pa",
    "san-diego-st": "san-diego-state", "san-jose-st": "san-jose-state", "seattle-u": "seattle",
    "south-carolina-st": "south-carolina-state", "south-dakota-st": "south-dakota-state",
    "south-fla": "south-florida", "southeastern-la": "southeastern-louisiana",
    "southern-ill": "southern-illinois", "southern-ind": "southern-indiana",
    "southern-u": "southern", "tarleton-st": "tarleton-state", "tennessee-st": "tennessee-state",
    "texas-a-and-m": "texas-am", "texas-st": "texas-state", "the-citadel": "citadel",
    "ualbany": "albany-ny", "uc-davis": "california-davis", "uconn": "connecticut",
    "umass-lowell": "massachusetts-lowell", "ut-martin": "tennessee-martin",
    "utah-st": "utah-state", "washington-st": "washington-state", "weber-st": "weber-state",
    "west-ga": "west-georgia", "western-caro": "western-carolina",
    "western-ill": "western-illinois", "western-mich": "western-michigan",
    "wichita-st": "wichita-state", "william-and-mary": "william-mary",
    "wright-st": "wright-state", "youngstown-st": "youngstown-state",
}


# ---------------------------------------------------------------------------
# Legacy compatibility wrappers -- byte-faithful to v15
# ---------------------------------------------------------------------------


def clean_cbs_net_team_v15(raw: str) -> str:
    """Verbatim port of cell 378's `clean_cbs_net_team`."""
    s = str(raw or "").strip()
    s = re.sub(r"\s+\d{1,2}\s*-\s*\d{1,2}.*$", "", s).strip()
    s = s.lower()
    s = s.replace(" ", " ")
    s = s.replace("&", "and")
    s = s.replace(".", "")
    s = s.replace("'", "")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def resolve_net_team_sr_slug_v15(
    raw: str, sr_slug_index: set, *, log_unresolved: bool = False
) -> str | None:
    """
    Verbatim port of cell 383's `resolve_net_team_sr_slug` (the CBS-era
    fallback-of-a-fallback `build_net_rankings_from_ncaa` still calls for
    any team its primary `NCAA_NET_TO_TEAM_SLUG_FALLBACK` table misses).
    Includes the hardcoded bare-"Miami"->"miami-fl" decision (via
    `CBS_NET_ALIASES_TO_SR`) and the original difflib fuzzy fallback
    (cutoff=0.80) -- preserved for exact parity, not used by the new
    canonical path.
    """
    if not raw:
        return None

    s = clean_cbs_net_team_v15(raw)
    slug = re.sub(r"[^a-z0-9]+", "-", s).strip("-")

    if slug in CBS_NET_ALIASES_TO_SR:
        alias = CBS_NET_ALIASES_TO_SR[slug]
        if alias in sr_slug_index:
            return alias
        slug = alias

    slug = NET_ABBREVIATIONS.get(slug, slug)

    tokens = slug.split("-")
    tokens = [DIRECTIONAL_EXPANSIONS.get(t, t) for t in tokens]
    slug = "-".join(tokens)

    if slug in CANONICAL_OVERRIDES and slug not in sr_slug_index:
        slug = CANONICAL_OVERRIDES[slug]

    for v in (slug, slug.replace("st-", "saint-"), slug.replace("saint-", "st-")):
        if v in sr_slug_index:
            return v

    if slug in sr_slug_index:
        return slug

    matches = difflib.get_close_matches(slug, sr_slug_index, n=1, cutoff=0.80)
    if matches:
        return matches[0]

    if log_unresolved:
        log.info("resolve_net_team_sr_slug_v15: unresolved %r -> tried slug %r", raw, slug)
    return None


def _ncaa_row_slug_v15(raw_team: str) -> str:
    """Verbatim port of cell 386's `_ncaa_row_slug`."""
    s = raw_team.lower().strip()
    s = re.sub(r"\s+\d{1,2}\s*-\s*\d{1,2}.*$", "", s).strip()
    s = s.replace("&", " and ").replace(".", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def ensure_sr_slug_column_v15(all_schools_df: pd.DataFrame) -> pd.DataFrame:
    """
    Verbatim port of cell 375's `ensure_sr_slug_column`. As documented
    above: given `all_schools_df` from `fetch_all_schools()`, this just
    copies `team_slug` into a new `sr_slug` column and applies
    `TEAM_SLUG_TO_SR_PATCH` (from `src.scraper`) -- it is not deriving a
    genuinely independent value in the common case.
    """
    if "team_slug" not in all_schools_df.columns:
        raise KeyError(f"Expected 'team_slug' in all_schools_df. Columns: {all_schools_df.columns.tolist()}")

    df = all_schools_df.copy()
    df["sr_slug"] = df["team_slug"].dropna().astype(str).str.strip().str.lower()
    df["sr_slug"] = df["sr_slug"].replace(TEAM_SLUG_TO_SR_PATCH)
    df["sr_slug"] = df["sr_slug"].astype(str).str.strip().str.lower()
    return df


def build_sr_to_team_slug_crosswalk_v15(all_schools_df: pd.DataFrame) -> dict[str, str]:
    """Verbatim port of cell 376's `build_sr_to_team_slug_crosswalk`."""
    if "sr_slug" not in all_schools_df.columns or "team_slug" not in all_schools_df.columns:
        raise KeyError(
            "Need both 'sr_slug' and 'team_slug' in all_schools_df. "
            f"Columns: {all_schools_df.columns.tolist()}"
        )

    tmp = all_schools_df[["sr_slug", "team_slug"]].dropna().astype(str)
    tmp["sr_slug"] = tmp["sr_slug"].str.strip()
    tmp["team_slug"] = tmp["team_slug"].str.strip()

    crosswalk: dict[str, str] = {}
    collisions = 0
    for sr, ts in zip(tmp["sr_slug"], tmp["team_slug"]):
        if sr in crosswalk and crosswalk[sr] != ts:
            collisions += 1
            continue
        crosswalk[sr] = ts

    if collisions:
        log.info("build_sr_to_team_slug_crosswalk_v15: %d SR->team_slug collisions (kept first)", collisions)

    return crosswalk


def build_net_rankings_from_ncaa_v15(
    url: str,
    schools_or_slugs: Union[pd.DataFrame, list, None] = None,
    *,
    fetch_fn: Callable[[str], pd.DataFrame] | None = None,
) -> pd.DataFrame:
    """
    Compatibility wrapper: notebook cell 386's `build_net_rankings_from_ncaa`
    (== `build_net_rankings_from_cbs`, the live backward-compat alias),
    preserving all three input modes and their exact resolution order
    (`NCAA_NET_TO_TEAM_SLUG_FALLBACK` first, then
    `resolve_net_team_sr_slug_v15`'s CBS-era fallback chain).

    ``fetch_fn`` is the one addition versus the original: an injectable
    replacement for the notebook's `_fetch_ncaa_net_table` (which always
    hits NCAA.com live, with no cache path at all -- there was nothing to
    preserve there for offline testing). Must return a DataFrame with
    ``Rank``/``Team`` columns, same contract as the original private
    helper. Omitting it means "no fetch source available" -- this wrapper
    does not perform a live request itself.
    """
    if fetch_fn is None:
        raise ValidationError(
            "build_net_rankings_from_ncaa_v15 requires fetch_fn (no live NCAA.com "
            "request is performed by this port -- inject a function returning a "
            "Rank/Team DataFrame, e.g. from a cached fixture)."
        )

    if schools_or_slugs is None:
        df = fetch_fn(url)
        if "Team" not in df.columns or "Rank" not in df.columns:
            raise KeyError(f"Unexpected NCAA NET table schema: {df.columns.tolist()}")
        result_df = df[["Rank", "Team"]].copy()
        result_df["team_slug"] = result_df["Team"].apply(_ncaa_row_slug_v15)
        result_df = result_df.rename(columns={"Rank": "net_rank"})
        return result_df[["team_slug", "net_rank"]].sort_values("net_rank").reset_index(drop=True)

    team_slug_universe: set | None = None
    to_pipeline_key: Callable[[str], str | None]

    if isinstance(schools_or_slugs, pd.DataFrame):
        all_schools_df = ensure_sr_slug_column_v15(schools_or_slugs)
        sr_to_team = build_sr_to_team_slug_crosswalk_v15(all_schools_df)
        sr_slug_index = set(sr_to_team.keys())
        team_slug_universe = set(all_schools_df["team_slug"].dropna().astype(str).str.strip().tolist())

        def to_pipeline_key(sr_slug: str) -> str | None:
            ts = sr_to_team.get(sr_slug)
            return ts if ts and ts in team_slug_universe else None

    elif isinstance(schools_or_slugs, list):
        canonical_set = set(map(str, schools_or_slugs))
        sr_slug_index = canonical_set

        def to_pipeline_key(sr_slug: str) -> str | None:
            return sr_slug if sr_slug in canonical_set else None

    else:
        raise TypeError(
            f"build_net_rankings_from_ncaa_v15 expects pd.DataFrame or list, got: {type(schools_or_slugs)}"
        )

    df = fetch_fn(url)
    if "Team" not in df.columns or "Rank" not in df.columns:
        raise KeyError(f"Unexpected NCAA NET table schema: {df.columns.tolist()}")

    records: list[dict] = []
    resolved = skipped = 0

    for _, row in df.iterrows():
        raw_team = str(row["Team"]).strip()
        try:
            net_rank = int(row["Rank"])
        except Exception:
            skipped += 1
            continue

        if team_slug_universe is not None:
            ncaa_slug = _ncaa_row_slug_v15(raw_team)
            sr_slug_override = NCAA_NET_TO_TEAM_SLUG_FALLBACK.get(ncaa_slug)
            if sr_slug_override:
                pipeline_key = to_pipeline_key(sr_slug_override)
                key = pipeline_key or sr_slug_override
                records.append({"team_slug": key, "net_rank": net_rank})
                resolved += 1
                continue

        sr_slug = resolve_net_team_sr_slug_v15(raw_team, sr_slug_index)
        if not sr_slug:
            skipped += 1
            continue

        key = to_pipeline_key(sr_slug)
        if not key:
            skipped += 1
            continue

        records.append({"team_slug": key, "net_rank": net_rank})
        resolved += 1

    log.info("build_net_rankings_from_ncaa_v15: resolved=%d skipped=%d", resolved, skipped)
    result_df = pd.DataFrame(records, columns=["team_slug", "net_rank"])
    return result_df.sort_values("net_rank").reset_index(drop=True)


def attach_net_rank_v15(profile: dict, net_rankings, *, slug_overrides: dict | None = None) -> dict:
    """
    Verbatim port of cell 345's `attach_net_rank`, including its 6-tier
    fallback chain (direct / bridge / slug_overrides / saint-st swap /
    fuzzy at cutoff=0.82 / give up) and its exact ``net_rating`` derivation.
    ``slug_overrides`` is meaningful, not redundant -- confirmed this
    session: `MANUAL_SLUG_OVERRIDES` (the caller's usual argument) is a
    live alias of `slug_utils._SR_OVERRIDES` (found two batches ago), so
    tier 3 genuinely leverages the canonical registry's own override table
    as one more slug-space bridge, not a dead/no-op parameter.
    """
    slug = profile.get("team_slug")
    if not slug:
        return profile

    def _rank_to_net(rank: int) -> float:
        return round(15.0 - (rank - 1) * 0.115, 2)

    def _apply_net_rating(p: dict, r: int) -> dict:
        p["net_rank"] = r
        cur = float(p.get("net_rating", 0.0) or 0.0)
        if abs(cur) > 15.0 or not p.get("_net_from_rank"):
            p["net_rating"] = _rank_to_net(r)
            p["_net_from_rank"] = True
        return p

    def _lookup(s):
        if isinstance(net_rankings, pd.DataFrame):
            match = net_rankings.loc[net_rankings["team_slug"] == s, "net_rank"]
            return int(match.iloc[0]) if not match.empty else None
        v = net_rankings.get(s)
        return int(v) if v is not None else None

    rank = _lookup(slug)
    if rank is not None:
        return _apply_net_rating(profile, rank)

    for variant in NET_SLUG_BRIDGE.get(slug, []):
        rank = _lookup(variant)
        if rank is not None:
            return _apply_net_rating(profile, rank)

    if slug_overrides:
        sr_slug = slug_overrides.get(profile.get("team", ""), slug_overrides.get(slug, None))
        if sr_slug:
            rank = _lookup(sr_slug)
            if rank is not None:
                return _apply_net_rating(profile, rank)

    if slug.startswith("st-"):
        alt = "saint-" + slug[3:]
    elif slug.startswith("saint-"):
        alt = "st-" + slug[6:]
    else:
        alt = None
    if alt:
        rank = _lookup(alt)
        if rank is not None:
            return _apply_net_rating(profile, rank)

    if isinstance(net_rankings, pd.DataFrame):
        all_slugs = net_rankings["team_slug"].tolist()
    else:
        all_slugs = list(net_rankings.keys())
    matches = difflib.get_close_matches(slug, all_slugs, n=1, cutoff=0.82)
    if matches:
        rank = _lookup(matches[0])
        if rank is not None:
            return _apply_net_rating(profile, rank)

    profile["net_rank"] = None
    return profile


# ---------------------------------------------------------------------------
# New canonical path -- no fuzzy matching, structured ambiguous/unresolved
# ---------------------------------------------------------------------------


#: Reverse index of NCAA_NET_TO_TEAM_SLUG_FALLBACK: pipeline/SR slug value
#: -> every NCAA-raw key that maps to it. Built once, lazily.
#:
#: Why this exists: a cached rankings file/DataFrame (like
#: `Reference Py Files/net_rankings_2026.csv`) already stores the
#: *pipeline* slug ("wisconsin-green-bay"), not the raw NCAA display-name
#: slug ("green-bay") the forward fallback dict is keyed by. Looking up
#: "wisconsin-green-bay" as a *key* naturally misses, and the canonical
#: registry knows this program as bare "Green Bay", not "Wisconsin-Green
#: Bay". The fix: recognize "wisconsin-green-bay" as a *value* in the
#: fallback dict, and retry resolution using its NCAA-raw sibling key(s)
#: ("green-bay"), which the registry resolves directly. Confirmed this
#: recovers "wisconsin-green-bay", "wisconsin-milwaukee", "grambling-state"
#: against the real 365-row fixture (see docs/migration_state.md). A few
#: others (e.g. "william-mary", "east-texas-am") remain genuinely unmapped
#: even with this -- their NCAA-raw sibling keys aren't in the canonical
#: registry either, a separate registry-alias-coverage gap, not something
#: this adapter can fix without touching slug_utils.py.
_REVERSE_NCAA_FALLBACK: dict[str, list[str]] | None = None


def _reverse_ncaa_fallback() -> dict[str, list[str]]:
    global _REVERSE_NCAA_FALLBACK
    if _REVERSE_NCAA_FALLBACK is None:
        reverse: dict[str, list[str]] = {}
        for k, v in NCAA_NET_TO_TEAM_SLUG_FALLBACK.items():
            reverse.setdefault(v, []).append(k)
        _REVERSE_NCAA_FALLBACK = reverse
    return _REVERSE_NCAA_FALLBACK


def resolve_net_team_identity(
    raw_name_or_slug: str, *, source: str = "net_ranking"
) -> TeamIdentity | UnresolvedIdentity:
    """
    Resolve a NET-rankings team string or slug (NCAA.com display name, or
    an already-slugified `team_slug` from a cached rankings file/DataFrame)
    to a canonical :class:`TeamIdentity`.

    Deliberately does **not** use
    ``team_identity.resolve_team_identity_by_shortening`` (the trailing-
    token-dropping helper the ESPN/Covers/Odds adapters share). That
    helper was built for "<school> <mascot>" input, where dropping the
    last word is safe. NET/NCAA data has no mascots -- but it *does* have
    "<school> <state-university-suffix>" collisions where dropping the
    last word lands on a DIFFERENT REAL TEAM, not nothing: "ohio-state" ->
    drop "state" -> "ohio" (Ohio, a real team) is wrong, not a safe miss;
    same for "iowa-state"/"iowa", "penn-state"/"pennsylvania",
    "kansas-city"/"missouri" (via `missouri-kansas-city`), and
    "florida-intl"/"florida". Found this session by cross-checking the
    real 365-row rankings fixture for team_id collisions -- 6 pairs of
    genuinely different programs were silently colliding onto the same
    team_id before this was fixed. Every step below uses plain
    ``resolve_team_identity`` (exact/deterministic only, no shortening).

    Order: (1) direct resolution on the cleaned input -- correct for the
    common case since NCAA names are already close to canonical; (2)
    ``NCAA_NET_TO_TEAM_SLUG_FALLBACK`` direct hit; (3) reverse fallback-dict
    lookup (see :func:`_reverse_ncaa_fallback`). Does **not** consult
    ``CBS_NET_ALIASES_TO_SR`` (hardcodes bare "Miami" -> "Miami (FL)") or
    any `difflib` fuzzy step -- a bare "Miami"/"Loyola" here comes back
    `ambiguous`, same as everywhere else in this migration.
    """
    if not raw_name_or_slug:
        return UnresolvedIdentity(query=str(raw_name_or_slug), source=source, reason="not_found")

    cleaned_slug = _ncaa_row_slug_v15(raw_name_or_slug)

    # Try the hyphenated slug as-is first -- a cached rankings file's
    # team_slug is often already an exact match for a registered SR slug
    # alias (e.g. "college-of-charleston"), which only matches verbatim,
    # not after being space-converted ("college of charleston" misses).
    direct = resolve_team_identity(cleaned_slug, source=source)
    if isinstance(direct, TeamIdentity):
        return direct
    if direct.reason == "ambiguous":
        return direct

    direct = resolve_team_identity(cleaned_slug.replace("-", " "), source=source)
    if isinstance(direct, TeamIdentity):
        return direct
    if direct.reason == "ambiguous":
        return direct

    fallback_hit = NCAA_NET_TO_TEAM_SLUG_FALLBACK.get(cleaned_slug)
    if fallback_hit:
        result = resolve_team_identity(fallback_hit.replace("-", " "), source=source)
        if isinstance(result, TeamIdentity):
            log.info("NCAA_NET_TO_TEAM_SLUG_FALLBACK matched %r -> %r", raw_name_or_slug, fallback_hit)
            return result
        if result.reason == "ambiguous":
            return result

    for sibling in _reverse_ncaa_fallback().get(cleaned_slug, []):
        result = resolve_team_identity(sibling.replace("-", " "), source=source)
        if isinstance(result, TeamIdentity):
            log.info(
                "reverse NCAA fallback matched %r -> sibling key %r -> %r",
                raw_name_or_slug, sibling, result.display_name,
            )
            return result

    # Final tier (Phase 4A pre-batch, Gate 1): the Gate-1-era SR-slug
    # reconciliation record covers a handful of verified slugs the
    # registry's own alias table doesn't yet know as an exact match (e.g.
    # "william-mary") -- wired in additively here, never editing
    # slug_utils.py. Only ever promotes a not_found to a resolved team;
    # an ambiguous result from the tiers above was already returned, so
    # this cannot introduce a new collision.
    reconciled = resolve_team_identity_with_reconciliation(cleaned_slug, source=source)
    if isinstance(reconciled, TeamIdentity):
        log.info("SR-slug reconciliation matched %r -> %r", raw_name_or_slug, reconciled.display_name)
        return reconciled

    return direct


def build_net_rankings_index(
    net_rankings: Union[pd.DataFrame, dict],
) -> tuple[dict[int, int], list[dict]]:
    """
    Canonical replacement for consuming a NET-rankings source: resolves
    every row's team key through :func:`resolve_net_team_identity` instead
    of the string-namespace-bridging tiers in :func:`attach_net_rank_v15`.

    Returns ``(rankings_by_team_id, unmapped)`` where ``unmapped`` is a list
    of ``{"raw_key", "reason", ...}`` dicts for every row that could not be
    cleanly resolved (not_found or ambiguous) -- recorded, never silently
    dropped or guessed at (this batch's instruction #11).
    """
    if isinstance(net_rankings, pd.DataFrame):
        if "team_slug" not in net_rankings.columns or "net_rank" not in net_rankings.columns:
            raise ValidationError(
                f"net_rankings DataFrame missing team_slug/net_rank columns: "
                f"{net_rankings.columns.tolist()}"
            )
        items = list(zip(net_rankings["team_slug"], net_rankings["net_rank"]))
    else:
        items = list(net_rankings.items())

    rankings_by_team_id: dict[int, int] = {}
    unmapped: list[dict] = []

    for raw_key, rank in items:
        result = resolve_net_team_identity(str(raw_key))
        if isinstance(result, TeamIdentity):
            rankings_by_team_id[result.team_id] = int(rank)
        else:
            unmapped.append({"raw_key": raw_key, "rank": rank, "reason": result.reason})
            log.info("NET ranking row unmapped: %r (rank=%s, reason=%s)", raw_key, rank, result.reason)

    return rankings_by_team_id, unmapped


def attach_net_rank_via_registry(
    team_id: int, rankings_by_team_id: dict[int, int]
) -> int | None:
    """
    Canonical replacement for `attach_net_rank`'s lookup half: given a
    canonical ``team_id`` and the output of :func:`build_net_rankings_index`,
    return that team's NET rank or ``None``. No string-slug bridging needed
    at all once both sides are keyed by ``team_id``.
    """
    return rankings_by_team_id.get(team_id)
