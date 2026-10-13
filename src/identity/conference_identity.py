"""
Conference-identity adapter (Phase 2, Gate 2).

INVENTORY (Gate 2 investigation)

The notebook has no existing stable conference identifier at all --
conferences are represented purely as string keys of the `conferences`
dict (33 conferences, the same dict `ensure_team_registry()` already
consumes via `load_conferences()`), and every downstream consumer
(`TeamIdentity.conference`, `TEAM_CONFERENCE_LOOKUP`, `CONF_OVR_WEIGHT`,
etc.) just carries that string around. The one piece of REAL alias data
already in the notebook is `build_composite_radar_v9`'s own
`_CONF_CANONICAL` dict (cell 207) -- a hand-maintained mapping of ~70
colloquial/abbreviated conference spellings ("big 10", "b1g", "acc
conference", "a-10", ...) to one canonical display string per conference.
That dict is reused verbatim below as `CONFERENCE_ALIASES_V15` rather than
re-invented, since it is the actual alias data this codebase already
trusts.

DESIGN

`conference_id` is `stable_namespaced_id("conference", canonical_name)` --
deterministic, ordering-independent, and explicitly namespaced (so it can
never collide with a team_id, sportsbook_id, etc. even for a coincidentally
similar key string). Conference identity is intentionally decoupled from
team membership: this module never accepts or stores a list of teams --
"which teams currently belong to this conference" is a separate, mutable
fact (already tracked elsewhere, e.g. `TEAM_CONFERENCE_LOOKUP`,
`TeamIdentity.conference`) that this module does not encode into the id
itself, satisfying "distinguish current membership from conference
identity" and "do not encode a team's conference into team_id" (the
latter was already true of `TeamIdentity`/`stable_team_id`, which hash
only the SR slug -- confirmed by reading `stable_team_id`, not assumed).

No fuzzy matching: unknown conference strings are `not_found`, never
guessed via similarity scoring.
"""
from __future__ import annotations

from src.identity.contracts import ConferenceIdentity, UnresolvedIdentity
from src.identity.stable_id import stable_namespaced_id
from src.ingestion.load_data import load_conferences
from src.logging_config import get_logger

log = get_logger(__name__)

#: Verbatim from notebook cell 207's `_CONF_CANONICAL` (inside
#: `build_composite_radar_v9`) -- real, already-trusted alias data.
CONFERENCE_ALIASES_V15: dict[str, str] = {
    "big east conference": "Big East",
    "be": "Big East",
    "big 10": "Big Ten",
    "b10": "Big Ten",
    "b1g": "Big Ten",
    "big ten conference": "Big Ten",
    "atlantic coast": "ACC",
    "atlantic coast conference": "ACC",
    "big12": "Big 12",
    "b12": "Big 12",
    "big 12 conference": "Big 12",
    "big twelve": "Big 12",
    "southeastern": "SEC",
    "southeastern conference": "SEC",
    "a-10": "Atlantic 10",
    "a10": "Atlantic 10",
    "atlantic-10": "Atlantic 10",
    "atlantic ten": "Atlantic 10",
    "atlantic 10 conference": "Atlantic 10",
    "atlantic ten conference": "Atlantic 10",
    "mwc": "Mountain West",
    "mw": "Mountain West",
    "mountain west conference": "Mountain West",
    "west coast": "WCC",
    "west coast conference": "WCC",
    "pac12": "Pac-12",
    "p12": "Pac-12",
    "pac-12 conference": "Pac-12",
    "pac 12": "Pac-12",
    "american athletic conference": "American Athletic",
    "aac": "American Athletic",
    "the american": "American Athletic",
    "american": "American Athletic",
    "missouri valley": "MVC",
    "missouri valley conference": "MVC",
    "mid-american": "MAC",
    "mid-american conference": "MAC",
    "mid american": "MAC",
    "mid american conference": "MAC",
    "colonial athletic": "CAA",
    "colonial athletic association": "CAA",
    "coastal athletic": "CAA",
    "coastal athletic association": "CAA",
    "socon": "Southern",
    "southern conference": "Southern",
    "ivy": "Ivy League",
    "ivy league conference": "Ivy League",
    "horizon": "Horizon League",
    "hl": "Horizon League",
    "metro atlantic": "MAAC",
    "metro atlantic athletic": "MAAC",
    "metro atlantic athletic conference": "MAAC",
    "big south conference": "Big South",
    "bsth": "Big South",
    "america east conference": "America East",
    "ae": "America East",
    "asun conference": "ASUN",
    "atlantic sun": "ASUN",
    "atlantic sun conference": "ASUN",
    "a-sun": "ASUN",
    "patriot": "Patriot League",
    "pl": "Patriot League",
    "big west conference": "Big West",
    "bw": "Big West",
    "summit": "Summit League",
    "the summit league": "Summit League",
    "big sky conference": "Big Sky",
    "bsky": "Big Sky",
    "ohio valley": "OVC",
    "ohio valley conference": "OVC",
    "sun belt conference": "Sun Belt",
    "sbc": "Sun Belt",
    "sunbelt": "Sun Belt",
    "northeast": "NEC",
    "northeast conference": "NEC",
    "southland conference": "Southland",
    "sl": "Southland",
    "western athletic": "WAC",
    "western athletic conference": "WAC",
    "cusa": "Conference USA",
    "c-usa": "Conference USA",
    "conf usa": "Conference USA",
    "southwestern athletic": "SWAC",
    "southwestern athletic conference": "SWAC",
    "mid-eastern athletic": "MEAC",
    "mid-eastern athletic conference": "MEAC",
    "mid eastern athletic": "MEAC",
    "mid eastern athletic conference": "MEAC",
}

_REGISTRY_READY = False
_CANONICAL_NAMES: set[str] = set()


def ensure_conference_registry() -> None:
    """
    Populates the known-conference universe from `load_conferences()` (the
    same 33-conference dict Phase 2A's team registry already uses) --
    idempotent, safe to call from every entry point.
    """
    global _REGISTRY_READY, _CANONICAL_NAMES
    if _REGISTRY_READY:
        return
    conferences = load_conferences()
    _CANONICAL_NAMES = set(conferences.keys())
    _REGISTRY_READY = True


def _conference_identity(canonical_name: str) -> ConferenceIdentity:
    return ConferenceIdentity(
        conference_id=stable_namespaced_id("conference", canonical_name),
        canonical_name=canonical_name,
    )


def resolve_conference_identity(query: str, *, source: str = "conference") -> ConferenceIdentity | UnresolvedIdentity:
    """
    Resolve a conference name/abbreviation/alias to a canonical
    `ConferenceIdentity`. Deterministic only: exact match against the real
    33-conference universe, then exact match (case-insensitive) against
    `CONFERENCE_ALIASES_V15`. No fuzzy matching -- an unrecognized string
    is `not_found`, never guessed.
    """
    ensure_conference_registry()

    if not query or not isinstance(query, str):
        return UnresolvedIdentity(query=str(query), source=source, reason="not_found")

    s = query.strip()
    if s in _CANONICAL_NAMES:
        return _conference_identity(s)

    alias_target = CONFERENCE_ALIASES_V15.get(s.lower())
    if alias_target and alias_target in _CANONICAL_NAMES:
        return _conference_identity(alias_target)

    # A canonical alias target not itself present in the current 33-conference
    # universe (e.g. a conference that has since been realigned/dissolved) --
    # still a real, known name, just not a *current* member of the universe.
    if alias_target:
        return _conference_identity(alias_target)

    return UnresolvedIdentity(query=query, source=source, reason="not_found")


def find_conference_alias_collisions() -> dict[str, list[str]]:
    """
    Diagnostic: aliases in `CONFERENCE_ALIASES_V15` whose canonical targets
    collide with a *different* alias's target under simple case-folding --
    i.e. the same evidence this migration's team-identity collision checks
    already look for, applied to conferences. Empty means no collision
    found in the current alias table.
    """
    by_lower_target: dict[str, set[str]] = {}
    for alias, target in CONFERENCE_ALIASES_V15.items():
        by_lower_target.setdefault(target.lower(), set()).add(target)

    return {
        lower: sorted(targets)
        for lower, targets in by_lower_target.items()
        if len(targets) > 1
    }
