"""
Location provider: Dawarich (self-hosted GPS tracker).
Fetches the most recent GPS point from the Dawarich API.
"""

import json
import time
import urllib.request
import urllib.parse


def get_location(api_url: str = "http://localhost:3000",
                 api_key: str = "",
                 max_age_s: int = 86400) -> dict | None:
    """
    Fetch latest GPS point from Dawarich.

    Returns:
        {"lat": float, "lon": float, "timestamp": int} or None
    """
    now = time.time()
    start = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now - max_age_s))
    end = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now))

    url = f"{api_url}/api/v1/points?start_at={urllib.parse.quote(start)}&end_at={urllib.parse.quote(end)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            points = json.loads(resp.read())
            if points and isinstance(points, list):
                p = points[0]
                return {
                    "lat": float(p["latitude"]),
                    "lon": float(p["longitude"]),
                    "timestamp": int(time.time()),
                }
    except Exception:
        pass
    return None
