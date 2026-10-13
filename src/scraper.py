"""
src/scraper.py
==============
Importable scraping primitives extracted from cbb_teamScrape_v11_TEST.ipynb.

Covers:
  - Rate-limit / backoff helpers (Sports-Reference 429/403 handling)
  - Disk-cache helpers with cache-invalidation support
  - Covers.com and ESPN injury scrapers
  - Injury analysis helpers (impact gating, penalty application)

All HTTP functions accept an optional ``session`` keyword argument so the
notebook's proxy-aware ``safe_get`` can be injected without changing call sites.
If no session is supplied the module falls back to a plain requests.Session.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import random
import re
import time
from pathlib import Path
from typing import Callable

import pandas as pd
import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Module-level defaults (override by passing kwargs or reassigning at import)
# ---------------------------------------------------------------------------

#: Where Sports-Reference HTML pages are cached on disk.
SR_CACHE_DIR: str = "sr_cache_html"

#: Hard cap on how long we will sleep for a single 429/403 before giving up
#: and falling back to cache or raising.
MAX_WAIT_429_SECONDS: int = 180

#: ESPN NCAAB injury feed.
ESPN_INJURY_URL: str = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/"
    "mens-college-basketball/injuries"
)

#: Mapping from pipeline slugs → Sports-Reference canonical slugs for teams
#: whose URL slug differs from the pipeline slug.  Extend as needed.
TEAM_SLUG_TO_SR_PATCH: dict[str, str] = {
    "unc-wilmington": "north-carolina-wilmington",
}

# One shared session for callers that don't inject their own.
_DEFAULT_SESSION: requests.Session = requests.Session()
_DEFAULT_SESSION.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SportsReferenceRateLimited(RuntimeError):
    """Raised when Sports-Reference asks us to wait longer than MAX_WAIT_429_SECONDS."""


# ---------------------------------------------------------------------------
# Low-level HTTP helpers
# ---------------------------------------------------------------------------


def _jitter(a: float = 0.25, b: float = 0.85) -> float:
    """Return a small random float to add to sleep durations."""
    return random.uniform(a, b)


def _resolve_session(session: requests.Session | None) -> requests.Session:
    """Return the injected session or fall back to the module default."""
    return session if session is not None else _DEFAULT_SESSION


def safe_get_with_backoff(
    url: str,
    *,
    timeout: int = 30,
    max_retries: int = 6,
    base_sleep: float = 8.0,
    headers: dict | None = None,
    session: requests.Session | None = None,
) -> requests.Response:
    """
    GET ``url`` with exponential backoff for Sports-Reference throttling.

    Behaviour
    ---------
    - 429 / 403 → exponential back-off + jitter; honours ``Retry-After`` header
      when present; sleep capped at ``MAX_WAIT_429_SECONDS``.
    - 5xx        → retry with back-off.
    - Other errors → retry up to ``max_retries``, then raise RuntimeError.

    Parameters
    ----------
    url:
        Target URL.
    timeout:
        Per-request timeout in seconds.
    max_retries:
        Total attempts before giving up.
    base_sleep:
        Base seconds for the first backoff interval (doubles each retry).
    headers:
        Extra request headers merged on top of session defaults.
    session:
        Inject a custom session (e.g. a proxy-aware one from the notebook).
        Falls back to the module-level default session if omitted.

    Returns
    -------
    requests.Response

    Raises
    ------
    RuntimeError
        After ``max_retries`` failed attempts.
    """
    sess = _resolve_session(session)
    hdrs = {
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.sports-reference.com/",
    }
    if headers:
        hdrs.update(headers)

    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            r = sess.get(url, timeout=timeout, headers=hdrs)
            if r.status_code in (429, 403):
                ra = r.headers.get("Retry-After")
                sleep_s = float(ra) if ra else base_sleep * (2 ** (attempt - 1))
                sleep_s = min(sleep_s, float(MAX_WAIT_429_SECONDS)) + _jitter()
                print(
                    f"[RATE LIMIT] {r.status_code} on attempt {attempt}."
                    f" Sleeping {sleep_s:.1f}s..."
                )
                time.sleep(sleep_s)
                continue
            if 500 <= r.status_code < 600:
                sleep_s = min(base_sleep * (2 ** (attempt - 1)), 120.0) + _jitter()
                print(
                    f"[SERVER ERR] {r.status_code} on attempt {attempt}."
                    f" Sleeping {sleep_s:.1f}s..."
                )
                time.sleep(sleep_s)
                continue
            r.raise_for_status()
            return r
        except Exception as exc:
            last_exc = exc
            sleep_s = min(base_sleep * (2 ** (attempt - 1)), 120.0) + _jitter()
            print(
                f"[RETRY] Error on attempt {attempt}: {repr(exc)}."
                f" Sleeping {sleep_s:.1f}s..."
            )
            time.sleep(sleep_s)

    raise RuntimeError(
        f"Failed to GET {url} after {max_retries} retries"
    ) from last_exc


def safe_get_best_effort(
    url: str,
    *,
    timeout: int = 30,
    headers: dict | None = None,
    session: requests.Session | None = None,
) -> requests.Response:
    """
    Non-blocking GET for cache-refresh flows.

    On 429 / 403 this function **does not sleep** — it returns the response
    immediately so the caller can decide to fall back to a cached copy rather
    than blocking the notebook.

    Parameters
    ----------
    url, timeout, headers, session:
        Same as :func:`safe_get_with_backoff`.

    Returns
    -------
    requests.Response
        The raw response (caller must check ``status_code``).
    """
    sess = _resolve_session(session)
    hdrs = {
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.sports-reference.com/",
    }
    if headers:
        hdrs.update(headers)
    return sess.get(url, timeout=timeout, headers=hdrs)


# ---------------------------------------------------------------------------
# Sports-Reference throttle tracker
# ---------------------------------------------------------------------------

_LAST_SR_HIT: float = 0.0


def sr_throttle(min_delay: float = 2.0, max_delay: float = 4.0) -> None:
    """
    Enforce a minimum inter-request gap for Sports-Reference requests.

    Call this immediately before each SR request.  If the last SR hit was
    less than ``[min_delay, max_delay]`` seconds ago the function sleeps for
    the remaining time.

    Parameters
    ----------
    min_delay:
        Minimum seconds to wait between consecutive SR requests.
    max_delay:
        Upper bound of the randomised wait window.
    """
    global _LAST_SR_HIT
    now = time.time()
    elapsed = now - _LAST_SR_HIT
    target = random.uniform(min_delay, max_delay)
    if elapsed < target:
        time.sleep(target - elapsed)
    _LAST_SR_HIT = time.time()


# ---------------------------------------------------------------------------
# Disk-cache helpers
# ---------------------------------------------------------------------------


def _cache_key(url: str) -> str:
    """Return the filename (MD5 hash + ``.html``) used for a URL's cached copy."""
    return hashlib.md5(url.encode("utf-8")).hexdigest() + ".html"


def invalidate_team_page_cache(
    team_slug: str,
    season: int,
    *,
    cache_dir: str | None = None,
    slug_patch: dict[str, str] | None = None,
) -> bool:
    """
    Delete the cached HTML file for a specific team + season page.

    Use this when you know a team's data has changed mid-season and you need
    to force a fresh fetch on the next call to :func:`fetch_team_page`.

    Parameters
    ----------
    team_slug:
        Pipeline slug for the team (e.g. ``"st-bonaventure"``).
    season:
        Four-digit season year (e.g. ``2026``).
    cache_dir:
        Override the cache directory.  Defaults to :data:`SR_CACHE_DIR`.
    slug_patch:
        Override :data:`TEAM_SLUG_TO_SR_PATCH`.  Pass ``None`` to use module default.

    Returns
    -------
    bool
        ``True`` if a cached file was found and deleted; ``False`` if nothing
        was cached.
    """
    patch = TEAM_SLUG_TO_SR_PATCH if slug_patch is None else slug_patch
    sr_slug = patch.get(team_slug, team_slug)
    url = f"https://www.sports-reference.com/cbb/schools/{sr_slug}/men/{season}.html"
    path = os.path.join(cache_dir or SR_CACHE_DIR, _cache_key(url))
    if os.path.exists(path):
        os.remove(path)
        print(f"[CACHE] Invalidated: {path}")
        return True
    return False


def invalidate_covers_slug_cache(cache_path: str = "covers_slug_cache.json") -> bool:
    """
    Delete the cached Covers.com team-slug map so the next call to
    :func:`build_covers_ncaab_slug_map` re-scrapes the full list.

    Parameters
    ----------
    cache_path:
        Path to the JSON cache file.  Defaults to ``"covers_slug_cache.json"``.

    Returns
    -------
    bool
        ``True`` if the file existed and was deleted.
    """
    if os.path.exists(cache_path):
        os.remove(cache_path)
        print(f"[CACHE] Invalidated Covers slug map: {cache_path}")
        return True
    return False


# ---------------------------------------------------------------------------
# Sports-Reference team page fetcher
# ---------------------------------------------------------------------------


def fetch_team_page(
    team_slug: str,
    season: int,
    *,
    force_refresh: bool = False,
    cache_dir: str | None = None,
    slug_patch: dict[str, str] | None = None,
    session: requests.Session | None = None,
) -> str:
    """
    Fetch the Sports-Reference CBB team season page and cache it to disk.

    Behaviour
    ---------
    - Returns the cached HTML file immediately unless ``force_refresh=True``.
    - On a live fetch: polite baseline jitter → retry loop with 429/403 handling.
    - If Sports-Reference returns a 429 with ``Retry-After`` > :data:`MAX_WAIT_429_SECONDS`:

      * Uses the cached copy if one exists.
      * Raises :exc:`SportsReferenceRateLimited` if no cache is available.

    Parameters
    ----------
    team_slug:
        Pipeline slug for the team (e.g. ``"st-bonaventure"``).
    season:
        Four-digit season year (e.g. ``2026``).
    force_refresh:
        Skip the cache and fetch a fresh copy.  On success the new HTML
        overwrites the existing cache file.
    cache_dir:
        Directory for HTML cache files.  Defaults to :data:`SR_CACHE_DIR`.
    slug_patch:
        Mapping from pipeline slugs to SR canonical slugs.  Defaults to
        :data:`TEAM_SLUG_TO_SR_PATCH`.
    session:
        Inject a proxy-aware session.  Falls back to module default.

    Returns
    -------
    str
        Raw HTML of the team season page.

    Raises
    ------
    ValueError
        If Sports-Reference returns 404 (bad slug).
    SportsReferenceRateLimited
        If rate-limited with no cache fallback available.
    """
    os.makedirs(cache_dir or SR_CACHE_DIR, exist_ok=True)
    sess = _resolve_session(session)
    patch = TEAM_SLUG_TO_SR_PATCH if slug_patch is None else slug_patch
    cdir = cache_dir or SR_CACHE_DIR

    # Sanitise slug
    team_slug = re.sub(r'[()"\']', "", team_slug).strip("-")

    # Apply SR canonical slug patch
    sr_slug = patch.get(team_slug, team_slug)
    if sr_slug != team_slug:
        print(f"[SR PATCH] '{team_slug}' → '{sr_slug}'")
        team_slug = sr_slug

    url = f"https://www.sports-reference.com/cbb/schools/{team_slug}/men/{season}.html"
    print(f"[SR] Fetching: {url}")
    cache_path = os.path.join(cdir, _cache_key(url))

    # 1) Serve from cache unless a refresh was requested
    if (not force_refresh) and os.path.exists(cache_path):
        with open(cache_path, "r", encoding="utf-8") as fh:
            return fh.read()

    # 2) Polite baseline jitter before a live fetch
    time.sleep(random.uniform(1.3, 2.8))

    _sr_headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://www.sports-reference.com/cbb/",
        "Connection": "keep-alive",
    }

    base_sleep = 3.0
    max_attempts = 8

    for attempt in range(1, max_attempts + 1):
        r = sess.get(url, timeout=30, headers=_sr_headers)

        if r.status_code == 404:
            raise ValueError(
                f"404 for team_slug='{team_slug}'. "
                "Verify the Sports-Reference canonical slug."
            )

        if r.status_code == 429:
            ra = r.headers.get("Retry-After")
            if ra and str(ra).isdigit():
                sleep_s = int(ra)
            else:
                sleep_s = base_sleep * (2 ** (attempt - 1)) + random.uniform(0.0, 2.5)

            if sleep_s > MAX_WAIT_429_SECONDS:
                if os.path.exists(cache_path):
                    print("[RATE LIMIT] 429 long cooldown — using cached page.")
                    with open(cache_path, "r", encoding="utf-8") as fh:
                        return fh.read()
                raise SportsReferenceRateLimited(
                    f"429 Too Many Requests. Retry-After={sleep_s}s exceeds cap="
                    f"{MAX_WAIT_429_SECONDS}s. No cache available. Try later."
                )

            print(f"[RATE LIMIT] 429 attempt {attempt}. Sleeping {sleep_s:.1f}s...")
            time.sleep(sleep_s)
            continue

        if r.status_code == 403:
            sleep_s = base_sleep * (2 ** (attempt - 1)) + random.uniform(2.0, 5.0)
            if sleep_s > MAX_WAIT_429_SECONDS:
                if os.path.exists(cache_path):
                    print("[RATE LIMIT] 403 long cooldown — using cached page.")
                    with open(cache_path, "r", encoding="utf-8") as fh:
                        return fh.read()
                raise SportsReferenceRateLimited(
                    f"403 Forbidden after {attempt} attempts — SR is blocking this IP."
                )
            print(f"[RATE LIMIT] 403 attempt {attempt}. Sleeping {sleep_s:.1f}s...")
            time.sleep(sleep_s)
            continue

        r.raise_for_status()

        html = r.text
        with open(cache_path, "w", encoding="utf-8") as fh:
            fh.write(html)
        return html

    raise SportsReferenceRateLimited(
        f"Failed after {max_attempts} attempts: {url}"
    )


# ---------------------------------------------------------------------------
# Injury-status normalisation helpers
# ---------------------------------------------------------------------------

#: Maps Covers/ESPN status strings to availability fractions (0 = out, 1 = healthy).
COVERS_AVAILABILITY: dict[str, float] = {
    "out": 0.0,
    "doubtful": 0.25,
    "questionable": 0.6,
    "probable": 0.85,
}


def normalize_covers_status(text: str) -> str:
    """
    Normalise a raw Covers or ESPN status string to a canonical value.

    Returns one of: ``"out"``, ``"doubtful"``, ``"questionable"``,
    ``"probable"``, ``"active"``.

    Parameters
    ----------
    text:
        Raw status string from Covers HTML or ESPN JSON (e.g. ``"Questionable - Undisclosed"``).
    """
    t = (text or "").lower()
    if "out" in t:
        return "out"
    if "doubtful" in t:
        return "doubtful"
    if "questionable" in t:
        return "questionable"
    if "probable" in t:
        return "probable"
    return "active"


def normalize_covers_player(raw_player: str) -> str:
    """
    Extract a normalised last name from a Covers player string.

    Handles suffixes (Jr., Sr., II–VI) and dotted-initial formats such as
    ``"P. Ngongba II"`` → ``"ngongba"`` and ``"A. Marshall"`` → ``"marshall"``.

    Parameters
    ----------
    raw_player:
        Raw player name string from Covers or ESPN (e.g. ``"Darius McGhee Jr."``).

    Returns
    -------
    str
        Lower-cased last name with punctuation stripped, or ``""`` if input is blank.
    """
    SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "v", "vi"}
    s = re.sub(r"\s+", " ", (raw_player or "").strip())
    if not s:
        return ""
    parts = s.split(" ")
    # Strip trailing suffixes before cleaning periods
    while parts and parts[-1].lower().rstrip(".") in SUFFIXES:
        parts.pop()
    if not parts:
        return ""
    return parts[-1].replace(".", "").lower().strip()


def canonicalize_team(team_name: str) -> str:
    """
    Strip punctuation and lower-case a team name for dictionary key matching.

    Parameters
    ----------
    team_name:
        Raw team name (e.g. ``"St. Bonaventure Bonnies"``).

    Returns
    -------
    str
        Lowercase alphanumeric-plus-spaces string (e.g. ``"st bonaventure bonnies"``).
    """
    return re.sub(r"[^a-z0-9 ]", "", (team_name or "").lower()).strip()


def covers_team_alias(team_name: str) -> str:
    """
    Return a simplified alias used to fuzzy-match Covers team keys.

    Handles the Miami (OH) / Miami (FL) disambiguation; otherwise returns
    the first word of the lower-cased name.

    Parameters
    ----------
    team_name:
        Display team name from the pipeline (e.g. ``"Miami (OH) RedHawks"``).
    """
    name = team_name.lower()
    if "miami" in name and "oh" in name:
        return "miami"
    if "miami" in name and "florida" in name:
        return "miami florida"
    return name.split()[0]


def is_relevant_injury(status: str) -> bool:
    """
    Return ``True`` if the status string represents an injury worth tracking.

    Includes ``"questionable"`` because Covers uses it heavily for game-time
    decisions that meaningfully affect lineup projections.

    Parameters
    ----------
    status:
        Normalised status string from :func:`normalize_covers_status`.
    """
    return status.lower() in {"out", "doubtful", "questionable", "probable"}


def filter_relevant_injuries(injuries: list[dict]) -> list[dict]:
    """
    Filter an injury list to only confirmed-out or doubtful players.

    More conservative than :func:`is_relevant_injury` — excludes
    ``"questionable"`` and ``"probable"`` which may still play.

    Parameters
    ----------
    injuries:
        List of injury dicts with at least a ``"status"`` key.

    Returns
    -------
    list[dict]
        Subset where ``status`` is ``"out"`` or ``"doubtful"``.
    """
    return [i for i in injuries if i["status"].lower() in {"out", "doubtful"}]


# ---------------------------------------------------------------------------
# Covers.com slug map
# ---------------------------------------------------------------------------


def build_covers_ncaab_slug_map(
    force_refresh: bool = False,
    cache_path: str = "covers_slug_cache.json",
    session: requests.Session | None = None,
) -> dict[str, str]:
    """
    Build a slug map from the Covers.com NCAAB teams index.

    Scrapes ``https://www.covers.com/sport/basketball/ncaab/teams`` and stores
    the result as a JSON file.  Subsequent calls load from cache unless
    ``force_refresh=True`` or the cache is stale (fewer than 10 entries).

    The map stores up to **four lookup keys per team** for maximum match
    coverage:

    1. Full Covers slug as-is          (``"saint-joseph-s-hawks"``)
    2. Display name normalised         (``"saint-josephs"`` — apostrophe stripped)
    3. Slug with ``-s-`` → ``'s-``     (apostrophe restoration variant)
    4. Slug without mascot             (``"saint-joseph-s"``, ``"saint-joseph"``)

    Parameters
    ----------
    force_refresh:
        Re-scrape even if a valid cache exists.
    cache_path:
        File path for the JSON slug-map cache.
    session:
        Inject a custom session.  Falls back to module default.

    Returns
    -------
    dict[str, str]
        Mapping of lookup key → Covers slug.
    """
    if not force_refresh and os.path.exists(cache_path):
        with open(cache_path) as fh:
            cached = json.load(fh)
        if len(cached) > 10:
            print(f"[COVERS] Slug map loaded from cache ({len(cached)} teams)")
            return cached
        print(f"[COVERS] Cache only has {len(cached)} entries — re-scraping...")

    COVERS_INDEX = "https://www.covers.com/sport/basketball/ncaab/teams"
    PREFIX = "/sport/basketball/ncaab/teams/main/"

    headers = {"User-Agent": "Mozilla/5.0"}
    sess = _resolve_session(session)
    resp = sess.get(COVERS_INDEX, headers=headers, timeout=10)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    slug_map: dict[str, str] = {}
    seen_slugs: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if PREFIX not in href:
            continue
        raw_path = href.split(PREFIX)[-1].strip("/")
        covers_slug = raw_path.split("/")[0]
        if not covers_slug or covers_slug in seen_slugs:
            continue
        seen_slugs.add(covers_slug)

        display_name = a.get_text(strip=True)
        if not display_name:
            continue

        # Key 1: covers slug as-is
        slug_map[covers_slug] = covers_slug

        # Key 2: display name normalised (apostrophe stripped)
        norm_strip = re.sub(
            r"[^a-z0-9]+", "-", re.sub(r"'", "", display_name.lower())
        ).strip("-")
        if norm_strip and norm_strip not in slug_map:
            slug_map[norm_strip] = covers_slug

        # Key 3: apostrophe-s restoration variant
        norm_apos = re.sub(r"-s-", "'s-", covers_slug)
        if norm_apos != covers_slug and norm_apos not in slug_map:
            slug_map[norm_apos] = covers_slug

        # Key 4: slug without mascot (last 1–2 words)
        parts = covers_slug.split("-")
        for cutoff in (-1, -2):
            short_key = "-".join(parts[:cutoff])
            if len(short_key) >= 5 and short_key not in slug_map:
                slug_map[short_key] = covers_slug

    with open(cache_path, "w") as fh:
        json.dump(slug_map, fh, indent=2)

    print(
        f"[COVERS] Slug map built ({len(slug_map)} keys"
        f" → {len(seen_slugs)} teams) → {cache_path}"
    )
    return slug_map


# ---------------------------------------------------------------------------
# Covers.com per-team injury scraper
# ---------------------------------------------------------------------------


def scrape_covers_injuries_for_teams(
    team_slugs: list[str],
    covers_slug_map: dict[str, str],
    *,
    session: requests.Session | None = None,
) -> dict[str, list[dict]]:
    """
    Scrape the injury page for each team in ``team_slugs`` from Covers.com.

    A polite rate-limit buffer of 2.5 – 4.0 s is inserted between consecutive
    team requests to avoid triggering Covers bot detection.

    Parameters
    ----------
    team_slugs:
        List of pipeline (Sports-Reference) team slugs to look up.
    covers_slug_map:
        Output of :func:`build_covers_ncaab_slug_map`.
    session:
        Inject a custom session.  Falls back to module default.

    Returns
    -------
    dict[str, list[dict]]
        Keys are SR slugs; values are lists of injury dicts with keys
        ``"player"`` (normalised last name), ``"status"``, ``"detail"``.
    """
    sess = _resolve_session(session)
    injuries: dict[str, list[dict]] = {}

    COVERS_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.covers.com/sport/basketball/ncaab/teams",
    }

    for i, sr_slug in enumerate(team_slugs):
        if i > 0:
            _sleep = 2.5 + random.uniform(0.5, 1.5)
            print(f"  [rate limit buffer] sleeping {_sleep:.1f}s before {sr_slug}...")
            time.sleep(_sleep)

        covers_slug = covers_slug_map.get(sr_slug)
        if not covers_slug:
            print(f"  [COVERS] No slug mapped for {sr_slug!r} — skipping")
            continue

        url = (
            f"https://www.covers.com/sport/basketball/ncaab/teams/"
            f"main/{covers_slug}/injuries"
        )
        try:
            r = sess.get(url, headers=COVERS_HEADERS, timeout=30)
            if r.status_code != 200:
                print(f"  [COVERS] {sr_slug}: HTTP {r.status_code}")
                continue

            soup = BeautifulSoup(r.text, "lxml")
            injury_pane = soup.find("div", id="TP_injuries")
            if not injury_pane:
                print(f"  [COVERS] {sr_slug}: injury pane not found")
                continue

            team_injuries: list[dict] = []

            for tr in injury_pane.find_all("tr"):
                if tr.get("id", "").startswith("injuryCollapse"):
                    continue
                tds = tr.find_all("td")
                if len(tds) < 3:
                    continue

                player_raw = re.sub(
                    r"\s+", " ", tds[0].get_text(" ", strip=True)
                ).strip()
                _b = tds[2].find("b")
                status_raw = (
                    _b.get_text(strip=True) if _b else tds[2].get_text(" ", strip=True)
                )
                status_raw = re.sub(r"\s+", " ", status_raw).strip()

                player_last = normalize_covers_player(player_raw)
                status = normalize_covers_status(status_raw)

                if player_last and status != "active":
                    team_injuries.append(
                        {
                            "player": player_last,
                            "status": status,
                            "detail": status_raw,
                        }
                    )
                    print(f"    → {player_raw.strip()} | {status_raw.strip()}")

            injuries[sr_slug] = team_injuries

            if team_injuries:
                print(f"  [COVERS] {sr_slug}: {len(team_injuries)} report(s)")
            else:
                print(f"  [COVERS] {sr_slug}: no injuries reported")

        except Exception as exc:
            print(f"  [COVERS] Scrape failed for {sr_slug}: {exc}")

    return injuries


# ---------------------------------------------------------------------------
# ESPN + minutes-drift injury feed
# ---------------------------------------------------------------------------


def scrape_espn_injuries(
    *,
    gamelog_base_dir: str | None = None,
    season: int = 2026,
    session: requests.Session | None = None,
) -> dict[str, list[dict]]:
    """
    Fetch NCAAB injury data.

    Primary source: ESPN public JSON API (fast, zero rate-limit concern).
    Fallback: minutes-drift detection from saved per-player game-log CSVs
    (used when the ESPN feed is empty post-season or during off-season).

    Parameters
    ----------
    gamelog_base_dir:
        Root directory containing per-team folders with ``*_gamelog.csv`` files.
        Only used by the fallback path.  Example:
        ``r"C:\\Users\\...\\TeamDump\\CBB"``.
    season:
        Season year for the gamelog fallback scan.
    session:
        Inject a custom session.  Falls back to module default.

    Returns
    -------
    dict[str, list[dict]]
        Keys are canonicalised team names; values are lists of injury dicts
        with keys ``"player"``, ``"status"``, ``"detail"``.
    """
    sess = _resolve_session(session)

    # ── Primary: ESPN API ────────────────────────────────────────────────────
    try:
        res = sess.get(ESPN_INJURY_URL, timeout=20)
        if res.status_code == 200:
            data = res.json()
            espn_injuries = data.get("injuries", [])
            if espn_injuries:
                injuries: dict[str, list[dict]] = {}
                for team_block in espn_injuries:
                    team_info = team_block.get("team", {})
                    raw_team = (
                        team_info.get("displayName", "") or team_info.get("name", "")
                    )
                    if not raw_team:
                        continue
                    team_key = canonicalize_team(raw_team)
                    injuries[team_key] = []
                    for entry in team_block.get("injuries", []):
                        athlete = entry.get("athlete", {})
                        full_name = (
                            athlete.get("displayName", "")
                            or athlete.get("fullName", "")
                        )
                        player_key = normalize_covers_player(full_name)
                        raw_status = entry.get("status", "") or ""
                        status = normalize_covers_status(raw_status)
                        detail = (
                            entry.get("longComment", "")
                            or entry.get("shortComment", "")
                            or raw_status
                        )
                        if status != "active" and player_key:
                            injuries[team_key].append(
                                {
                                    "player": player_key,
                                    "status": status,
                                    "detail": detail,
                                }
                            )
                print(f"[ESPN] Injury feed loaded — {len(injuries)} teams")
                return injuries
    except Exception as exc:
        print(f"[ESPN] Injury fetch failed: {exc}")

    # ── Fallback: minutes-drift from saved gamelogs ──────────────────────────
    print("[ESPN] Feed empty — falling back to minutes-drift detection")
    injuries_fallback: dict[str, list[dict]] = {}

    if not gamelog_base_dir or not os.path.isdir(gamelog_base_dir):
        print("[ESPN] gamelog_base_dir not set or missing — fallback skipped")
        return injuries_fallback

    try:
        for team_folder in os.listdir(gamelog_base_dir):
            team_path = os.path.join(gamelog_base_dir, team_folder)
            if not os.path.isdir(team_path):
                continue

            player_logs = glob.glob(
                os.path.join(team_path, f"*_{season}_gamelog.csv")
            )
            # Exclude the team-level game log
            player_logs = [
                p
                for p in player_logs
                if os.path.basename(p) != f"{team_folder}_{season}_game_log.csv"
            ]
            if not player_logs:
                continue

            allstats_path = os.path.join(
                team_path, f"{team_folder}_{season}_AllStats.xlsx"
            )
            if not os.path.exists(allstats_path):
                continue

            try:
                df_as = pd.read_excel(allstats_path)
                if "MPG" not in df_as.columns or "Name" not in df_as.columns:
                    continue
            except Exception:
                continue

            team_injuries: list[dict] = []

            for log_path in player_logs:
                fname = os.path.basename(log_path)
                pname = fname.replace(f"_{season}_gamelog.csv", "").replace("_", " ")
                last = pname.strip().split()[-1].lower()

                match = df_as[df_as["Name"].str.lower().str.contains(last, na=False)]
                if match.empty:
                    continue
                season_mpg = float(match.iloc[0]["MPG"])
                if season_mpg < 8:
                    continue  # not a rotation player

                try:
                    gl = pd.read_csv(log_path)
                    min_col = next(
                        (c for c in ["mp", "MP", "MIN", "minutes"] if c in gl.columns),
                        None,
                    )
                    if min_col is None:
                        continue

                    recent_mpg = float(
                        pd.to_numeric(gl[min_col], errors="coerce")
                        .dropna()
                        .tail(3)
                        .mean()
                    )
                    # Flag if recent MPG dropped more than 40% vs season average
                    if season_mpg > 0 and (season_mpg - recent_mpg) / season_mpg >= 0.40:
                        team_injuries.append(
                            {
                                "player": last,
                                "status": "questionable",
                                "detail": (
                                    f"Minutes drop: {season_mpg:.1f}"
                                    f" → {recent_mpg:.1f} MPG"
                                ),
                            }
                        )
                except Exception:
                    continue

            if team_injuries:
                team_key = canonicalize_team(team_folder.replace("-", " "))
                injuries_fallback[team_key] = team_injuries
                print(
                    f"  [DRIFT] {team_folder} — {len(team_injuries)} player(s)"
                    " flagged for minutes drop"
                )

    except Exception as exc:
        print(f"[ESPN] Gamelog fallback failed: {exc}")

    return injuries_fallback


# ---------------------------------------------------------------------------
# ESPN injury deduction layer (FIX-5 pattern)
# ---------------------------------------------------------------------------

_espn_injury_cache: dict[str, list[dict]] = {}

#: PPG-bracket deduction multipliers by status for ESPN-sourced injuries.
_INJURY_IMPACT: dict[str, dict[str, float]] = {
    "Out":          {"ppg_10_15": 0.85, "ppg_15_plus": 1.00},
    "Questionable": {"ppg_10_15": 0.40, "ppg_15_plus": 0.55},
    "Doubtful":     {"ppg_10_15": 0.65, "ppg_15_plus": 0.80},
    "Day-To-Day":   {"ppg_10_15": 0.20, "ppg_15_plus": 0.30},
}


def _fetch_espn_injuries_for_team(
    team_espn_id: str | None = None,
    *,
    session: requests.Session | None = None,
) -> list[dict]:
    """
    Pull the NCAAB injury list from the ESPN public API.

    Results are cached in-process for the duration of the Python session.
    Call :func:`clear_espn_injury_cache` at the start of each game day to
    ensure fresh data.

    Parameters
    ----------
    team_espn_id:
        ESPN numeric team ID string.  If ``None`` all teams are returned.
    session:
        Inject a custom session.  Falls back to module default.

    Returns
    -------
    list[dict]
        Each dict has keys ``"name"`` (player display name), ``"status"``,
        ``"ppg"`` (points per game, 0.0 if unavailable).
        Returns ``[]`` on any fetch failure.
    """
    cache_key = team_espn_id or "all"
    if cache_key in _espn_injury_cache:
        return _espn_injury_cache[cache_key]

    sess = _resolve_session(session)
    try:
        resp = sess.get(ESPN_INJURY_URL, timeout=12)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        print(f"[INJURY] ESPN fetch failed: {exc} — no adjustment applied.")
        return []

    injuries: list[dict] = []
    for team_entry in data.get("injuries", []):
        tid = str(team_entry.get("team", {}).get("id", ""))
        if team_espn_id and tid != str(team_espn_id):
            continue
        for inj in team_entry.get("injuries", []):
            athlete = inj.get("athlete", {})
            ppg = 0.0
            for stat in inj.get("stats", []):
                if stat.get("name", "").lower() in ("pts", "points", "ppg"):
                    try:
                        ppg = float(stat.get("value", 0))
                    except Exception:
                        pass
            injuries.append(
                {
                    "name":   athlete.get("displayName", ""),
                    "status": inj.get("status", ""),
                    "ppg":    ppg,
                }
            )

    _espn_injury_cache[cache_key] = injuries
    return injuries


def compute_injury_deduction(
    team_espn_id: str | None,
    team_name: str,
    *,
    ppg_threshold: float = 10.0,
    verbose: bool = True,
    session: requests.Session | None = None,
) -> float:
    """
    Compute the total off-rating point deduction from key injured players.

    Only players averaging ``>= ppg_threshold`` are considered.  The result
    is capped at 8.0 pts.

    Parameters
    ----------
    team_espn_id:
        ESPN numeric team ID.  Pass ``None`` to pull all teams.
    team_name:
        Display name for logging.
    ppg_threshold:
        Minimum PPG to be considered an impactful injury.
    verbose:
        Print a per-player breakdown when injuries are found.
    session:
        Inject a custom session.

    Returns
    -------
    float
        Total off-rating deduction (0.0 – 8.0).
    """
    injuries = _fetch_espn_injuries_for_team(team_espn_id, session=session)
    if not injuries:
        return 0.0

    total = 0.0
    applied: list[tuple] = []

    for inj in injuries:
        ppg = float(inj.get("ppg", 0.0))
        status = inj.get("status", "")
        name = inj.get("name", "?")
        if ppg < ppg_threshold:
            continue
        impact = _INJURY_IMPACT.get(status)
        if not impact:
            continue
        bracket = "ppg_15_plus" if ppg >= 15.0 else "ppg_10_15"
        deduction = ppg * impact[bracket]
        total += deduction
        applied.append((name, status, ppg, deduction))

    total = min(total, 8.0)

    if verbose and applied:
        print(f"[INJURY] {team_name}:")
        for name, status, ppg, ded in applied:
            print(f"  • {name} ({status}) {ppg:.1f} ppg → −{ded:.1f} pts")
        print(f"  Total: −{total:.1f} pts")
    elif verbose:
        print(f"[INJURY] {team_name} — no key injuries (ESPN)")

    return round(total, 2)


def clear_espn_injury_cache() -> None:
    """
    Flush the in-process ESPN injury cache.

    Call once at the start of each game day to ensure :func:`compute_injury_deduction`
    and :func:`_fetch_espn_injuries_for_team` pull fresh data.
    """
    global _espn_injury_cache
    _espn_injury_cache = {}
    print("[INJURY] ESPN cache cleared.")


def lookup_espn_team_id(
    fragment: str,
    *,
    session: requests.Session | None = None,
) -> list[dict]:
    """
    Find ESPN numeric team IDs by partial name match.

    Parameters
    ----------
    fragment:
        Partial team name or location (case-insensitive).
    session:
        Inject a custom session.

    Returns
    -------
    list[dict]
        Each dict has keys ``"id"`` and ``"name"``.  Empty list on failure.
    """
    url = (
        "https://site.api.espn.com/apis/site/v2/sports/basketball/"
        "mens-college-basketball/teams?limit=700"
    )
    sess = _resolve_session(session)
    try:
        resp = sess.get(url, timeout=15)
        resp.raise_for_status()
        teams = (
            resp.json()
            .get("sports", [{}])[0]
            .get("leagues", [{}])[0]
            .get("teams", [])
        )
    except Exception as exc:
        print(f"[ESPN] Team lookup failed: {exc}")
        return []

    frag = fragment.lower()
    return [
        {"id": t["team"].get("id"), "name": t["team"].get("displayName")}
        for t in teams
        if frag in t["team"].get("displayName", "").lower()
        or frag in t["team"].get("location", "").lower()
    ]


# ---------------------------------------------------------------------------
# Injury analysis helpers
# ---------------------------------------------------------------------------


def identify_impact_players(
    team_slug: str,
    season: int,
    allstats_path: str,
    *,
    min_mpg: float = 18.0,
    top_n: int = 5,
) -> pd.DataFrame:
    """
    Return the top impact players likely to shift the line if unavailable.

    Parameters
    ----------
    team_slug:
        Pipeline slug (used for logging only; path is explicit).
    season:
        Season year (used for logging only).
    allstats_path:
        Absolute path to the ``*_AllStats.xlsx`` file for this team/season.
    min_mpg:
        Minimum minutes per game threshold to qualify.
    top_n:
        Number of players to return.

    Returns
    -------
    pd.DataFrame
        Columns: ``["Player", "MPG", "PlayerValueIndex"]``, sorted by PVI desc.

    Raises
    ------
    ValueError
        If ``AllStats`` is missing required columns.
    """
    df = pd.read_excel(allstats_path)
    required = {"Player", "MPG", "PlayerValueIndex"}
    if not required.issubset(df.columns):
        raise ValueError(
            f"AllStats for {team_slug}/{season} is missing columns: "
            f"{required - set(df.columns)}"
        )
    df["MPG"] = pd.to_numeric(df["MPG"], errors="coerce").fillna(0.0)
    df["PlayerValueIndex"] = pd.to_numeric(
        df["PlayerValueIndex"], errors="coerce"
    ).fillna(0.0)
    df = df[df["MPG"] >= min_mpg].sort_values("PlayerValueIndex", ascending=False)
    return df.head(top_n)[["Player", "MPG", "PlayerValueIndex"]]


def get_meaningful_lastnames(df: pd.DataFrame) -> set[str]:
    """
    Identify rotation/impact players who matter for injury gating.

    Uses two gates so neither is overly restrictive:

    - **Gate A** – any player with MPG ≥ 8 **or** total minutes ≥ 120.
    - **Gate B** – top-9 players by ``ImpactScore = PlayerValueIndex × MPG``.

    Parameters
    ----------
    df:
        AllStats DataFrame that must contain ``"__last_name"``, ``"MPG"``,
        ``"MP_Totals"``, and ``"PlayerValueIndex"`` columns.

    Returns
    -------
    set[str]
        Lower-cased last names of rotation/impact players.
    """
    df2 = df.copy()

    mpg = pd.to_numeric(df2.get("MPG", 0), errors="coerce").fillna(0.0)
    mp_tot = pd.to_numeric(df2.get("MP_Totals", 0), errors="coerce").fillna(0.0)
    pvi = pd.to_numeric(df2.get("PlayerValueIndex", 0), errors="coerce").fillna(0.0)
    df2["ImpactScore"] = pvi * mpg

    MIN_MPG = 8.0
    MIN_TOTAL_MIN = 120.0
    TOP_N = 9

    meaningful: set[str] = set()

    gate_a = df2[(mpg >= MIN_MPG) | (mp_tot >= MIN_TOTAL_MIN)]
    meaningful |= set(gate_a["__last_name"].dropna().astype(str).str.strip())

    gate_b = df2.sort_values("ImpactScore", ascending=False).head(TOP_N)
    meaningful |= set(gate_b["__last_name"].dropna().astype(str).str.strip())

    meaningful.discard("")
    return meaningful
