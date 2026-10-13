"""
Phase 7, Slices 2B/3A/3C: the Streamlit controller coordinating injected
dependencies and Phase 6 service calls for the read-only Team/Rankings/
Injury page, the Player raw-statistics page, and the descriptive Matchup
feature-comparison page.

Streamlit reruns its script synchronously on every interaction -- unlike
`src.discord_bot.consumer_bridge`, no thread bridge (`asyncio.to_thread`)
is needed here, since nothing in this module blocks an event loop that
would otherwise stall. Each `run_*` function still:

1. opens exactly one `Session` from an injected session factory;
2. calls exactly one Phase 6 service operation (Team resolution, plus a
   second per-team Rankings/Injury service call only once Team
   resolution has already succeeded);
3. maps the result (or a raised exception) to a `ConsumerPresentation`
   via the existing, unmodified Phase 7 Slice 1 adapters/error mapper;
4. closes the session in a `finally` block, on both success and
   failure;
5. never commits, flushes, or otherwise mutates data.

This module never imports `discord`, `src.discord_bot.*`, or `src.api.*`
-- it is written as its own, Streamlit-owned controller rather than
reusing `src.discord_bot.consumer_bridge` directly, so the two consumer
surfaces stay structurally independent even though they necessarily
share the same thin orchestration shape (resolve Team, short-circuit on
failure, call the domain service, map the result). No identity rule,
calculation, cache-reading behavior, or error-sanitization meaning is
reimplemented anywhere in this module -- all of that remains
single-sourced in `src.services.*` and `src.consumers.*`.

No engine/session configuration is duplicated either: the default
session factory reuses `src.db.session.build_engine`/`build_session_factory`
(the same authority `src.api.main.create_app` and the Discord bridge
both already use), and the default Rankings configuration reuses
`src.rankings.config.load_rankings_runtime_config_from_env` (the same
env-var resolution `src.api.deps.get_rankings_context` delegates to).
"""
from __future__ import annotations

import traceback
from dataclasses import dataclass
from typing import Callable

from sqlalchemy.orm import Session, sessionmaker

from src.consumers.contracts import ConsumerPresentation
from src.consumers.errors import present_error
from src.consumers.injury import present_team_injury
from src.consumers.matchup import present_matchup_feature_comparison
from src.consumers.player import present_player_raw_statistics
from src.consumers.rankings import present_team_ranking
from src.consumers.team import present_team_lookup
from src.db.session import build_engine, build_session_factory
from src.exceptions import (
    IdentityConflictError,
    InjuryDataIntegrityError,
    RankingsCacheIntegrityError,
    RankingsConfigurationError,
)
from src.rankings.config import RankingsRuntimeConfig, load_rankings_runtime_config_from_env
from src.services.injury import get_injury_report_for_team
from src.services.matchup import compare_team_matchup_features
from src.services.player import get_player_raw_statistics
from src.services.rankings import get_team_ranking
from src.services.team import resolve_team

#: The only exceptions `present_error` recognizes by type -- expected,
#: already-sanitized control-flow outcomes, never printed server-side.
#: Any other exception is printed to the terminal running `streamlit
#: run` (the closest Streamlit equivalent to a server console) before
#: being sanitized the same way, so it remains available to existing
#: internal logging without ever being rendered on the page itself.
_KNOWN_PRESENTATION_EXCEPTIONS: tuple[type[Exception], ...] = (
    IdentityConflictError,
    RankingsConfigurationError,
    RankingsCacheIntegrityError,
    InjuryDataIntegrityError,
)


def _map_exception(exc: Exception) -> ConsumerPresentation:
    if not isinstance(exc, _KNOWN_PRESENTATION_EXCEPTIONS):
        traceback.print_exception(type(exc), exc, exc.__traceback__)
    return present_error(exc)


_default_session_factory: sessionmaker[Session] | None = None


def _default_sessionmaker() -> sessionmaker[Session]:
    """
    Lazily builds and memoizes exactly one engine/session-factory pair
    for the whole process -- built on first *use*, never at import time
    (so importing this module opens no database connection), and never
    rebuilt on later calls.
    """
    global _default_session_factory
    if _default_session_factory is None:
        engine = build_engine()
        _default_session_factory = build_session_factory(engine)
    return _default_session_factory


def open_default_session() -> Session:
    """`Callable[[], Session]` shape expected by `ConsumerControllerDependencies.session_factory`."""
    return _default_sessionmaker()()


@dataclass(frozen=True)
class ConsumerControllerDependencies:
    """
    Everything the page needs, injectable for tests. Both callables
    default to lazy authorities so constructing this dataclass with no
    arguments never itself opens a database connection or reads an
    environment variable -- only calling the resulting callables does,
    exactly once each, per submitted operation.
    """

    session_factory: Callable[[], Session] = open_default_session
    rankings_config_provider: Callable[[], RankingsRuntimeConfig] = load_rankings_runtime_config_from_env


def default_dependencies() -> ConsumerControllerDependencies:
    return ConsumerControllerDependencies()


def run_team_lookup(query: str, *, session_factory: Callable[[], Session]) -> ConsumerPresentation:
    """Exactly one `resolve_team` call, then exactly one `present_team_lookup`/`present_error` call. Session opened and closed entirely within this call."""
    session = session_factory()
    try:
        result = resolve_team(session, query)
        return present_team_lookup(result)
    except Exception as exc:
        return _map_exception(exc)
    finally:
        session.close()


def run_rankings_lookup(
    query: str,
    *,
    session_factory: Callable[[], Session],
    rankings_config_provider: Callable[[], RankingsRuntimeConfig],
) -> ConsumerPresentation:
    """
    Resolves Team first; a `not_found` outcome or a raised
    `IdentityConflictError` short-circuits before the Rankings cache is
    ever touched (`get_team_ranking` is not called).
    """
    session = session_factory()
    try:
        team_result = resolve_team(session, query)
        if team_result.outcome == "not_found":
            return present_team_lookup(team_result)
        config = rankings_config_provider()
        ranking_result = get_team_ranking(config.repository, team_result.team.team_id, season=config.season)
        return present_team_ranking(ranking_result)
    except Exception as exc:
        return _map_exception(exc)
    finally:
        session.close()


def run_injury_lookup(query: str, *, session_factory: Callable[[], Session]) -> ConsumerPresentation:
    """
    Resolves Team first; a `not_found` outcome or a raised
    `IdentityConflictError` short-circuits before the Injury cache is
    ever touched (`get_injury_report_for_team` is not called).
    """
    session = session_factory()
    try:
        team_result = resolve_team(session, query)
        if team_result.outcome == "not_found":
            return present_team_lookup(team_result)
        injury_result = get_injury_report_for_team(session, team_result.team.team_id)
        return present_team_injury(injury_result)
    except Exception as exc:
        return _map_exception(exc)
    finally:
        session.close()


def run_player_raw_statistics_lookup(
    player_id: str,
    *,
    session_factory: Callable[[], Session],
    season_id: int | None = None,
    stat_table: str | None = None,
) -> ConsumerPresentation:
    """
    Phase 7 Slice 3A: exactly one `get_player_raw_statistics` call, then
    exactly one `present_player_raw_statistics`/`present_error` call.
    Session opened and closed entirely within this call. Takes an
    already-canonical `player_id` -- this module performs no Player name
    resolution of its own (none exists anywhere in this codebase; see
    `src.services.player`'s own module docstring). `season_id`/
    `stat_table` are passed straight through to the service unmodified,
    exactly mirroring the existing HTTP query-parameter contract
    (`GET /v1/players/{player_id}/raw-statistics`).
    """
    session = session_factory()
    try:
        result = get_player_raw_statistics(session, player_id, season_id=season_id, stat_table=stat_table)
        return present_player_raw_statistics(result)
    except Exception as exc:
        return _map_exception(exc)
    finally:
        session.close()


def run_matchup_feature_comparison(
    team_a_slug: str,
    team_b_slug: str,
    team_a_features: dict[str, float | None],
    team_b_features: dict[str, float | None],
) -> ConsumerPresentation:
    """
    Phase 7 Slice 3C: exactly one `compare_team_matchup_features` call,
    then exactly one `present_matchup_feature_comparison`/`present_error`
    call. Unlike every other operation in this module, this one opens no
    `Session` at all and accepts no `session_factory` parameter --
    `compare_team_matchup_features` has no database, filesystem, or
    network dependency of its own (it performs no Team identity
    resolution and accepts the two teams' feature values exactly as
    supplied), so there is nothing here for a session to own.
    """
    try:
        result = compare_team_matchup_features(team_a_slug, team_b_slug, team_a_features, team_b_features)
        return present_matchup_feature_comparison(result, team_a_features, team_b_features)
    except Exception as exc:
        return _map_exception(exc)
