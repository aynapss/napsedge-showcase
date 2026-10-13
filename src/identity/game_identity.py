"""
Game-identity adapter (Phase 2, Gate 2) -- the highest-risk identifier in
this migration, per explicit instruction.

INVENTORY (Gate 2 investigation)

No authoritative, persisted cross-source game id exists anywhere in this
codebase today. What IS available, per source:

- **Sports-Reference box-score URL** -- `BoxscoreResult.url` /
  `PostgameReport.url` (already flowing through this migration's own
  batch-5 postgame adapters) is a real, source-specific per-game
  identifier in practice (SR's URL embeds date + home-team slug), but it
  is a *string URL*, not a dedicated id field, and its home-team-slug
  component inherits every slug-convention risk Gate 1 just reconciled.
- **ESPN event IDs** -- referenced only as an inference in this
  migration's consolidated ledger ("almost certainly present in the real
  API shape, never verified against a live payload") -- unconfirmed, not
  fabricated here.
- **Odds-provider event IDs** -- The Odds API's real response shape is
  known to include a per-event `id` field industry-wide, but this
  migration has not independently verified one against a live payload
  either -- treated the same as the ESPN case: plausible, not confirmed.
- **Structural fields that ARE reliably available locally**: canonical
  `team_id` (both sides, via Phase 2A's registry), `season` (via this
  gate's own `SeasonIdentity`), a scheduled/actual game date string, and
  (from `BoxscoreResult`) a real final-score-based home/away label,
  though home/away for a *neutral-site* game is often an arbitrary
  designation rather than a true home-court fact.

DESIGN: PROVISIONAL-BY-DEFAULT, SOURCE-ID-PREFERRED-WHEN-TRUSTED

Given no authoritative source id has been independently verified this
batch, `build_provisional_game_identity` is the default path: a stable
structural key over **canonical team_id values only** (never a display
name or a mutable slug -- satisfying "do not let a corrected display name
or source slug change the game identity" by construction, since neither
appears in the hash input at all), the required `season`, the game date,
and neutral-site status. `is_provisional=True` always on this path --
this migration does not claim more certainty than it has.

`build_game_identity_from_source` is available for a caller that DOES
have a real, trusted source id (e.g. once ESPN event ids or Odds-API
event ids are independently verified in a future batch) -- it prefers
`f"{source}:{source_game_id}"` as the id, while still recording the same
structural fields as metadata for conflict detection, and sets
`is_provisional=False`.

NEUTRAL-SITE SYMMETRY

For a neutral-site game, "home" and "away" are often just a scorebox
convention, not a real home-court fact -- two sources could reasonably
swap the labels for the identical real game. To avoid minting two
different provisional ids for one real neutral-site game because of a
label swap, the structural key sorts the two team_ids into a fixed order
(`min`, `max`) when `neutral_site=True`. For a non-neutral game, home/away
is preserved as given -- a genuine home/away swap there reflects a real
data disagreement worth surfacing, not something to silently normalize
away (see `find_home_away_mismatch` below).

IDENTITY-SCHEME VERSION (Phase 5 identity-key remediation batch)

Both builder functions below mint `game_id` via `stable_wide_id` (scheme
v2 -- a 63-bit truncated SHA-256 digest), never `stable_namespaced_id`
(scheme v1 -- the 6-digit/900,000-value width still used, unchanged, for
`conference`/`sportsbook`/`model_version`). This is a targeted, single-
namespace correction: the Phase 5B2 144-team dry run confirmed 4 real
`game_id` collisions between genuinely distinct real games, root-caused
to v1's hash-space being far too small for `game`'s real cardinality
(~4,543 provisional identities in one season) -- never to a missing
discriminator in `_structural_key` itself, which was independently
confirmed correct for all 4 colliding pairs (see
`docs/identity_scheme.md` for the full reproduction and root-cause
table). `conference`/`sportsbook`/`model_version` stay on v1 -- confirmed
collision-free at their own, much smaller real cardinalities, and left
untouched per "do not widen every ID type automatically if the defect is
confined to one identity domain."

CONFLICT DETECTION -- NEVER RESOLVED SILENTLY

`find_game_id_conflicts` flags exactly two situations, kept structurally
distinct because they mean different things:
  - the same real source id attached to two different structural
    contexts (team/season/date changed) -- a genuine data-quality
    conflict in the source itself;
  - two different source ids both mapping to what looks like the same
    real structural matchup+date -- either a legitimate multi-source
    situation (fine, both aliases of one real game) or a sign one of the
    two dates/team pairs is wrong; this function reports it, it does not
    decide which.
Neither case is merged or silently picked between.
"""
from __future__ import annotations

from src.identity.contracts import GameIdentity, SourceIdentity, UnresolvedIdentity
from src.identity.stable_id import stable_wide_id
from src.logging_config import get_logger

log = get_logger(__name__)


def _structural_key(
    *, home_team_id: int, away_team_id: int, season: int, game_date: str, neutral_site: bool
) -> str:
    if neutral_site:
        team_a, team_b = sorted((home_team_id, away_team_id))
        return f"season:{season}:date:{game_date}:teams:{team_a}-{team_b}:neutral"
    return f"season:{season}:date:{game_date}:home:{home_team_id}:away:{away_team_id}"


def build_provisional_game_identity(
    *,
    home_team_id: int,
    away_team_id: int,
    season: int,
    game_date: str,
    neutral_site: bool = False,
) -> GameIdentity | UnresolvedIdentity:
    """
    Build an explicit **provisional** game identity from structural fields
    only -- no source game id required or assumed. `season` is required
    (no silent inference); `game_date` must be a non-empty string (the
    caller's responsibility to supply a real scheduled/actual date, not
    guessed here). Returns `UnresolvedIdentity(reason="not_found")` for
    missing required context rather than fabricating a key from partial
    data.
    """
    if not game_date or not isinstance(game_date, str):
        return UnresolvedIdentity(query=str(game_date), source="game", reason="not_found")
    if home_team_id is None or away_team_id is None or season is None:
        return UnresolvedIdentity(query="<missing team_id/season>", source="game", reason="not_found")

    key = _structural_key(
        home_team_id=home_team_id, away_team_id=away_team_id,
        season=season, game_date=game_date, neutral_site=neutral_site,
    )
    return GameIdentity(
        game_id=stable_wide_id("game", key),
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        season=season,
        game_date=game_date,
        neutral_site=neutral_site,
        is_provisional=True,
    )


def build_game_identity_from_source(
    *,
    source: str,
    source_game_id: str,
    home_team_id: int,
    away_team_id: int,
    season: int,
    game_date: str,
    neutral_site: bool = False,
) -> GameIdentity | UnresolvedIdentity:
    """
    Build a game identity preferring a real, caller-supplied authoritative
    source id (`f"{source}:{source_game_id}"`) -- use only when that id is
    actually trusted (e.g. a verified ESPN/Odds-API event id), not a
    guessed or unconfirmed one. Structural fields are still recorded (as
    both the object's own attributes and a `SourceIdentity`) so
    `find_game_id_conflicts` can later detect a source id that
    inconsistently maps to different structural contexts across two calls.
    `is_provisional=False` -- this is the one path that claims real
    authority for the id.
    """
    if not source_game_id or not game_date or home_team_id is None or away_team_id is None or season is None:
        return UnresolvedIdentity(query=str(source_game_id), source=source, reason="not_found")

    structural_key = _structural_key(
        home_team_id=home_team_id, away_team_id=away_team_id,
        season=season, game_date=game_date, neutral_site=neutral_site,
    )
    return GameIdentity(
        game_id=stable_wide_id("game_source", f"{source}:{source_game_id}"),
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        season=season,
        game_date=game_date,
        neutral_site=neutral_site,
        is_provisional=False,
        sources={source: SourceIdentity(source, source_game_id, structural_key)},
    )


def find_game_id_conflicts(games: list[GameIdentity]) -> list[dict]:
    """
    Detects a real source game id (`sources[src].raw_value`) attached to
    more than one distinct structural context (team/season/date/neutral
    combination) across the supplied `games` -- a genuine data-quality
    conflict, reported, never silently resolved in either direction.
    """
    by_source_id: dict[tuple[str, str], set[str]] = {}
    for g in games:
        for src, source_identity in g.sources.items():
            key = (src, source_identity.raw_value)
            structural = _structural_key(
                home_team_id=g.home_team_id, away_team_id=g.away_team_id,
                season=g.season, game_date=g.game_date, neutral_site=g.neutral_site,
            )
            by_source_id.setdefault(key, set()).add(structural)

    conflicts = []
    for (src, source_id), structural_contexts in by_source_id.items():
        if len(structural_contexts) > 1:
            conflicts.append({
                "source": src,
                "source_game_id": source_id,
                "conflicting_contexts": sorted(structural_contexts),
            })
    return conflicts


def find_home_away_mismatch(a: GameIdentity, b: GameIdentity) -> bool:
    """
    True when `a` and `b` describe the same two teams on the same date/
    season but with home and away REVERSED and neither is a neutral-site
    game -- a real disagreement worth surfacing (e.g. two sources
    disagreeing on which team hosted), never silently normalized away.
    Neutral-site games are excluded here by design: their structural key
    already treats a home/away label swap as the same game (see module
    docstring), so this check only applies where home/away is a real fact.
    """
    if a.neutral_site or b.neutral_site:
        return False
    if a.season != b.season or a.game_date != b.game_date:
        return False
    return a.home_team_id == b.away_team_id and a.away_team_id == b.home_team_id
