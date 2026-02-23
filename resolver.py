"""
Location resolver: GPS → timezone, geofence checks.

Uses tzfpy (Rust-based, no numpy/numba/scipy) for timezone lookups.
"""

import math
from tzfpy import get_tz


def timezone_at(lat: float, lon: float) -> str | None:
    """Resolve IANA timezone from GPS coordinates."""
    return get_tz(lon, lat)  # tzfpy takes (lng, lat)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance in meters between two GPS points."""
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def in_zone(lat: float, lon: float, zone: dict) -> bool:
    """Check if point is within a named zone's radius."""
    return haversine_m(lat, lon, zone["lat"], zone["lon"]) <= zone.get("radius_m", 50)
