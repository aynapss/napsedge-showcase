"""
slug_utils.py — Universal Team Identity Registry
═══════════════════════════════════════════════════════════════════════════════
Every team gets ONE deterministic integer ID (stable across sessions).
Every name variant from every source (SR, Torvik, Covers, ESPN, odds feeds)
maps to that ID. Downstream code resolves names → team_id, then pulls
whatever source-specific slug it needs.

Usage:
    from src.slug_utils import (
        build_registry,          # call once at startup with conferences dict
        resolve_team,            # any string → team_id (or TeamLookupError)
        try_resolve_team,        # any string → team_id | None (silent)
        get_sr_slug,             # team_id → SR slug
        get_torvik_slug,         # team_id → Torvik slug
        get_covers_slug,         # team_id → Covers slug
        get_display_name,        # team_id → display name
        get_conference,          # team_id → conference string
        stable_team_id,          # sr_slug → deterministic int (pure function)
        canonical_team_slug,     # name → normalized slug (helper)
        normalize_team_name,     # name → fuzzy-match-ready string (helper)
        TEAM_REGISTRY,           # dict[int, TeamRecord]
    )

Sport-agnostic: NCAAB populates at startup via build_registry(conferences).
MLB/NBA will call build_registry() with their own team dicts.

Future: extract to src/slug_utils.py in VS Code project.
═══════════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from difflib import get_close_matches
from typing import Optional


# ═════════════════════════════════════════════════════════════════════════════
# ERROR TYPES
# ═════════════════════════════════════════════════════════════════════════════

class TeamLookupError(Exception):
    """
    Raised when a team string cannot be resolved to a team_id.
    Contains diagnostic information for troubleshooting.
    """

    def __init__(self, query: str, source: str = "unknown", context: str = ""):
        self.query = query
        self.source = source
        self.context = context

        # Find near-matches for the error message
        candidates = list(_ALIAS_TO_ID.keys())
        close = get_close_matches(query.lower(), candidates, n=5, cutoff=0.6)
        close_str = ", ".join(f"'{c}'" for c in close) if close else "none"

        msg = (
            f"\n{'═' * 70}\n"
            f"  ❌ TEAM LOOKUP FAILED\n"
            f"  Query:    '{query}'\n"
            f"  Source:   {source}\n"
            f"  Context:  {context}\n"
            f"  Near matches: {close_str}\n"
            f"  Fix: Add this alias to the appropriate layer in slug_utils.py\n"
            f"{'═' * 70}"
        )
        super().__init__(msg)


class SourceSlugError(Exception):
    """
    Raised when a team_id exists but has no slug registered for a
    specific source (e.g., team exists but no Covers slug is known).
    """

    def __init__(self, team_id: int, source: str):
        rec = TEAM_REGISTRY.get(team_id)
        name = rec.display_name if rec else f"id={team_id}"
        msg = (
            f"\n{'═' * 70}\n"
            f"  ❌ SOURCE SLUG MISSING\n"
            f"  Team:     '{name}' (id={team_id})\n"
            f"  Source:   {source}\n"
            f"  SR slug:  {rec.sr_slug if rec else 'unknown'}\n"
            f"  Fix: Add a {source} alias in _SOURCE_ALIASES['{source}'] in slug_utils.py\n"
            f"{'═' * 70}"
        )
        super().__init__(msg)


# ═════════════════════════════════════════════════════════════════════════════
# TEAM RECORD
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class TeamRecord:
    team_id: int
    display_name: str
    sr_slug: str
    conference: str
    aliases: set = field(default_factory=set)
    source_slugs: dict = field(default_factory=dict)  # {"torvik": "vcu", "covers": "..."}


# ═════════════════════════════════════════════════════════════════════════════
# GLOBAL STATE
# ═════════════════════════════════════════════════════════════════════════════

TEAM_REGISTRY: dict[int, TeamRecord] = {}
_ALIAS_TO_ID: dict[str, int] = {}       # lowercased alias → team_id
_SR_SLUG_TO_ID: dict[str, int] = {}     # sr_slug → team_id (primary key)
_REGISTRY_BUILT = False


# ═════════════════════════════════════════════════════════════════════════════
# PURE UTILITY FUNCTIONS (no state dependency)
# ═════════════════════════════════════════════════════════════════════════════

def stable_team_id(sr_slug: str) -> int:
    """
    Deterministic 6-digit ID from the SR slug.
    SHA-256 based — never changes between sessions.
    """
    return int(hashlib.sha256(sr_slug.encode()).hexdigest(), 16) % 900_000 + 100_000


def canonical_team_slug(name: str) -> str:
    """
    Canonical identity slug for ALL data joins.
    Preserves 'state' to prevent team collisions.
    """
    if not name:
        return ""

    name = name.lower()
    name = re.sub(r"\(.*?\)", "", name)

    # Remove mascots and suffixes
    mascots = [
        "falcons", "zips", "eagles", "tigers", "bulldogs", "wildcats", "bears",
        "hawks", "panthers", "raiders", "vikings", "broncs", "stags",
        "phoenix", "jaguars", "gaels", "pioneers", "red foxes",
        "saints", "warriors", "mountaineers", "peacocks",
        "titans", "griffins", "jaspers", "golden grizzlies", "mountain hawks",
        "wolf pack",
    ]
    for m in mascots:
        name = name.replace(m, "")

    replacements = {
        "&": "and",
        "st.": "saint",
        "st ": "saint ",
        "university": "",
        "college": "",
        "the": "",
        ".": "",
        "'": "",
    }
    for k, v in replacements.items():
        name = name.replace(k, v)

    name = re.sub(r"[^a-z ]+", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name.replace(" ", "-")


def normalize_team_name(name: str) -> str:
    """
    Normalize team name for fuzzy matching.
    Mascots intentionally ignored.
    """
    if not name:
        return ""

    name = name.lower()
    name = re.sub(r"\(.*?\)", "", name)

    replacements = {
        "&": "and",
        "st.": "saint",
        "st ": "saint ",
        "'": "",
        "\u2019": "",    # right single quote
        ".": "",
        "-": " ",
        "_": " ",
    }
    for k, v in replacements.items():
        name = name.replace(k, v)

    name = re.sub(r"\s+", " ", name).strip()
    return name


def normalize_team_name_for_odds(name: str) -> str:
    """Lightweight normalization for odds feed matching."""
    return (
        name.lower()
        .replace("state", "st")
        .replace(".", "")
        .replace("&", "and")
        .strip()
    )


# ═════════════════════════════════════════════════════════════════════════════
# ALIAS DICTIONARIES — ONE PLACE FOR ALL SOURCES
# ═════════════════════════════════════════════════════════════════════════════
#
# Structure:
#   _DISPLAY_NAME_ALIASES: user-facing name variants → canonical display name
#   _SR_OVERRIDES:         any string → SR slug (merges old MANUAL_SLUG_OVERRIDES
#                          + TEAM_SLUG_TO_SR_PATCH)
#   _SOURCE_ALIASES:       { "torvik": {sr_slug: torvik_slug},
#                            "covers": {sr_slug: covers_slug} }
#
# To add a new source (ESPN, odds feed, etc.): add a key to _SOURCE_ALIASES.
# ═════════════════════════════════════════════════════════════════════════════

# ── Display name corrections (colloquial → canonical) ────────────────────────
_DISPLAY_NAME_ALIASES: dict[str, str] = {
    # ATLANTIC 10
    "bonnies": "St. Bonaventure",
    "st bonas": "St. Bonaventure",
    "st bonaventure": "St. Bonaventure",
    "st. bonaventure": "St. Bonaventure",
    "st josephs": "Saint Joseph's",
    "st joes": "Saint Joseph's",
    "st. josephs": "Saint Joseph's",
    "saint josephs": "Saint Joseph's",
    "st louis": "Saint Louis",
    "st. louis": "Saint Louis",
    "saint louis": "Saint Louis",
    "lasalle": "La Salle",
    "la salle": "La Salle",
    "gw": "George Washington",
    "uri": "Rhode Island",
    "vcu": "Virginia Commonwealth",
    "loyola chicago": "Loyola (IL)",
    "loyola il": "Loyola (IL)",
    "loyola-chicago": "Loyola (IL)",
    # BIG EAST
    "uconn": "Connecticut",
    "st johns": "St. John's (NY)",
    "st john's": "St. John's (NY)",
    "st johns ny": "St. John's (NY)",
    "st. johns": "St. John's (NY)",
    "st. john's": "St. John's (NY)",
    # ACC
    "unc": "North Carolina",
    "nc state": "North Carolina State",
    "ncsu": "North Carolina State",
    "vt": "Virginia Tech",
    "fsu": "Florida State",
    "pitt": "Pittsburgh",
    # BIG TEN
    "osu": "Ohio State",
    "msu": "Michigan State",
    # BIG 12
    "k-state": "Kansas State",
    "ok state": "Oklahoma State",
    # SEC
    "ole miss": "Mississippi",
    "mizzou": "Missouri",
    # MID-MAJOR / NATIONAL
    "umass": "Massachusetts",
    "ipfw": "Purdue Fort Wayne",
    # ABBREVIATIONS
    "app state": "Appalachian State",
    "byu": "Brigham Young",
    "lsu": "Louisiana State",
    "smu": "Southern Methodist",
    "tcu": "Texas Christian",
    "ucf": "Central Florida",
    "uab": "Alabama-Birmingham",
    "utep": "Texas-El Paso",
    # NAME VARIANTS
    "central connecticut": "Central Connecticut State",
    "long island university": "LIU",
    "long island university sharks": "Long Island University",
    "queens university": "Queens (NC)",
    "saint francis": "Saint Francis (PA)",
    "saint peters": "St. Peter's",
    "st peters": "St. Peter's",
    # GEOGRAPHY
    "hawai'i": "Hawaii",
    "hawaii": "Hawaii",
    "se louisiana": "Southeastern Louisiana",
    "st thomas minnesota": "St. Thomas (MN)",
    "ul monroe": "Louisiana-Monroe",
    "umbc": "Maryland-Baltimore County",
    # REBRANDS
    "iu indianapolis": "IUPUI",
    "loyola maryland": "Loyola (MD)",
    # SPECIAL CASES
    "ualbany": "Albany",
    "ut martin": "Tennessee-Martin",
    "western illinois": "Western Illinois",
    "njit": "New Jersey Institute of Technology",
    "uncw": "UNC Wilmington",
    "ucsb": "UC Santa Barbara",
    "unc-ash": "UNC Asheville",
    "uncg": "UNC Greensboro",
    "iui": "IUPUI",
}


# ── SR slug overrides (any string → canonical SR slug) ───────────────────────
# Merges old MANUAL_SLUG_OVERRIDES + TEAM_SLUG_TO_SR_PATCH into ONE dict.
# Key = anything the pipeline might produce; Value = exact SR URL slug.
_SR_OVERRIDES: dict[str, str] = {
    # ── Abbreviation → Full SR slug ──────────────────────────────────────────
    "TCU":                          "texas-christian",
    "Texas Christian":              "texas-christian",
    "BYU":                          "brigham-young",
    "Brigham Young":                "brigham-young",
    "SMU":                          "southern-methodist",
    "Southern Methodist":           "southern-methodist",
    "LSU":                          "louisiana-state",
    "Louisiana State":              "louisiana-state",
    "UNLV":                         "nevada-las-vegas",
    "Nevada-Las Vegas":             "nevada-las-vegas",
    "VCU":                          "virginia-commonwealth",
    "Virginia Commonwealth":        "virginia-commonwealth",
    "UAB":                          "alabama-birmingham",
    "UTEP":                         "texas-el-paso",
    "UTSA":                         "texas-san-antonio",
    "NJIT":                         "njit",
    "UConn":                        "connecticut",
    "Connecticut":                  "connecticut",
    "Ole Miss":                     "mississippi",
    "Mississippi":                  "mississippi",
    "Ohio State":                   "ohio-state",
    "OSU":                          "ohio-state",
    "Penn State":                   "penn-state",
    "Penn":                         "pennsylvania",
    "Pitt":                         "pittsburgh",
    "Pittsburgh":                   "pittsburgh",
    "Southern Miss Golden Eagles":  "southern-mississippi",
    "Southern Miss Golden":         "southern-mississippi",
    "Southern Miss":                "southern-mississippi",
    "Houston Christian":            "houston-baptist",
    "Nicholls":                     "nicholls-state",
    # UCF
    "UCF":                          "central-florida",
    "Central Florida":              "central-florida",
    # IUPUI
    "IU Indianapolis Jaguars":      "iupui",
    "IU Indianapolis":              "iupui",
    "iu-indy":                      "iupui",
    "iu-indianapolis":              "iupui",
    "IUPUI":                        "iupui",
    # Arkansas / Little Rock / UMass
    "Little Rock":                  "arkansas-little-rock",
    "UMass Lowell":                 "massachusetts-lowell",
    "UMass":                        "massachusetts",
    # & encoding
    "Alabama A&M":                  "alabama-aandm",
    "FAMU":                         "florida-aandm",
    "Florida A&M":                  "florida-aandm",
    "North Carolina A&T":           "north-carolina-at",
    "NC A&T":                       "north-carolina-at",
    "Texas A&M":                    "texas-am",
    "A&M-Corpus Christi":           "texas-aandm-corpus-christi",
    "Texas A&M-CC":                 "texas-aandm-corpus-christi",
    "Texas A&M-Corpus Christi":     "texas-am-corpus-christi",
    "East Texas A&M":               "east-texas-am",
    "Prairie View A&M":             "prairie-view",
    "Prairie View A&M Panthers":    "prairie-view",
    "prairie-view-am":              "prairie-view",
    # Mascot appended
    "Savannah State":               "savannah-state-tigers",
    # UNC family
    "UNC":                          "north-carolina",
    "UIC Flames":                   "illinois-chicago",
    "uic":                          "illinois-chicago",
    "Illinois-Chicago":             "illinois-chicago",
    "UNCW":                         "north-carolina-wilmington",
    "UNC Wilmington":               "north-carolina-wilmington",
    "UCSB":                         "california-santa-barbara",
    "UC Santa Barbara":             "california-santa-barbara",
    "UNCG":                         "north-carolina-greensboro",
    "UNC Greensboro":               "north-carolina-greensboro",
    "UNC Asheville":                "north-carolina-asheville",
    "UNC-Ash.":                     "north-carolina-asheville",
    "UMBC":                         "maryland-baltimore-county",
    # UT family
    "UT Arlington":                 "texas-arlington",
    "UT-Arlington":                 "texas-arlington",
    "UTA":                          "texas-arlington",
    "UTRGV":                        "texas-pan-american",
    "UT Rio Grande Valley":         "texas-pan-american",
    # FIU / FAU / UIW
    "FIU":                          "florida-international",
    "Florida International":        "florida-international",
    "Florida Int'l":                "florida-international",
    "FAU":                          "florida-atlantic",
    "Florida Atlantic":             "florida-atlantic",
    "UIW":                          "incarnate-word",
    "Incarnate Word":               "incarnate-word",
    # Saint / St variants
    "Saint Francis (PA)":           "saint-francis-pa",
    "Saint Francis PA":             "saint-francis-pa",
    "SFU":                          "saint-francis-pa",
    "Saint Francis (NY)":           "st-francis-ny-terriers",
    "SFC":                          "st-francis-ny-terriers",
    "Saint Peter's":                "saint-peters",
    "St. Peter's":                  "saint-peters",
    "Saint Joseph's":               "saint-josephs",
    "St. Joseph's":                 "saint-josephs",
    # Real local TeamDump/CBB directory-slug spelling (apostrophe preserved,
    # unlike the naive-slugification path's own "saint-josephs" -- confirmed
    # by direct reproduction to be a real, distinct literal string that the
    # existing overrides above never matched). Same pattern as the
    # "st-thomas-(mn)" entry below.
    "saint-joseph's":               "saint-josephs",
    "Saint Mary's Gaels":           "saint-marys-ca",
    "Saint Mary's":                 "saint-marys-ca",
    "saint-marys":                  "saint-marys-ca",
    "St. Thomas":                   "st-thomas-mn",
    "Saint Thomas":                 "st-thomas-mn",
    "St. Thomas (MN)":              "st-thomas-mn",
    "st-thomas-(mn)":               "st-thomas-mn",
    "St. Thomas-Minnesota Tommies": "st-thomas-mn",
    "Saint Louis":                  "saint-louis",
    "St. Bonaventure":              "st-bonaventure",
    "st-johns-ny":                  "st-johns-ny",
    "St. John's Red Storm":         "st-johns-ny",
    "St. John's (NY)":              "st-johns-ny",
    "St. John's":                   "st-johns-ny",
    "st-johns":                     "st-johns-ny",
    "USC Upstate":                  "south-carolina-upstate",
    # Loyola variants
    "LMU":                          "loyola-marymount",
    "Loyola Marymount":             "loyola-marymount",
    "Loyola (MD)":                  "loyola-md",
    "Loyola MD":                    "loyola-md",
    "Loyola Maryland":              "loyola-md",
    "Loyola Maryland Greyhounds":   "loyola-md",
    "Loyola (IL)":                  "loyola-il",
    "Loyola Chicago":               "loyola-il",
    "Loyola IL":                    "loyola-il",
    # Hawaiʻi
    "Hawai\u02bbi":                 "hawaii",
    "Hawai\u02bbi Rainbow Warriors": "hawaii",
    "Hawai'i":                      "hawaii",
    "Hawai'i Rainbow Warriors":     "hawaii",
    "Hawaii":                       "hawaii",
    "Hawaii Rainbow Warriors":      "hawaii",
    # SIU / LIU / UMKC
    "SIU":                          "southern-illinois",
    "Southern Illinois":            "southern-illinois",
    "SIU Edwardsville":             "southern-illinois-edwardsville",
    "SIUE":                         "southern-illinois-edwardsville",
    "LIU":                          "long-island-university",
    "LIU Brooklyn":                 "long-island-university",
    "Long Island":                  "long-island-university",
    "UMKC":                         "kansas-city",
    "Kansas City":                  "kansas-city",
    # Purdue / IPFW
    "IPFW":                         "purdue-fort-wayne",
    "Purdue Fort Wayne Mastodons":  "ipfw",
    "purdue-fort-wayne":            "ipfw",
    # Sam Houston / SFA
    "Sam Houston Bearkats":         "sam-houston-state",
    "sam-houston":                  "sam-houston-state",
    "SHSU":                         "sam-houston-state",
    "Stephen F. Austin":            "stephen-f-austin",
    "SFA":                          "stephen-f-austin",
    # Southeast / Appalachian / Mountain
    "Southeast Missouri State":     "southeast-missouri-state",
    "SE Missouri St":               "southeast-missouri-state",
    "SEMO":                         "southeast-missouri-state",
    "Southeast Missouri":           "southeast-missouri-state",
    "Tennessee-Martin":             "tennessee-martin",
    "Tenn-Martin":                  "tennessee-martin",
    "UT Martin":                    "tennessee-martin",
    "App State":                    "appalachian-state",
    "Appalachian State":            "appalachian-state",
    "ETSU":                         "east-tennessee-state",
    "East Tennessee State":         "east-tennessee-state",
    "WCU":                          "western-carolina",
    "Western Carolina":             "western-carolina",
    "Furman":                       "furman",
    "Mercer":                       "mercer",
    "Samford":                      "samford",
    "Chattanooga":                  "chattanooga",
    "UTC":                          "chattanooga",
    "The Citadel":                  "citadel",
    "Citadel":                      "citadel",
    "VMI":                          "virginia-military-institute",
    "Virginia Military":            "virginia-military-institute",
    # Miami variants
    "Miami (FL)":                   "miami-fl",
    "Miami FL":                     "miami-fl",
    "miami-(fl)":                   "miami-fl",
    "miami-fl":                     "miami-fl",
    "Miami (OH)":                   "miami-oh",
    "Miami OH":                     "miami-oh",
    # Real local TeamDump/CBB directory-slug spelling -- the "(fl)" sibling
    # above already had this exact form registered; "(oh)" did not, the
    # confirmed gap this batch closes. Never derived via canonical_team_slug()
    # (which strips the whole parenthetical and collides "miami-(oh)" with
    # "miami-(fl)" into the same bare "miami" key) -- an explicit, narrow,
    # literal-string override instead, same mechanism as every other entry
    # in this table.
    "miami-(oh)":                   "miami-oh",
    # Midwest / Plains
    "Bowling Green":                "bowling-green",
    "BGSU":                         "bowling-green",
    # Real local TeamDump/CBB directory-slug spelling (Phase 5C4A, confirmed
    # by direct reproduction: `bowling-green` above already resolves to the
    # canonical MAC team; the real local directory is named `bowling-green-state`
    # instead -- an explicit, narrow, literal-string override, same mechanism
    # as `miami-(oh)` above, never a broadened normalization rule).
    "bowling-green-state":          "bowling-green",
    # William & Mary (CAA): the display name "William & Mary" already
    # naturally derives the sr_slug "william-&-mary" (its own real,
    # already-registered primary slug -- confirmed by direct reproduction,
    # never re-derived here). The real local TeamDump/CBB directory is
    # named `william-mary` (no ampersand) instead -- an explicit, narrow,
    # literal-string override to that same already-registered slug, same
    # mechanism as every other entry in this table. Distinct from the
    # separate, not-yet-wired-in `sr_slug_reconciliation.json`/
    # `resolve_team_identity_with_reconciliation` path (Phase 4A), which
    # already resolves this same real slug through a different mechanism
    # not used by the bulk-import adapters this override serves.
    "william-mary":                 "william-&-mary",
    "WIU":                          "western-illinois",
    "Western Illinois":             "western-illinois",
    "EIU":                          "eastern-illinois",
    "Eastern Illinois":             "eastern-illinois",
    "EKU":                          "eastern-kentucky",
    "Eastern Kentucky":             "eastern-kentucky",
    "Northern Colorado":            "northern-colorado",
    "Omaha":                        "nebraska-omaha",
    "Nebraska-Omaha":               "nebraska-omaha",
    "UNO":                          "nebraska-omaha",
    "South Dakota":                 "south-dakota",
    "USD":                          "south-dakota",
    "North Dakota":                 "north-dakota",
    "UND":                          "north-dakota",
    "NDSU":                         "north-dakota-state",
    "North Dakota State":           "north-dakota-state",
    "SDSU":                         "south-dakota-state",
    "South Dakota State":           "south-dakota-state",
    "Morehead State":               "morehead-state",
    "Tennessee State":              "tennessee-state",
    "TSU":                          "tennessee-state",
    "MSU-Bozeman":                  "montana-state",
    "Montana State":                "montana-state",
    "Idaho State":                  "idaho-state",
    "Weber State":                  "weber-state",
    "Portland State":               "portland-state",
    "Sacramento State":             "sacramento-state",
    "Sac State":                    "sacramento-state",
    "Jackson State":                "jackson-state",
    "Jacksonville State":           "jacksonville-state",
    "JSU":                          "jacksonville-state",
    "Kennesaw State":               "kennesaw-state",
    "KSU":                          "kennesaw-state",
    "Lindenwood":                   "lindenwood",
    "Tarleton State":               "tarleton-state",
    "Tarleton":                     "tarleton-state",
    "Detroit":                      "detroit-mercy",
    # Western schools
    "NAU":                          "northern-arizona",
    "Northern Arizona":             "northern-arizona",
    "SUU":                          "southern-utah",
    "Southern Utah":                "southern-utah",
    "UVU":                          "utah-valley",
    "Utah Valley":                  "utah-valley",
    "Utah Tech":                    "dixie-state",
    "Dixie State":                  "dixie-state",
    "GCU":                          "grand-canyon",
    "Grand Canyon":                 "grand-canyon",
    "CBU":                          "california-baptist",
    "Cal Baptist":                  "california-baptist",
    "ACU":                          "abilene-christian",
    "Abilene Christian":            "abilene-christian",
    "ORU":                          "oral-roberts",
    "Oral Roberts":                 "oral-roberts",
    "Cal Poly":                     "cal-poly",
    "Cal Poly SLO":                 "cal-poly",
    "Long Beach State":             "long-beach-state",
    "Long Beach St":                "long-beach-state",
    "UC Irvine":                    "california-irvine",
    "UC Davis":                     "california-davis",
    "UC San Diego":                 "california-san-diego",
    "UCSD":                         "california-san-diego",
    "UC Riverside":                 "california-riverside",
    "CSUN":                         "cal-state-northridge",
    "CSU Northridge":               "cal-state-northridge",
    "CSUF":                         "cal-state-fullerton",
    "CSU Fullerton":                "cal-state-fullerton",
    "CSU Bakersfield":              "cal-state-bakersfield",
    "San Diego State":              "san-diego-state",
    # Northeast / Mid-Atlantic
    "Albany":                       "albany-ny",
    "UAlbany":                      "albany-ny",
    "SUNY Albany":                  "albany-ny",
    "CCSU":                         "central-connecticut-state",
    "Central Connecticut":          "central-connecticut-state",
    "Northeastern":                 "northeastern",
    "Queens (NC)":                  "queens-nc",
    "Queens NC":                    "queens-nc",
    "Queens University Royals":     "queens-nc",
    "Queens University":            "queens-nc",
    "queens":                       "queens-nc",
    "Bellarmine":                   "bellarmine",
    "Stonehill":                    "stonehill",
    "Le Moyne":                     "le-moyne",
    "Mercyhurst":                   "mercyhurst",
    "Denver":                       "denver",
    "DU":                           "denver",
    "HPU":                          "high-point",
    "High Point":                   "high-point",
    "Longwood":                     "longwood",
    "Campbell":                     "campbell",
    "Charleston Southern":          "charleston-southern",
    "Charleston":                   "college-of-charleston",
    "Winthrop":                     "winthrop",
    "Presbyterian":                 "presbyterian",
    # Southern / SWAC / MEAC
    "UNA":                          "north-alabama",
    "North Alabama":                "north-alabama",
    "USI":                          "southern-indiana",
    "Southern Indiana":             "southern-indiana",
    "Texas Southern":               "texas-southern",
    "Grambling State":              "grambling",
    "Grambling St":                 "grambling",
    "Alcorn State":                 "alcorn-state",
    "Alcorn St":                    "alcorn-state",
    "Alabama State":                "alabama-state",
    "Alabama St":                   "alabama-state",
    "Southern":                     "southern-university",
    "Southern University":          "southern-university",
    "Coppin State":                 "coppin-state",
    "Coppin St":                    "coppin-state",
    "Morgan State":                 "morgan-state",
    "Morgan St":                    "morgan-state",
    "Norfolk State":                "norfolk-state",
    "Norfolk St":                   "norfolk-state",
    "Howard":                       "howard",
    "NCCU":                         "north-carolina-central",
    "North Carolina Central":       "north-carolina-central",
    "SC State":                     "south-carolina-state",
    "South Carolina State":         "south-carolina-state",
    "South Carolina St":            "south-carolina-state",
    "UMES":                         "maryland-eastern-shore",
    "MD-Eastern Shore":             "maryland-eastern-shore",
    "Maryland-Eastern Shore":       "maryland-eastern-shore",
    "MVS":                          "mississippi-valley-state",
    "Miss Valley St":               "mississippi-valley-state",
    "Mississippi Valley State":     "mississippi-valley-state",
    "B-CU":                         "bethune-cookman",
    "Bethune-Cookman":              "bethune-cookman",
    "UAPB":                         "arkansas-pine-bluff",
    "Arkansas-Pine Bluff":          "arkansas-pine-bluff",
    "Delaware State":               "delaware-state",
    "Delaware St":                  "delaware-state",
    "USC":                          "southern-california",
    "USC Trojans":                  "southern-california",
    "Texas-El Paso":                "texas-el-paso",
    "UTEP Miners":                  "texas-el-paso",
    "Wake":                         "wake-forest",
    "mcneese":                      "mcneese-state",
    "mcneese-state":                "mcneese-state",

    # ── From TEAM_SLUG_TO_SR_PATCH (slug-form variants) ──────────────────────
    "california-bakersfield":       "cal-state-bakersfield",
    "csu-bakersfield":              "cal-state-bakersfield",
    "cal-st-bakersfield":           "cal-state-bakersfield",
    "long-island-university":       "liu",
    "long-island":                  "liu",
    "texas-pan-american":           "texas-rio-grande-valley",
    "dixie-state":                  "utah-tech",
    "detroit":                      "detroit-mercy",
    "charleston":                   "college-of-charleston",
    "omaha":                        "nebraska-omaha",
    "md-east-shore":                "maryland-eastern-shore",
    "new-jersey-institute-of-technology": "njit",
    "little-rock":                  "arkansas-little-rock",
    "texas-aandm-corpus-christi":   "texas-am-corpus-christi",
    "tx-am-corpus-christi":         "texas-am-corpus-christi",
    "tcu":                          "texas-christian",
    "ucf":                          "central-florida",
    "uab":                          "alabama-birmingham",
    "uic":                          "illinois-chicago",
    "uncw":                         "north-carolina-wilmington",
    "uncg":                         "north-carolina-greensboro",
    "unc-ash":                      "north-carolina-asheville",
    "uc-irvine":                    "california-irvine",
    "ucsb":                         "california-santa-barbara",
    "uc-riverside":                 "california-riverside",
    "utep":                         "texas-el-paso",
    "iui":                          "iupui",
    "fdu":                          "fairleigh-dickinson",
    "kansas-city":                  "missouri-kansas-city",
    "pfw":                          "ipfw",
    "cal-baker":                    "cal-state-bakersfield",
    "unc ash":                      "north-carolina-asheville",
    "unc-ash.":                     "north-carolina-asheville",
    "uncw.":                        "north-carolina-wilmington",
    "uncg.":                        "north-carolina-greensboro",
    "u-c-s-b":                      "california-santa-barbara",
    "uc-sb":                        "california-santa-barbara",
    "unc-wilmington":               "north-carolina-wilmington",
    "unc-greensboro":               "north-carolina-greensboro",
    "unc-asheville":                "north-carolina-asheville",
    "vcu":                          "virginia-commonwealth",
    "smu":                          "southern-methodist",
    "byu":                          "brigham-young",
    "unlv":                         "nevada-las-vegas",
    "uconn":                        "connecticut",
    "lsu":                          "louisiana-state",
    "usc":                          "southern-california",
    "pitt":                         "pittsburgh",
    "ole-miss":                     "mississippi",
    "app-state":                    "appalachian-state",
    "siue":                         "southern-illinois-edwardsville",
    "siu-edward":                   "southern-illinois-edwardsville",
    "liu":                          "long-island-university",
}


# ── Source-specific slug maps ────────────────────────────────────────────────
# Key = sr_slug (canonical), Value = slug as it appears on that source.
# These get registered as aliases AND stored in TeamRecord.source_slugs.
_SOURCE_ALIASES: dict[str, dict[str, str]] = {

    "torvik": {
        # SR slug → Torvik slug (only where they differ)
        "virginia-commonwealth":          "vcu",
        "saint-marys-ca":                 "saint-marys",
        "loyola-il":                      "loyola-chicago",
        "texas-christian":                "tcu",
        "brigham-young":                  "byu",
        "central-florida":                "ucf",
        "north-carolina-state":           "nc-state",
        "miami-fl":                       "miami-fl",
        "florida-international":          "fiu",
        "southern-california":            "usc",
        "massachusetts":                  "umass",
        "louisiana-state":                "lsu",
        "mississippi":                    "ole-miss",
        "california-santa-barbara":       "ucsb",
        "california-davis":               "uc-davis",
        "california-irvine":              "uc-irvine",
        "california-san-diego":           "uc-san-diego",
        "st-johns-ny":                    "st-johns",
        "saint-josephs":                  "saint-josephs",
        "saint-louis":                    "saint-louis",
        "illinois-chicago":               "uic",
        "texas-san-antonio":              "utsa",
        "texas-el-paso":                  "utep",
        "texas-arlington":                "ut-arlington",
        "texas-rio-grande-valley":        "utrgv",
        "maryland-baltimore-county":      "umbc",
        "north-carolina-asheville":       "unc-asheville",
        "north-carolina-greensboro":      "unc-greensboro",
        "north-carolina-wilmington":      "unc-wilmington",
        "southeast-missouri-state":       "semo",
        "louisiana-lafayette":            "louisiana",
        "louisiana-monroe":               "ul-monroe",
        "nebraska-omaha":                 "omaha",
        "colorado-state":                 "colorado-st",
        "cal-state-fullerton":            "cal-st-fullerton",
        "cal-state-bakersfield":          "cal-st-bakersfield",
        "cal-state-northridge":           "cal-st-northridge",
        "long-beach-state":               "long-beach-st",
    },

    "covers": {
        # SR slug → Covers URL slug
        "st-johns-ny":                    "st.-john-s-red-storm",
        "saint-josephs":                  "saint-joseph-s-hawks",
        "prairie-view":                   "prairie-view-a-m-panthers",
        "virginia-military-institute":    "vmi-keydets",
        "southern-illinois-edwardsville": "siue-cougars",
        "louisiana-state":                "lsu-tigers",
        "mississippi":                    "mississippi-rebels",
        "pittsburgh":                     "pittsburgh-panthers",
        "connecticut":                    "connecticut-huskies",
        "central-florida":                "central-florida-knights",
        "illinois-chicago":               "illinois-chicago-flames",
        "maryland-baltimore-county":      "maryland-baltimore-county-retrievers",
        "north-carolina":                 "north-carolina-tar-heels",
        "north-carolina-asheville":       "north-carolina-asheville-bulldogs",
        "north-carolina-greensboro":      "north-carolina-greensboro-spartans",
        "north-carolina-wilmington":      "north-carolina-wilmington-seahawks",
        "north-carolina-state":           "north-carolina-state-wolfpack",
        "miami-fl":                       "miami-fl-hurricanes",
        "miami-oh":                       "miami-oh-redhawks",
        "florida-international":          "florida-international-panthers",
        "florida-atlantic":               "florida-atlantic-owls",
        "texas-san-antonio":              "texas-san-antonio-roadrunners",
        "texas-el-paso":                  "texas-el-paso-miners",
        "tennessee-martin":               "tennessee-martin-skyhawks",
    },
}


# ═════════════════════════════════════════════════════════════════════════════
# REGISTRY CONSTRUCTION
# ═════════════════════════════════════════════════════════════════════════════

def _register_alias(alias: str, team_id: int) -> None:
    """Register a single alias → team_id mapping. Case-insensitive."""
    key = alias.strip().lower() if isinstance(alias, str) else str(alias).strip().lower()
    if key:
        _ALIAS_TO_ID[key] = team_id


def _register_team(
    display_name: str,
    sr_slug: str,
    conference: str,
) -> int:
    """
    Idempotent team registration. Returns stable team_id.
    Registers display_name, sr_slug, and common derivations as aliases.
    """
    sr_slug = sr_slug.strip().lower()

    if sr_slug in _SR_SLUG_TO_ID:
        tid = _SR_SLUG_TO_ID[sr_slug]
        rec = TEAM_REGISTRY[tid]
        # Still register new alias
        for alias in [display_name, display_name.lower(), sr_slug]:
            _register_alias(alias, tid)
            rec.aliases.add(alias.lower())
        return tid

    tid = stable_team_id(sr_slug)
    rec = TeamRecord(
        team_id=tid,
        display_name=display_name,
        sr_slug=sr_slug,
        conference=conference,
    )
    TEAM_REGISTRY[tid] = rec
    _SR_SLUG_TO_ID[sr_slug] = tid

    # Register standard aliases
    for alias in [display_name, display_name.lower(), sr_slug]:
        _register_alias(alias, tid)
        rec.aliases.add(alias.lower())

    # Also register the naive slugification of the display name
    _naive = display_name.lower()
    # Strip parentheticals: "(NY)", "(FL)", "(PA)" etc.
    _naive = re.sub(r"\(.*?\)", "", _naive)
    naive_slug = (
        _naive
        .replace(" ", "-")
        .replace("'", "")
        .replace("\u2019", "")
        .replace(".", "")
    )
    # Clean double dashes and trailing dashes from parenthetical removal
    naive_slug = re.sub(r"-{2,}", "-", naive_slug).strip("-")
    if naive_slug and naive_slug != sr_slug:
        _register_alias(naive_slug, tid)
        rec.aliases.add(naive_slug)

    return tid


def _register_source_slugs() -> None:
    """
    Register all source-specific slugs as aliases and store them
    in each TeamRecord's source_slugs dict.
    """
    for source_name, slug_map in _SOURCE_ALIASES.items():
        for sr_slug, source_slug in slug_map.items():
            tid = _SR_SLUG_TO_ID.get(sr_slug)
            if tid is None:
                # Team not yet registered — skip (might be from a different sport)
                continue
            rec = TEAM_REGISTRY[tid]
            rec.source_slugs[source_name] = source_slug
            _register_alias(source_slug, tid)
            rec.aliases.add(source_slug.lower())


def _register_sr_overrides() -> None:
    """
    Register all SR override aliases. These map any string (display names,
    abbreviations, slug variants) to their canonical SR slug's team_id.
    """
    for alias, sr_slug in _SR_OVERRIDES.items():
        sr_slug_lower = sr_slug.strip().lower()
        tid = _SR_SLUG_TO_ID.get(sr_slug_lower)
        if tid is not None:
            _register_alias(alias, tid)
            _register_alias(alias.lower(), tid)
            TEAM_REGISTRY[tid].aliases.add(alias.lower())


def _register_display_aliases() -> None:
    """
    Register display name correction aliases.
    Maps colloquial names to their canonical display name's team_id.
    """
    for alias, canonical_name in _DISPLAY_NAME_ALIASES.items():
        # Find the team_id for the canonical name
        tid = _ALIAS_TO_ID.get(canonical_name.lower())
        if tid is not None:
            _register_alias(alias, tid)
            TEAM_REGISTRY[tid].aliases.add(alias.lower())


def build_registry(conferences: dict[str, list[str]]) -> None:
    """
    Build the team identity registry from a conferences dict.
    Call once at startup. Idempotent — safe to call multiple times.

    Args:
        conferences: { "Atlantic 10": ["St. Bonaventure", "VCU", ...], ... }
    """
    global _REGISTRY_BUILT

    # Step 1: Register every team from conferences dict
    for conf_name, teams in conferences.items():
        for display_name in teams:
            # Resolve SR slug through overrides — try multiple forms
            raw_slug = (
                display_name.lower()
                .replace(" ", "-")
                .replace("'", "")
                .replace("\u2019", "")
                .replace(".", "")
            )
            # Also try without parentheticals: "St. John's (NY)" → "St. John's"
            stripped_name = re.sub(r"\s*\(.*?\)\s*", "", display_name).strip()
            stripped_slug = re.sub(r"\s*\(.*?\)\s*", "", raw_slug).strip("-")
            stripped_slug = re.sub(r"-{2,}", "-", stripped_slug)

            sr_slug = (
                _SR_OVERRIDES.get(display_name)
                or _SR_OVERRIDES.get(stripped_name)
                or _SR_OVERRIDES.get(raw_slug)
                or _SR_OVERRIDES.get(stripped_slug)
                or raw_slug
            )
            _register_team(display_name, sr_slug, conf_name)

    # Step 2: Register all SR override aliases
    _register_sr_overrides()

    # Step 3: Register all display name aliases
    _register_display_aliases()

    # Step 4: Register source-specific slugs (Torvik, Covers, etc.)
    _register_source_slugs()

    _REGISTRY_BUILT = True
    print(f"✅ TEAM_REGISTRY built — {len(TEAM_REGISTRY)} teams, {len(_ALIAS_TO_ID)} aliases")


# ═════════════════════════════════════════════════════════════════════════════
# PUBLIC LOOKUP API
# ═════════════════════════════════════════════════════════════════════════════

def resolve_team(name_or_slug: str, *, source: str = "unknown", context: str = "") -> int:
    """
    Resolve any team string to its stable integer team_id.

    Resolution order:
      1. Exact match (case-insensitive) in alias index
      2. SR override lookup → alias index
      3. Canonical slug derivation → alias index
      4. Fuzzy match (cutoff=0.75) → alias index
      5. TeamLookupError with diagnostic near-matches

    Args:
        name_or_slug: Any team name, abbreviation, or slug from any source
        source: Where this string came from (for error diagnostics)
        context: Additional context for error messages

    Returns:
        int — stable team_id

    Raises:
        TeamLookupError — with diagnostic message including near-matches
    """
    if not name_or_slug or not isinstance(name_or_slug, str):
        raise TeamLookupError(str(name_or_slug), source, context or "empty/null input")

    s = name_or_slug.strip()
    s_lower = s.lower()

    # 1. Exact alias match
    tid = _ALIAS_TO_ID.get(s_lower)
    if tid is not None:
        return tid

    # Also try original case (some overrides are case-sensitive)
    tid = _ALIAS_TO_ID.get(s)
    if tid is not None:
        return tid

    # 2. SR override → slug lookup
    sr_slug = _SR_OVERRIDES.get(s) or _SR_OVERRIDES.get(s_lower)
    if sr_slug:
        tid = _SR_SLUG_TO_ID.get(sr_slug.lower())
        if tid is not None:
            # Auto-register for faster future lookups
            _register_alias(s, tid)
            return tid

    # 3. Canonical slug derivation
    canon = canonical_team_slug(s)
    if canon:
        tid = _ALIAS_TO_ID.get(canon)
        if tid is not None:
            _register_alias(s_lower, tid)
            return tid

    # 4. Fuzzy match
    candidates = list(_ALIAS_TO_ID.keys())
    close = get_close_matches(s_lower, candidates, n=1, cutoff=0.75)
    if close:
        tid = _ALIAS_TO_ID[close[0]]
        print(f"  🔧 Fuzzy resolved: '{s}' → '{TEAM_REGISTRY[tid].display_name}' (via '{close[0]}')")
        _register_alias(s_lower, tid)
        return tid

    # 5. Failure
    raise TeamLookupError(s, source, context)


def try_resolve_team(name_or_slug: str, *, source: str = "unknown") -> Optional[int]:
    """
    Like resolve_team() but returns None instead of raising.
    Use when you want to handle missing teams yourself.
    Prints a warning on failure.
    """
    try:
        return resolve_team(name_or_slug, source=source)
    except TeamLookupError:
        print(f"  ⚠️  Could not resolve team: '{name_or_slug}' (source: {source})")
        return None


def get_sr_slug(team_id: int) -> str:
    """
    Get the Sports-Reference slug for a team.

    Raises SourceSlugError if team_id not found.
    """
    rec = TEAM_REGISTRY.get(team_id)
    if rec is None:
        raise SourceSlugError(team_id, "sr")
    return rec.sr_slug


def get_torvik_slug(team_id: int) -> str:
    """
    Get the Torvik slug for a team.
    Falls back to SR slug if no Torvik-specific slug is registered
    (many teams use the same slug on both sites).

    Raises SourceSlugError if team_id not found.
    """
    rec = TEAM_REGISTRY.get(team_id)
    if rec is None:
        raise SourceSlugError(team_id, "torvik")
    return rec.source_slugs.get("torvik", rec.sr_slug)


def get_covers_slug(team_id: int) -> str:
    """
    Get the Covers.com slug for a team.

    Raises SourceSlugError if team_id not found AND no Covers slug registered.
    Falls back to SR slug with mascot-appended guess if no specific mapping.
    """
    rec = TEAM_REGISTRY.get(team_id)
    if rec is None:
        raise SourceSlugError(team_id, "covers")
    covers = rec.source_slugs.get("covers")
    if covers:
        return covers
    # Covers typically uses sr_slug format — return SR slug as reasonable default
    return rec.sr_slug


def get_display_name(team_id: int) -> str:
    """Get the canonical display name for a team."""
    rec = TEAM_REGISTRY.get(team_id)
    if rec is None:
        raise SourceSlugError(team_id, "display")
    return rec.display_name


def get_conference(team_id: int) -> str:
    """Get the conference for a team."""
    rec = TEAM_REGISTRY.get(team_id)
    if rec is None:
        raise SourceSlugError(team_id, "conference")
    return rec.conference


def get_team_record(team_id: int) -> Optional[TeamRecord]:
    """Get the full TeamRecord. Returns None if not found (no raise)."""
    return TEAM_REGISTRY.get(team_id)


# ═════════════════════════════════════════════════════════════════════════════
# BACKWARD COMPATIBILITY — drop-in replacements for old API
# ═════════════════════════════════════════════════════════════════════════════

def registry_team_id(name_or_slug: str) -> Optional[int]:
    """Drop-in replacement for old registry_team_id(). Returns None on miss."""
    return try_resolve_team(name_or_slug, source="registry_team_id_compat")


def registry_sr_slug(name_or_slug: str) -> Optional[str]:
    """Drop-in replacement for old registry_sr_slug()."""
    tid = try_resolve_team(name_or_slug, source="registry_sr_slug_compat")
    if tid is None:
        return None
    return get_sr_slug(tid)


def registry_conference(name_or_slug: str) -> Optional[str]:
    """Drop-in replacement for old registry_conference()."""
    tid = try_resolve_team(name_or_slug, source="registry_conference_compat")
    if tid is None:
        return None
    return get_conference(tid)


# ═════════════════════════════════════════════════════════════════════════════
# DIAGNOSTICS
# ═════════════════════════════════════════════════════════════════════════════

def audit_registry() -> dict:
    """
    Returns diagnostic info about the registry state.
    Call after build_registry() to verify integrity.
    """
    teams_without_torvik = [
        rec.display_name for rec in TEAM_REGISTRY.values()
        if "torvik" not in rec.source_slugs
    ]
    teams_without_covers = [
        rec.display_name for rec in TEAM_REGISTRY.values()
        if "covers" not in rec.source_slugs
    ]

    return {
        "total_teams": len(TEAM_REGISTRY),
        "total_aliases": len(_ALIAS_TO_ID),
        "avg_aliases_per_team": round(len(_ALIAS_TO_ID) / max(len(TEAM_REGISTRY), 1), 1),
        "teams_without_torvik_slug": len(teams_without_torvik),
        "teams_without_covers_slug": len(teams_without_covers),
        "registry_built": _REGISTRY_BUILT,
    }