from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
import ctypes
from ctypes import wintypes

from src.paths import ROOT_DIR, TEAMDUMP_DIR


CHROMEDRIVER_PATH = TEAMDUMP_DIR / "chromedriver.exe"


def dependency_status() -> list[dict[str, str]]:
    """
    Report whether important Python packages are importable.
    """
    packages = [
        "streamlit",
        "pandas",
        "numpy",
        "requests",
        "bs4",
        "lxml",
        "openpyxl",
        "pyarrow",
        "PIL",
        "playwright",
        "selenium",
        "plotly",
    ]

    rows = []
    for package in packages:
        rows.append(
            {
                "package": package,
                "available": "yes" if importlib.util.find_spec(package) else "no",
            }
        )

    return rows


def _run_version(command: list[str]) -> str:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except Exception as exc:
        return f"unavailable ({type(exc).__name__})"

    output = (result.stdout or result.stderr or "").strip()
    return output or "unavailable"


def _windows_file_version(path: Path) -> str:
    """
    Read Windows file version metadata without depending on PowerShell.
    """
    try:
        size = ctypes.windll.version.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return "unavailable"

        buffer = ctypes.create_string_buffer(size)
        ctypes.windll.version.GetFileVersionInfoW(str(path), 0, size, buffer)

        value = ctypes.c_void_p()
        value_len = wintypes.UINT()
        ctypes.windll.version.VerQueryValueW(
            buffer,
            "\\",
            ctypes.byref(value),
            ctypes.byref(value_len),
        )

        class VS_FIXEDFILEINFO(ctypes.Structure):
            _fields_ = [
                ("dwSignature", wintypes.DWORD),
                ("dwStrucVersion", wintypes.DWORD),
                ("dwFileVersionMS", wintypes.DWORD),
                ("dwFileVersionLS", wintypes.DWORD),
                ("dwProductVersionMS", wintypes.DWORD),
                ("dwProductVersionLS", wintypes.DWORD),
                ("dwFileFlagsMask", wintypes.DWORD),
                ("dwFileFlags", wintypes.DWORD),
                ("dwFileOS", wintypes.DWORD),
                ("dwFileType", wintypes.DWORD),
                ("dwFileSubtype", wintypes.DWORD),
                ("dwFileDateMS", wintypes.DWORD),
                ("dwFileDateLS", wintypes.DWORD),
            ]

        ffi = ctypes.cast(value, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
        major = ffi.dwProductVersionMS >> 16
        minor = ffi.dwProductVersionMS & 0xFFFF
        build = ffi.dwProductVersionLS >> 16
        patch = ffi.dwProductVersionLS & 0xFFFF
        return f"{major}.{minor}.{build}.{patch}"
    except Exception as exc:
        return f"unavailable ({type(exc).__name__})"


def browser_status() -> dict[str, str]:
    """
    Report the active browser/driver assumptions used by the pipeline.
    """
    chrome_candidates = [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    ]
    chrome_path = next((path for path in chrome_candidates if path.exists()), None)

    chrome_version = _windows_file_version(chrome_path) if chrome_path else "not found"

    chromedriver_version = (
        _run_version([str(CHROMEDRIVER_PATH), "--version"])
        if CHROMEDRIVER_PATH.exists()
        else "not found"
    )

    warning = ""
    if chrome_version != "not found" and chromedriver_version.startswith("ChromeDriver"):
        chrome_major = chrome_version.split(".", 1)[0]
        driver_major = chromedriver_version.split()[1].split(".", 1)[0]
        if chrome_major != driver_major:
            warning = (
                f"Chrome major version {chrome_major} does not match "
                f"ChromeDriver major version {driver_major}."
            )

    return {
        "chrome_path": str(chrome_path) if chrome_path else "not found",
        "chrome_version": chrome_version,
        "chromedriver_path": str(CHROMEDRIVER_PATH),
        "chromedriver_version": chromedriver_version,
        "warning": warning or "none",
    }


def pipeline_flow() -> list[dict[str, str]]:
    """
    High-level operational flow from initialization to output generation.
    """
    return [
        {
            "stage": "Configuration",
            "behavior": (
                "Load paths, season/team context, proxy settings, API keys, "
                "conference/team identity maps, and source-specific slug aliases."
            ),
            "streamlit_status": "partially represented",
        },
        {
            "stage": "HTTP/proxy routing",
            "behavior": (
                "Notebook safe_get routes selected hosts through Webshare proxy, "
                "bypasses ESPN/Odds API style hosts, and retries direct when the proxy fails."
            ),
            "streamlit_status": "not yet centralized in app",
        },
        {
            "stage": "Browser rendering",
            "behavior": (
                "Dynamic pages use Playwright helper with headless Chromium, NCAA warmup, "
                "custom headers, optional proxy, and table selector waits. Selenium support "
                "depends on ChromeDriver compatibility."
            ),
            "streamlit_status": "documented here; not yet exposed as refresh controls",
        },
        {
            "stage": "Sports Reference scraping",
            "behavior": (
                "Fetch team/player pages with cache-first behavior, polite delays, "
                "403/429 backoff, comment-table parsing, validation, and cache fallback."
            ),
            "streamlit_status": "src.scraper partially represents this",
        },
        {
            "stage": "ESPN ingestion",
            "behavior": (
                "Build ESPN team index, load scoreboard/injury APIs, validate empty responses, "
                "stamp neutral-site/tournament context, and fall back where possible."
            ),
            "streamlit_status": "not yet represented as live page logic",
        },
        {
            "stage": "Transformation",
            "behavior": (
                "Merge roster, per-game, totals, per-40, per-100, advanced stats, "
                "game logs, injuries, style/psych context, and identity normalization."
            ),
            "streamlit_status": "local file readers only; full transformation still notebook-owned",
        },
        {
            "stage": "Analytics/projections",
            "behavior": (
                "Build team/player profiles, OVR/NIL scores, matchup features, projections, "
                "market comparisons, confidence tiers, and betting edge outputs."
            ),
            "streamlit_status": "first-pass player card generator only; projections not extracted",
        },
        {
            "stage": "Output generation",
            "behavior": (
                "Write CSV/XLSX/parquet/json/jsonl, generated PNG cards, HTML dashboards, "
                "line snapshots, postmortems, and social/Discord post payloads."
            ),
            "streamlit_status": "viewers plus first-pass card generation",
        },
    ]


def known_gaps() -> list[str]:
    return [
        "The full notebook projection engine is not yet extracted into src.betting_engine.py.",
        "The app-native player card generator is a first-pass local-data version, not the full build_composite_radar_v9 pipeline.",
        "Team overview and conference dashboards are currently displayed from existing exports, not regenerated from Streamlit.",
        "Team-v-team graphics are currently file viewers; live matchup projection generation is not yet wired in.",
        "Proxy credentials still appear to be notebook/env driven; Streamlit secrets are not yet the single source of truth.",
        "ChromeDriver in TeamDump is version 114 while local Chrome is newer; Selenium flows need driver alignment or Selenium Manager.",
        "Sports Reference forced refresh can fail under 403/429 and should never delete old CSVs before a successful replacement.",
        "ESPN scoreboard/index validation exists in the notebook but is not yet surfaced as app health checks.",
    ]


def output_inventory() -> list[dict[str, str]]:
    roots = {
        "TeamDump": ROOT_DIR / "TeamDump",
        "PlayerCards": ROOT_DIR / "PlayerCards",
        "Conference_Ratings": ROOT_DIR / "Conference_Ratings",
        "Gameday Comps": ROOT_DIR / "Gameday Comps",
        "Live Scoreboard": ROOT_DIR / "Live Scoreboard",
        "bet_tracker": ROOT_DIR / "bet_tracker",
        "sr_cache_html": ROOT_DIR / "sr_cache_html",
    }

    rows = []
    for name, path in roots.items():
        if not path.exists():
            rows.append({"area": name, "path": str(path), "status": "missing"})
            continue

        file_count = sum(1 for item in path.rglob("*") if item.is_file())
        rows.append(
            {
                "area": name,
                "path": str(path),
                "status": f"{file_count} files",
            }
        )

    return rows
