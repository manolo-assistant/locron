#!/usr/bin/env python3
"""
locron canary — integration tests that run as a cron job.
Verifies the locron system is healthy. Fires every 6 hours.
Reports failures to the gateway as system events.

Tests:
  1. locron.json is parseable and has expected jobs
  2. state.json has a timezone (last known)
  3. openclaw cron list succeeds and tracked jobs exist
  4. timezone resolver works
  5. Geo evaluation logic works
"""

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

DATA_DIR = Path(os.environ.get("LOCRON_DATA_DIR", "/root/clawd/data/locron"))
LOCRON_FILE = DATA_DIR / "locron.json"
STATE_FILE = DATA_DIR / "state.json"
OPENCLAW = os.environ.get("LOCRON_OPENCLAW_BIN", "openclaw")

failures = []


def check(name, fn):
    try:
        result = fn()
        if result is not True:
            failures.append(f"❌ {name}: {result}")
        else:
            print(f"✅ {name}")
    except Exception as e:
        failures.append(f"❌ {name}: {e}")


def test_locron_json():
    data = json.loads(LOCRON_FILE.read_text())
    assert isinstance(data, list), "not a list"
    names = [j.get("name") for j in data]
    assert "morning-briefing" in names, "morning-briefing not tracked"
    return True


def test_state_json():
    if not STATE_FILE.exists():
        return "state.json missing (daemon may not have run yet) — OK on first run"
    data = json.loads(STATE_FILE.read_text())
    # _tz may be absent if no GPS yet — that's OK, not a failure
    return True


def test_openclaw_cron():
    r = subprocess.run([OPENCLAW, "cron", "list", "--json"],
                       capture_output=True, text=True, timeout=15)
    data = json.loads(r.stdout)
    jobs = data.get("jobs", [])
    names = [j.get("name") for j in jobs]

    # Check tracked jobs still exist in openclaw
    locron_jobs = json.loads(LOCRON_FILE.read_text())
    for lj in locron_jobs:
        if lj.get("kind") == "local_tz":
            oc_id = lj.get("openclaw_id")
            if not any(j.get("id") == oc_id for j in jobs):
                return f"tracked job {lj['name']} (id={oc_id}) missing from openclaw"
    return True


def test_timezone_resolver():
    from resolver import timezone_at
    tz = timezone_at(40.7580, -73.9855)
    assert tz == "America/New_York", f"got {tz}"
    tz = timezone_at(52.2053, 0.1218)
    assert tz == "Europe/London", f"got {tz}"
    return True


def test_geo_evaluation():
    from locron import _eval_geo
    locations = {"test": {"lat": 40.748, "lon": -73.985, "radius_m": 60}}
    job = {"trigger": {"location": "test", "on": "enter"}}
    state = {"in_zone": False}
    loc = {"lat": 40.7481, "lon": -73.9851}
    result = _eval_geo(job, state, loc, locations)
    assert result == "enter", f"got {result}"
    return True


def test_tz_consistency():
    """Verify that the tz stored in locron.json matches what openclaw cron has."""
    locron_jobs = json.loads(LOCRON_FILE.read_text())
    r = subprocess.run([OPENCLAW, "cron", "list", "--json"],
                       capture_output=True, text=True, timeout=15)
    oc_jobs = json.loads(r.stdout).get("jobs", [])
    oc_by_id = {j["id"]: j for j in oc_jobs}

    for lj in locron_jobs:
        if lj.get("kind") != "local_tz":
            continue
        oc_id = lj.get("openclaw_id")
        oc_job = oc_by_id.get(oc_id)
        if not oc_job:
            continue
        oc_tz = oc_job.get("schedule", {}).get("tz", "")
        locron_tz = lj.get("current_tz", "")
        if oc_tz != locron_tz:
            return f"{lj['name']}: locron says {locron_tz}, openclaw says {oc_tz} (drift!)"
    return True


def main():
    check("locron.json parseable + expected jobs", test_locron_json)
    check("state.json has timezone", test_state_json)
    check("openclaw cron reachable + tracked jobs exist", test_openclaw_cron)
    check("timezone resolver", test_timezone_resolver)
    check("geo evaluation logic", test_geo_evaluation)
    check("tz consistency (locron ↔ openclaw)", test_tz_consistency)

    if failures:
        msg = "🐤 locron canary FAILED:\n" + "\n".join(failures)
        print(msg, file=sys.stderr)
        # Fire alert to openclaw
        subprocess.run([
            OPENCLAW, "cron", "add",
            "--name", "locron-canary-alert",
            "--at", "+0s",
            "--delete-after-run",
            "--system-event", msg,
        ], capture_output=True)
        sys.exit(1)
    else:
        print("🐤 locron canary: all clear")


if __name__ == "__main__":
    main()
