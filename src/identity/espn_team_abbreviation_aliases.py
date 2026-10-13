"""
Phase 5B3: reviewed, evidence-backed crosswalk for ESPN's short
team-abbreviation codes (the `Team` column in
`TeamDump/ESPN/espn_full_player_stats.csv`, e.g. `"BYU"`, `"ECU"`,
`"ARK"`) -- a third team-naming convention, structurally distinct from
both `src.identity.espn_identity` (full "<school> <mascot>" names from
ESPN's team-list/logo page) and Sports-Reference's own directory slugs.

CONFIRMED BASELINE (unchanged, reproduced this batch): of 365 distinct
real codes in the local fixture, only 84 (23.0%) resolve through the
existing, exact/deterministic `resolve_team_identity()` -- confirmed
identical to the Phase 3/Phase 4B findings already on record in
`docs/local_data_inventory.md` and
`src.transformations.espn_allstats_transformation`.

EVIDENCE METHOD FOR THE 231 NEW ENTRIES BELOW (roster-overlap evidence,
never fuzzy string matching, never "looks similar")

For every one of the 281 codes that do NOT resolve via the exact path,
this batch cross-referenced that code's own ESPN player names (from the
same real CSV row) against every real local `TeamDump/CBB/<slug>/
Roster_2026.xlsx` file's own `Player` column (canonical-keyed via the
already-established `canonical_player_key_v15`, and each roster's own
directory slug resolved to a real canonical `team_id` via the existing
`resolve_team_identity`, never assumed from the slug string itself).

An ESPN code became a **reviewed alias** here only when:
  1. At least one real local team roster shared **2 or more** of that
     code's ESPN players (by canonical player key) -- a single shared
     name was explicitly rejected as too weak (confirmed real false
     positive found and rejected during this batch's own analysis:
     `"ARST"` shares exactly one player name, `"Chandler Jackson"`, with
     Milwaukee's roster -- a genuine same-name coincidence between two
     different real people; Arkansas State itself has no local roster
     file at all, so this evidence source could never have produced the
     correct team for that code, and correctly leaves it unresolved
     rather than silently guessing a wrong one).
  2. No other team's roster shared 2+ players with the same code (a
     dominant single candidate, not a close multi-team split). Two
     apparent "ambiguous" cases surfaced during analysis were resolved
     without guessing once verified as the SAME real canonical team under
     two different local directory-slug spellings (`"saint-joseph's"`/
     `"saint-josephs"` and `"miami-(oh)"`/`"miami-oh"` both resolve to one
     canonical `team_id` each) -- grouping evidence by canonical
     `team_id`, not raw directory-slug string, made these correctly
     unambiguous. Zero genuine multi-team ambiguous cases were found in
     this batch's real data.

Result: 231 reviewed aliases added, 0 genuinely ambiguous, 50 remain
unresolved (no local roster shares 2+ real players with that code, most
commonly because the target team has no local `Roster_2026.xlsx` at all
-- e.g. `"ARST"` (Arkansas State), `"HARV"` (Harvard), `"BING"`
(Binghamton) -- an absence of local evidence, not a defect in this
method). Post-remediation team-code resolution: 84 + 231 = 315/365
(86.3%), up from the 84/365 (23.0%) baseline.

THIS MODULE NEVER MODIFIES THE SHARED RESOLVER

`resolve_team_identity()` and `src/slug_utils.py`'s in-memory
`TEAM_REGISTRY`/`_ALIAS_TO_ID` are completely unmodified by this batch
(confirmed by `git diff --stat`) -- adding a row to the DB `team_aliases`
table would NOT change what that shared, in-memory resolver returns at
runtime (it never reads that table). This module is instead an
**adapter-local fallback**, consulted only by
`src.pipeline.espn_import_adapter`, exactly mirroring the existing,
established "reviewed crosswalk supplied as adapter input, never baked
into the shared resolver" pattern already used for
`known_espn_athlete_id_map` in
`src.transformations.espn_allstats_transformation`. Every applied
reviewed alias is still persisted as an auditable `team_aliases` row
(`source="espn_stat_abbreviation"`) by the import adapter, so the
evidence trail is real and queryable -- it just never feeds back into the
live resolution path.

PHASE 5B6 ADDENDUM -- independent reproduction, extended evidence search,
and provenance typing (2026-09-08)

All four checkpoint figures (365 unique codes, 84 baseline, 231 reviewed,
50 unresolved) were independently reproduced exactly against the real
local fixture before any change was made -- no discrepancy this time
(unlike an earlier batch's own player-gamelog identity audit, which did
find one). Three integrity checks were run and now hold, and are locked
in by regression test: (1) no reviewed-alias code also resolves via the
baseline exact path (would indicate a stale/redundant entry -- none
found); (2) no two reviewed-alias codes map to the same `team_id`
unintentionally (none found -- every one of the 231 targets is currently
unique); (3) every reviewed-alias `team_id` still has a real
`TEAM_REGISTRY` record (none dangling).

This batch's own extended evidence search -- re-running the exact same
>=2-shared-player roster-overlap method against the 50 remaining codes,
but this time ALSO checking the newly-available (Phase 5B4/5B5) local
per-player game-log filenames as a second roster-equivalent evidence
source, not just `Roster_2026.xlsx` -- found **zero** new qualifying
(>=2 shared player) candidates. 45 of the 50 have **zero** local
player-name overlap of any kind with any local team; 5 have exactly
**one** coincidental shared player name (correctly below this module's
own established 2-match evidence bar): the already-documented `"ARST"`/
Milwaukee case, plus 4 newly-confirmed-still-too-weak cases this batch's
own search surfaced (`"EVAN"`/Samford, `"LAF"`/Georgetown, `"LONG"`/
Texas-El Paso, `"MORG"`/North Carolina -- each a single same-name
coincidence, not a real connection). None were promoted to a reviewed
alias. **This batch adds zero new reviewed aliases** -- every remaining
code was independently re-verified, with a broader evidence search than
Phase 5B3 had available, to still lack the evidence this module requires.

One genuinely new, more precise classification was found: `"UNCA"` is not
a plain "not found" case like the other 49 -- `resolve_team_identity`
itself already reports it as **ambiguous** among 3 real named candidates
(North Carolina, North Alabama, UNC Wilmington), confirmed by direct
reproduction. No local roster or game-log evidence connects `"UNCA"`'s
own ESPN players to any of the three (zero overlap with all three),
so it correctly stays unresolved -- but its underlying reason is
"ambiguous," not "not_found," and `ESPN_TEAM_RESOLUTION_METHODS` already
reserved an `"ambiguous"` value for exactly this shape of case (dead/
unreachable for team resolution until this batch). `resolve_espn_team_
abbreviation_with_provenance` below now surfaces this distinction; the
plain `resolve_espn_team_abbreviation` wrapper's return type is
unchanged, so no existing caller is affected.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.identity.contracts import TeamIdentity, UnresolvedIdentity
from src.identity.team_identity import resolve_team_identity

#: {ESPN abbreviation code: canonical team_id}. See module docstring for
#: the exact evidence method. Every value here was independently
#: confirmed reachable via >=2 real, distinct ESPN-player-to-local-roster
#: matches, with no competing candidate team at the same evidence
#: strength.
REVIEWED_ESPN_ABBREVIATION_ALIASES: dict[str, int] = {
    "AF": 273579, "AKR": 366272, "ALA": 469722, "ALCN": 423016, "ALST": 877528,
    "AMCC": 646448, "AMER": 461043, "APSU": 573202, "ARIZ": 430849, "ARK": 749597,
    "ASU": 932238, "AUB": 455470, "BALL": 999263, "BAY": 499144, "BC": 188667,
    "BELL": 135885, "BOIS": 161232, "BRAD": 264257, "BRY": 847639, "BU": 520942,
    "BUCK": 576037, "BUF": 547225, "BUT": 113776, "CAL": 663064, "CAM": 571643,
    "CAN": 686675, "CARK": 542348, "CCU": 602847, "CHSO": 542457, "CIN": 571710,
    "CIT": 600527, "CLE": 388564, "CLEM": 217399, "CLT": 406436, "CMU": 644495,
    "COFC": 298572, "COLG": 352620, "COLO": 952264, "COLU": 736783, "CONN": 245299,
    "COR": 193443, "CP": 214724, "CSU": 599252, "CSUB": 976487, "DART": 952114,
    "DAV": 343943, "DAY": 763189, "DEL": 315825, "DEP": 408115, "DETM": 789240,
    "DREX": 496561, "DRKE": 775653, "DSU": 772184, "DUQ": 767060, "ECU": 602152,
    "EMU": 186247, "EWU": 115212, "FAIR": 858088, "FGCU": 128255, "FLA": 549811,
    "FOR": 274328, "FRES": 509232, "FUR": 468884, "GASO": 891836, "GB": 272904,
    "GMU": 594713, "GONZ": 828140, "GRAM": 769447, "GT": 309214, "GTWN": 703224,
    "GWEB": 928516, "HAMP": 250462, "HAW": 542276, "HC": 793859, "HCU": 395948,
    "HOF": 524678, "HOU": 617944, "HOW": 426092, "IDHO": 363945, "IDST": 321508,
    "ILL": 429258, "ILST": 566276, "INST": 633910, "ISU": 156514, "IU": 460807,
    "IUIN": 593444, "JMU": 623519, "JOES": 954821, "JXST": 494988, "KENN": 793493,
    "KENT": 943399, "KU": 896362, "L-MD": 283557, "LAS": 468989, "LBSU": 667579,
    "LEH": 270943, "LEM": 586100, "LIB": 996703, "LIP": 600730, "LOU": 598355,
    "LT": 955548, "LUC": 924232, "M-OH": 529692, "MARQ": 127686, "MASS": 993782,
    "MCN": 539023, "MD": 196098, "ME": 477717, "MEM": 866567, "MER": 694071,
    "MERC": 871288, "MIA": 843365, "MICH": 526077, "MILW": 968044, "MINN": 620065,
    "MISS": 319176, "MIZ": 200279, "MONM": 248053, "MONT": 559561, "MORE": 817131,
    "MOST": 845276, "MRMK": 699128, "MRSH": 877888, "MRST": 773720, "MSM": 271931,
    "MSST": 951113, "MTST": 232442, "MTSU": 531428, "MUR": 367877, "MVSU": 971511,
    "NCAT": 742178, "ND": 956787, "NE": 250549, "NEB": 439552, "NEV": 185503,
    "NIA": 620616, "NICH": 330156, "NIU": 810115, "NKU": 929127, "NMSU": 384664,
    "NU": 394237, "NWST": 928835, "OAK": 645192, "OKST": 451404, "OMA": 870072,
    "ORE": 184245, "ORST": 857391, "OU": 271629, "PAC": 727966, "PEPP": 692249,
    "PFW": 944481, "PORT": 567346, "PRES": 504085, "PRIN": 175502, "PROV": 386358,
    "PRST": 725042, "PSU": 438866, "PUR": 924691, "PV": 615608, "QUC": 403176,
    "QUIN": 864477, "RGV": 281745, "RICH": 977583, "RMU": 583310, "RUTG": 847439,
    "SAC": 247859, "SAM": 376957, "SBU": 905884, "SC": 545648, "SCST": 568521,
    "SCU": 434190, "SEA": 733701, "SF": 862894, "SHU": 163855, "SIE": 860107,
    "SJSU": 173715, "SJU": 923441, "SLU": 775230, "SMC": 878553, "STAN": 476265,
    "STBK": 233543, "STMN": 699550, "STO": 656050, "SYR": 772030, "TA&M": 217951,
    "TAR": 383950, "TEM": 301533, "TENN": 397850, "TEX": 857248, "TLSA": 414834,
    "TNST": 293046, "TNTC": 376921, "TOL": 195295, "TOW": 726142, "TTU": 712031,
    "TULN": 833096, "UALB": 934753, "UCI": 706997, "UGA": 591539, "UK": 157913,
    "UML": 649643, "UNCO": 627088, "UNF": 912316, "UNH": 277706, "UNI": 903704,
    "UNM": 711786, "USA": 922312, "USF": 134085, "USM": 124442, "USU": 908608,
    "UTM": 632046, "UTU": 151173, "UVA": 456978, "UVM": 752313, "VAL": 990577,
    "VAN": 472603, "VILL": 523432, "WAG": 391260, "WASH": 217860, "WEB": 290227,
    "WGA": 117174, "WICH": 220551, "WIN": 931258, "WIS": 426798, "WKU": 514850,
    "WMU": 694863, "WOF": 820587, "WRST": 648032, "WVU": 561646, "WYO": 447563,
    "XAV": 765996,
}

#: 50 codes with no qualifying local roster evidence (see module
#: docstring) -- listed explicitly so a future batch with more local
#: roster coverage (or a genuine external crosswalk) knows exactly what
#: remains, rather than re-deriving this set. Never resolved by guessing.
UNRESOLVED_ESPN_ABBREVIATIONS: tuple[str, ...] = (
    "AAMU", "APP", "ARST", "BCU", "BEL", "BING", "BRWN", "CHST", "COPP", "CREI",
    "DEN", "ETAM", "EVAN", "GAST", "HALL", "HARV", "JAX", "JKST", "KC", "LAF",
    "LAM", "LIN", "LONG", "LR", "MAN", "MORG", "NHVN", "NORF", "ODU", "RAD",
    "RID", "SDAK", "SDST", "SELA", "SFPA", "SOU", "SPU", "STET", "TXSO", "TXST",
    "UCD", "UCR", "UL", "ULM", "UNCA", "UNT", "UPST", "W&M", "WSU", "YSU",
)


#: Explicit resolution-method vocabulary for this module's typed result --
#: kept exactly aligned with the already-persisted, already-CHECK-
#: constrained `ESPN_TEAM_RESOLUTION_METHODS` (`src/db/enums.py`) so this
#: module's own `.method` value can be written straight to
#: `EspnSourceObservation.team_resolution_method` with no translation
#: layer and no schema/migration change. `"exact_alias"` names the
#: baseline `resolve_team_identity()` path (kept as-is, not renamed, to
#: match that already-persisted vocabulary exactly).
EspnTeamCodeResolutionMethod = Literal["exact_alias", "reviewed_alias", "ambiguous", "unresolved"]


@dataclass(frozen=True)
class EspnTeamCodeResolution:
    """
    Typed, provenance-carrying result of resolving one ESPN abbreviation
    code. Added this batch to replace the import adapter's own previous
    re-derivation of "was this a reviewed alias?" via a private re-import
    of `REVIEWED_ESPN_ABBREVIATION_ALIASES` on every row -- this module now
    reports its own resolution method directly, at the one place that
    actually knows it.
    """
    code: str
    identity: TeamIdentity | UnresolvedIdentity
    method: EspnTeamCodeResolutionMethod

    @property
    def team_id(self) -> int | None:
        return self.identity.team_id if isinstance(self.identity, TeamIdentity) else None


def resolve_espn_team_abbreviation_with_provenance(
    code: str, *, source: str = "espn_stat_abbreviation",
) -> EspnTeamCodeResolution:
    """
    The full, provenance-carrying resolution: tries the existing exact/
    deterministic `resolve_team_identity()` first (the 84/365 baseline,
    unchanged), then this batch's own reviewed crosswalk, and -- only if
    neither resolves -- reports whether the underlying baseline reason was
    genuinely `"ambiguous"` (multiple real named candidates, e.g. `"UNCA"`)
    or a plain `"unresolved"` (`"not_found"`, no candidate at all). Never
    fuzzy-matches and never invents an entry beyond
    `REVIEWED_ESPN_ABBREVIATION_ALIASES`.
    """
    baseline = resolve_team_identity(code, source=source)
    if isinstance(baseline, TeamIdentity):
        return EspnTeamCodeResolution(code=code, identity=baseline, method="exact_alias")

    team_id = REVIEWED_ESPN_ABBREVIATION_ALIASES.get(code.strip().upper() if code else code)
    if team_id is not None:
        # Resolve the *team_id* back to a full TeamIdentity via its own
        # canonical slug -- never fabricate a TeamIdentity by hand -- so the
        # result carries the same real display_name/sources shape as every
        # other resolution path in this codebase. `resolve_team_identity`
        # above already called `ensure_team_registry()`, so TEAM_REGISTRY is
        # guaranteed populated here.
        from src import slug_utils

        record = slug_utils.TEAM_REGISTRY.get(team_id)
        if record is not None:
            resolved = resolve_team_identity(record.sr_slug, source=source)
            if isinstance(resolved, TeamIdentity):
                return EspnTeamCodeResolution(code=code, identity=resolved, method="reviewed_alias")

    # No reviewed alias could resolve it -- classify by the baseline's own
    # reason, never inventing a new evidence-free ambiguity claim of this
    # module's own (see the module docstring's "UNCA" finding).
    if isinstance(baseline, UnresolvedIdentity) and baseline.reason == "ambiguous":
        return EspnTeamCodeResolution(code=code, identity=baseline, method="ambiguous")
    return EspnTeamCodeResolution(code=code, identity=baseline, method="unresolved")


def resolve_espn_team_abbreviation(code: str, *, source: str = "espn_stat_abbreviation") -> TeamIdentity | UnresolvedIdentity:
    """
    The adapter-local team-resolution entry point for ESPN's short
    abbreviation codes. Preserves the original `TeamIdentity |
    UnresolvedIdentity` return contract for existing callers -- delegates
    to :func:`resolve_espn_team_abbreviation_with_provenance` and returns
    just its `.identity`.
    """
    return resolve_espn_team_abbreviation_with_provenance(code, source=source).identity
