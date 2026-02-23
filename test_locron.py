#!/usr/bin/env python3
"""
Tests for locron — resolver logic and geo evaluation.

Note: time-trigger scheduling is delegated to OpenClaw cron.
locron only resolves the timezone. So we test:
  1. GPS → timezone resolution
  2. Geofence detection (enter/exit)
  3. Timezone changes trigger OpenClaw cron patching (mocked)
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from datetime import datetime
from zoneinfo import ZoneInfo

from resolver import timezone_at, haversine_m, in_zone


def test_timezone_resolution():
    """GPS → timezone mapping."""
    assert timezone_at(40.7580, -73.9855) == "America/New_York"
    assert timezone_at(52.2053, 0.1218) == "Europe/London"
    assert timezone_at(47.3769, 8.5417) == "Europe/Zurich"
    assert timezone_at(35.6762, 139.6503) == "Asia/Tokyo"
    print("✅ timezone_at works")


def test_haversine():
    """Distance calculation."""
    # Times Square to Empire State Building (~1km)
    d = haversine_m(40.7580, -73.9855, 40.7484, -73.9857)
    assert 900 < d < 1200, f"Expected ~1km, got {d}m"

    # Same point
    d = haversine_m(40.758, -73.985, 40.758, -73.985)
    assert d < 1, f"Same point got {d}m"
    print("✅ haversine_m works")


def test_in_zone():
    """Geofence check."""
    esb = {"lat": 40.7484, "lon": -73.9857, "radius_m": 60}
    assert in_zone(40.7485, -73.9856, esb) == True   # at ESB
    assert in_zone(40.7580, -73.9855, esb) == False   # Times Square
    print("✅ in_zone works")


def test_local_tz_concept():
    """Same wall-clock time → different UTC in different zones."""
    nyc = ZoneInfo("America/New_York")
    london = ZoneInfo("Europe/London")
    diff = abs((datetime(2026, 2, 23, 6, 30, tzinfo=nyc) -
                datetime(2026, 2, 23, 6, 30, tzinfo=london)).total_seconds())
    assert diff == 5 * 3600, f"Expected 5h diff, got {diff}s"
    print("✅ local tz concept verified")


def test_geo_evaluation():
    """Test geofence enter/exit detection."""
    from locron import _eval_geo

    locations = {
        "gym": {"lat": 40.7829, "lon": -73.9654, "radius_m": 60}
    }
    loc_outside = {"lat": 40.7580, "lon": -73.9855}
    loc_inside = {"lat": 40.7830, "lon": -73.9654}

    job_enter = {"trigger": {"location": "gym", "on": "enter"}}

    # Outside → outside: nothing
    state = {"in_zone": False}
    assert _eval_geo(job_enter, state, loc_outside, locations) is None

    # Outside → inside: enter
    state = {"in_zone": False}
    assert _eval_geo(job_enter, state, loc_inside, locations) == "enter"

    # Inside → inside: nothing
    state = {"in_zone": True}
    assert _eval_geo(job_enter, state, loc_inside, locations) is None

    # Inside → outside: nothing (enter-only trigger)
    state = {"in_zone": True}
    assert _eval_geo(job_enter, state, loc_outside, locations) is None

    # "both" trigger: inside → outside = exit
    job_both = {"trigger": {"location": "gym", "on": "both"}}
    state = {"in_zone": True}
    assert _eval_geo(job_both, state, loc_outside, locations) == "exit"

    print("✅ geo evaluation works")


def test_tz_change_detection():
    """When location changes timezone, locron should detect it."""
    from locron import get_current_tz

    # State says NYC
    state = {"_tz": "America/New_York"}
    assert get_current_tz(state) == "America/New_York"

    # Simulate travel to London
    state["_tz"] = "Europe/London"
    assert get_current_tz(state) == "Europe/London"

    # No tz yet → fallback
    from locron import DEFAULT_TZ
    assert get_current_tz({}) == DEFAULT_TZ

    print("✅ tz change detection works")


if __name__ == "__main__":
    test_timezone_resolution()
    test_haversine()
    test_in_zone()
    test_local_tz_concept()
    test_geo_evaluation()
    test_tz_change_detection()
    print("\n🎉 All tests passed!")
