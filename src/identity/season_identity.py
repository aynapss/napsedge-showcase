"""
Season-identity adapter (Phase 2, Gate 2).

CONVENTION, MADE EXPLICIT (Gate 2 requirement: "document whether 2026
means the 2025-26 season ending in 2026")

Confirmed by consistent usage across every real fixture and adapter this
migration has touched (AllStats workbooks named
``{slug}_{season}_AllStats.xlsx``, ``net_rankings_2026.csv``, every
`PlayerIdentity`/`TeamIdentity` adapter's own `season: int` parameter):
**this codebase's `season` integer is always the season's ENDING year.**
``season=2026`` means the 2025-26 season (the one whose NCAA tournament is
in March 2026) -- never the calendar year in which the season *began*.
This module makes that convention an explicit, testable contract instead
of an implicit assumption every caller has to already know.

DESIGN

`SeasonIdentity.season_id` is simply the ending-year integer itself --
already deterministic and independent of any ordering, so no hashing is
needed (unlike conference/sportsbook/model-version/game, which hash a
string key). `resolve_season_identity` validates the input is a plausible
ending year (rejects obviously malformed values -- negative, absurdly
large, or given as a 2-digit shorthand) rather than silently accepting
garbage. `parse_season_label` gives callers an explicit, tested conversion
from a human-readable "2025-26"-style label to the canonical ending-year
int, specifically so a caller with a labeled season string has a real
parsing path instead of guessing which half of the label the existing
integer convention wants.
"""
from __future__ import annotations

import re

from src.identity.contracts import SeasonIdentity, UnresolvedIdentity
from src.logging_config import get_logger

log = get_logger(__name__)

#: A generous but real sanity bound -- NCAA D1 basketball predates this
#: codebase by decades, and no fixture in this migration references a
#: season more than a couple of years past "now" (today per this session:
#: 2026-08-27, i.e. the 2026-27 season, season_id 2027, is a legitimate
#: near-future value; anything wildly beyond that is almost certainly a
#: malformed/mistyped input, not a real season).
_MIN_PLAUSIBLE_SEASON = 1900
_MAX_PLAUSIBLE_SEASON = 2100

_LABEL_PATTERN = re.compile(r"^(\d{4})-(\d{2})$")


def resolve_season_identity(value: int | str, *, source: str = "season") -> SeasonIdentity | UnresolvedIdentity:
    """
    Resolve a season value to a canonical `SeasonIdentity`, under the
    ending-year convention documented above.

    Accepts a plain `int` (the existing, already-widespread calling
    convention throughout this migration -- "preserve compatibility with
    existing integer inputs") or a numeric string. Does **not** accept a
    "2025-26"-style label here -- use `parse_season_label` explicitly for
    that, so a caller is never silently guessing which convention applies.
    Malformed years (out of the plausible range, non-integer garbage) are
    `not_found`, never silently coerced.
    """
    if isinstance(value, bool):  # bool is an int subclass -- reject explicitly
        return UnresolvedIdentity(query=str(value), source=source, reason="not_found")

    try:
        end_year = int(value)
    except (TypeError, ValueError):
        return UnresolvedIdentity(query=str(value), source=source, reason="not_found")

    if isinstance(value, str) and not value.strip().lstrip("-").isdigit():
        return UnresolvedIdentity(query=str(value), source=source, reason="not_found")

    if not (_MIN_PLAUSIBLE_SEASON <= end_year <= _MAX_PLAUSIBLE_SEASON):
        return UnresolvedIdentity(query=str(value), source=source, reason="not_found")

    return SeasonIdentity(
        season_id=end_year,
        start_year=end_year - 1,
        end_year=end_year,
        label=f"{end_year - 1}-{str(end_year)[-2:]}",
    )


def parse_season_label(label: str, *, source: str = "season") -> SeasonIdentity | UnresolvedIdentity:
    """
    Parse an explicit "2025-26"-style season label into the canonical
    ending-year `SeasonIdentity` -- the tested conversion path this
    module's docstring calls for, so a caller holding a labeled string
    never has to guess whether the bare 4-digit start year or a derived
    end year is what this codebase's `season: int` convention wants.

    Requires the two-digit second half to be exactly the first half's
    year plus one (rejects e.g. "2025-27" or "2025-25" as malformed rather
    than silently accepting a non-adjacent pair).
    """
    if not isinstance(label, str):
        return UnresolvedIdentity(query=str(label), source=source, reason="not_found")

    match = _LABEL_PATTERN.match(label.strip())
    if not match:
        return UnresolvedIdentity(query=label, source=source, reason="not_found")

    start_year = int(match.group(1))
    end_suffix = match.group(2)
    expected_end_year = start_year + 1

    if str(expected_end_year)[-2:] != end_suffix:
        return UnresolvedIdentity(query=label, source=source, reason="not_found")

    return resolve_season_identity(expected_end_year, source=source)
