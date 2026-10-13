"""
Phase 7 Slice 3A: pure presentation adapter for `src.services.player`'s
existing `PlayerRawStatisticsResult` -- no database, no identity
resolution, no network, and no import of `src.db`/`sqlalchemy`/
`src.ovr_model`/notebook-execution machinery anywhere in this module.
The adapter only ever reads the fields
`src.services.contracts.PlayerRawStatisticsResult`/`PlayerStatRecord`
already carry.

A Player-not-found or a service-layer exception is never seen here as
an exception -- `get_player_raw_statistics` never raises for an unknown
`player_id` (it returns `outcome="not_found"`); any other exception a
caller's own service call raises is mapped separately by the existing,
unmodified `src.consumers.errors.present_error`, exactly matching the
Team/Rankings/Injury precedent (an adapter only ever receives an
already-computed result, never an exception).
"""
from __future__ import annotations

from dataclasses import fields as _dataclass_fields

from src.consumers.contracts import ConsumerPresentation, PresentationField
from src.services.contracts import PlayerRawStatisticsResult, PlayerStatRecord

_NOT_FOUND_MESSAGE = "No player matched that lookup."
_KNOWN_EMPTY_SUMMARY = "No statistics available for this player/season."

#: Every `PlayerStatRecord` field except `player_key` -- an internal,
#: lowercased matching key, redundant with `source_player_name`, already
#: excluded at the Phase 6 API boundary (`src.api.schemas.PlayerStatResponse`,
#: confirmed by `tests/api/test_players.py`). This is the maximum
#: external presentation boundary this adapter mirrors exactly -- no
#: more, no fewer. Derived from the dataclass's own field order via
#: `dataclasses.fields` (rather than a second, hand-maintained literal
#: list) so this ordering can never silently drift from the authoritative
#: contract if a field is ever added or reordered there.
_EXCLUDED_STAT_FIELDS = frozenset({"player_key"})
_STAT_FIELD_ORDER: tuple[str, ...] = tuple(
    field.name for field in _dataclass_fields(PlayerStatRecord) if field.name not in _EXCLUDED_STAT_FIELDS
)


def _value_to_str(value: object) -> str | None:
    """`None` stays `None` (never coerced to "unavailable" or empty string here -- that is a rendering decision, not a presentation-contract one); a genuine stored `0`/`0.0`/`False` is preserved via plain `str()`, never treated as missing. No rounding, no normalization, no recalculation -- the value is exactly what the database already stored."""
    return None if value is None else str(value)


def _stat_row(record: PlayerStatRecord) -> tuple[PresentationField, ...]:
    return tuple(PresentationField(name, _value_to_str(getattr(record, name))) for name in _STAT_FIELD_ORDER)


def present_player_raw_statistics(result: PlayerRawStatisticsResult) -> ConsumerPresentation:
    """
    Maps one `PlayerRawStatisticsResult` to a `ConsumerPresentation`:

    - `outcome="not_found"` -> an explicit not-found presentation, never
      an empty successful one.
    - `outcome="found"`, `stats` empty -> a successful, structurally
      distinct **known-empty** presentation (`status="ok"`, a summary
      sentence, `rows=()`) -- a known player with zero qualifying
      statistics is never conflated with an unknown player.
    - `outcome="found"`, `stats` non-empty -> a successful presentation
      with one `PresentationRow` per `PlayerStatRecord`, in the exact
      order `stats` already carries, covering every field except
      `player_key`. `validation_status` is always included, unchanged --
      never hidden, exactly matching the already-approved API decision
      this adapter mirrors.

    Player-level metadata (`display_name`, `player_id`, `team_id`,
    `season_id` -- the player's own canonical season, distinct from each
    stat row's own `season_id`) is exposed as top-level `fields`,
    separate from the per-row statistics table, so a renderer can show
    "who" once and "what" as a table beneath it.
    """
    if result.outcome == "not_found":
        return ConsumerPresentation(status="not_found", title="Player Not Found", message=_NOT_FOUND_MESSAGE)

    assert result.player_id is not None and result.display_name is not None
    # outcome == "found" always carries player_id/display_name/team_id/season_id, per PlayerRawStatisticsResult's own contract.

    metadata_fields = (
        PresentationField("Player", result.display_name),
        PresentationField("Player ID", result.player_id),
        PresentationField("Team ID", str(result.team_id)),
        PresentationField("Season ID", str(result.season_id)),
    )

    if not result.stats:
        return ConsumerPresentation(
            status="ok",
            title=result.display_name,
            summary=_KNOWN_EMPTY_SUMMARY,
            fields=metadata_fields,
        )

    rows = tuple(_stat_row(record) for record in result.stats)
    return ConsumerPresentation(status="ok", title=result.display_name, fields=metadata_fields, rows=rows)
