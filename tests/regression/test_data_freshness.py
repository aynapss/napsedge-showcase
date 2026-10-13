"""
Phase 9E-3: the operator's data-freshness report (`src.services.freshness`, `entrypoints data-freshness`). No API route is added.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from src.services.freshness import FreshnessConfigurationError, data_freshness, max_age_hours

NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)


def aged(path, hours: float):
    path.write_text("x", encoding="utf-8")
    ts = (NOW - timedelta(hours=hours)).timestamp()
    os.utime(path, (ts, ts))
    return path


def test_fresh_caches_are_ok_and_the_report_names_no_path(tmp_path):
    rankings = aged(tmp_path / "net_rankings.csv", 3)
    injuries = [aged(tmp_path / "a_injuries.json", 30), aged(tmp_path / "b_injuries.json", 5)]
    report = data_freshness(now=lambda: NOW, rankings_path=rankings, injury_files=injuries, environ={})
    assert report["status"] == "ok" and report["stale"] == [] and report["max_age_hours"] == 48
    assert report["rankings"] == {"present": True, "as_of": "2026-10-09T09:00:00Z", "age_hours": 3.0}
    assert report["injuries"]["files"] == 2 and report["injuries"]["newest_age_hours"] == 5.0 and report["injuries"]["oldest_age_hours"] == 30.0
    assert str(tmp_path) not in json.dumps(report) and "net_rankings.csv" not in json.dumps(report)


@pytest.mark.parametrize("case, stale", [("old_rankings", ["rankings"]), ("old_injuries", ["injuries"]), ("missing_rankings", ["rankings"]),
                                         ("no_injury_files", ["injuries"])])
def test_old_or_missing_data_is_stale(tmp_path, case, stale):
    rankings = aged(tmp_path / "r.csv", 100 if case == "old_rankings" else 1)
    if case == "missing_rankings":
        rankings = tmp_path / "absent.csv"
    files = [] if case == "no_injury_files" else [aged(tmp_path / "x_injuries.json", 100 if case == "old_injuries" else 1)]
    report = data_freshness(now=lambda: NOW, rankings_path=rankings, injury_files=files, environ={})
    assert report["status"] == "stale" and report["stale"] == stale


def test_injury_staleness_follows_the_newest_file_not_the_oldest(tmp_path):
    files = [aged(tmp_path / "old_injuries.json", 500), aged(tmp_path / "new_injuries.json", 2)]
    report = data_freshness(now=lambda: NOW, rankings_path=aged(tmp_path / "r.csv", 1), injury_files=files, environ={})
    assert report["status"] == "ok"


@pytest.mark.parametrize("raw, expected", [("", 48), ("1", 1), ("720", 720), ("24", 24)])
def test_the_threshold_setting_has_bounds(raw, expected):
    assert max_age_hours({"NAPSEDGE_DATA_MAX_AGE_HOURS": raw}) == expected


@pytest.mark.parametrize("raw", ["0", "721", "-5", "1.5", "abc"])
def test_a_malformed_threshold_is_refused_without_echoing_it(raw):
    with pytest.raises(FreshnessConfigurationError) as exc:
        max_age_hours({"NAPSEDGE_DATA_MAX_AGE_HOURS": raw})
    assert raw not in str(exc.value).replace("1 to 720", "")


def test_the_operator_command_exits_10_when_stale_and_64_on_a_bad_setting(monkeypatch, capsys, tmp_path):
    from src.deployment import entrypoints

    monkeypatch.setenv("NAPSEDGE_NET_RANKINGS_CACHE_PATH", str(tmp_path / "absent.csv"))
    with pytest.raises(SystemExit) as exc:
        entrypoints.main(["data-freshness"])
    report = json.loads(capsys.readouterr().out)
    assert exc.value.code == 10 and "rankings" in report["stale"] and str(tmp_path) not in json.dumps(report)
    monkeypatch.setenv("NAPSEDGE_DATA_MAX_AGE_HOURS", "9999")
    with pytest.raises(SystemExit) as exc:
        entrypoints.main(["data-freshness"])
    assert exc.value.code == 64 and "9999" not in capsys.readouterr().out


def test_the_api_route_set_is_unchanged_by_this_unit():
    from src.api.main import create_app

    paths = set(create_app(database_url="sqlite+pysqlite:///:memory:").openapi()["paths"])
    assert not any("fresh" in p for p in paths)
