"""
Smoke test for the running demo (`docker compose -f compose.demo.yaml up --build --wait`): calls every API route and
the Streamlit health endpoint, and exits non-zero if any check fails. Standard library only.

    python demo/smoke_demo.py
"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "http://127.0.0.1:8000/v1"
TOKEN = "napsedge-demo-token"  # public demo token (see compose.demo.yaml)


def call(path, *, token=TOKEN, body=None):
    request = urllib.request.Request(API + path, data=json.dumps(body).encode() if body else None, method="POST" if body else "GET")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    if body:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"null")


failures = []


def check(name, condition):
    print(("ok    " if condition else "FAIL  ") + name)
    if not condition:
        failures.append(name)


status, body = call("/health", token=None)
check("health is 200", status == 200)
status, body = call("/ready", token=None)
check("ready is 200 (migrated database)", status == 200 and body["status"] == "ready")
check("a request without a token is rejected (401)", call("/teams/resolve?query=Harbor%20State", token=None)[0] == 401)
check("a wrong token is rejected (401)", call("/teams/resolve?query=Harbor%20State", token="wrong")[0] == 401)

status, team = call("/teams/resolve?query=" + urllib.parse.quote("Harbor State"))
check("team resolve finds Harbor State", status == 200 and team["canonical_name"] == "Harbor State")
team_id = team.get("team_id")
check("team lookup by id", call(f"/teams/{team_id}")[0] == 200)
check("an unknown team is 404", call("/teams/resolve?query=Duke")[0] == 404)

player_id = urllib.parse.quote(f"team:{team_id}:season:2026:key:harbor state demo player 1", safe="")
status, player = call(f"/players/{player_id}/raw-statistics")
check("player raw statistics come from PostgreSQL", status == 200 and player["stats"] and player["stats"][0]["stat_table"] == "PerGame")

status, rankings = call("/rankings/net")
check("NET rankings list has the 8 demo teams", status == 200 and rankings["total_rows"] == 8)
status, rank = call(f"/rankings/net/teams/{team_id}")
check("Harbor State is NET #2", status == 200 and rank["net_rank"] == 2)

status, injuries = call(f"/teams/{team_id}/injuries")
check("injury report loads (2 impactful)", status == 200 and json.dumps(injuries).count("Demo Player") == 2)

features = {"avg_margin": 6.5, "recent_avg_margin": 4.0}
status, matchup = call("/matchups/feature-comparison", body={"team_a_slug": "harbor-state", "team_b_slug": "pine-ridge",
                                                             "team_a_features": features, "team_b_features": {**features, "avg_margin": 2.0}})
check("matchup comparison: Harbor State leads season margin by 4.5",
      status == 200 and matchup["deltas"]["avg_margin"] == 4.5 and matchup["key_edges"][0]["leader"] == "harbor-state")

with urllib.request.urlopen("http://127.0.0.1:8501/_stcore/health", timeout=10) as response:
    check("Streamlit is healthy", response.status == 200)

print(f"\n{'PASSED' if not failures else 'FAILED'}: {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
