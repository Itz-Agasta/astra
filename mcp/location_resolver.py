"""
location_resolver.py

Resolves the observer's location automatically using OpenStreetMap
(Nominatim for geocoding, OpenTopoData/SRTM for elevation) — instead of
asking Claude / the user to type coordinates in chat.

Priority order when a tool needs a location:
    1. Explicit lon/lat passed straight into input_body["location"]
       (kept for backwards-compat / manual overrides).
    2. A free-text `location_query` (e.g. "Srirampur, West Bengal, India")
       which gets geocoded via Nominatim.
"""

import time
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()

NOMINATIM_BASE = "https://nominatim.openstreetmap.org"
OPENTOPO_BASE = "https://api.opentopodata.org/v1/srtm90m"
NOMINATIM_RATE_LIMIT = 1.0  # seconds — Nominatim's usage policy requires <=1 req/s
NOMINATIM_USER_AGENT = "astra-mcp/0.1 (telescope-control)"

_last_nominatim_call = 0.0
_geocode_cache: dict[str, dict[str, Any]] = {}
_elevation_cache: dict[tuple[float, float], float] = {}


def _rate_limit_nominatim() -> None:
    """Nominatim's public instance requires <=1 request/second."""
    global _last_nominatim_call
    elapsed = time.time() - _last_nominatim_call
    if elapsed < NOMINATIM_RATE_LIMIT:
        time.sleep(NOMINATIM_RATE_LIMIT - elapsed)
    _last_nominatim_call = time.time()


def geocode_location(query: str) -> dict[str, Any] | None:
    """
    Geocode a free-text place name (city, address, landmark) using the
    OpenStreetMap Nominatim API.

    Returns {lon, lat, display_name, country} or None if nothing was found.
    """
    query = query.strip()
    if not query:
        return None

    cache_key = query.lower()
    if cache_key in _geocode_cache:
        return _geocode_cache[cache_key]

    _rate_limit_nominatim()

    params = {"q": query, "format": "json", "limit": 1, "addressdetails": 1}
    headers = {"User-Agent": NOMINATIM_USER_AGENT}

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(f"{NOMINATIM_BASE}/search", params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError, ValueError:
        return None

    if not data:
        return None

    result = data[0]
    location = {
        "lon": float(result["lon"]),
        "lat": float(result["lat"]),
        "display_name": result.get("display_name", query),
        "country": result.get("address", {}).get("country", ""),
    }

    _geocode_cache[cache_key] = location
    return location


def get_elevation(lat: float, lon: float) -> float:
    """
    Look up elevation in kilometres for given coordinates using OpenTopoData
    (SRTM 90m dataset, free/open, no API key required).

    Falls back to 0.0 km if the lookup fails for any reason.
    """
    cache_key = (round(lat, 4), round(lon, 4))
    if cache_key in _elevation_cache:
        return _elevation_cache[cache_key]

    params = {"locations": f"{lat},{lon}"}
    elevation_km = 0.0

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(OPENTOPO_BASE, params=params)
            resp.raise_for_status()
            data = resp.json()
            elevation_m = data["results"][0].get("elevation", 0.0) or 0.0
            elevation_km = elevation_m / 1000.0
    except httpx.HTTPError, ValueError, KeyError, IndexError:
        elevation_km = 0.0

    _elevation_cache[cache_key] = elevation_km
    return elevation_km


def resolve_location(
    location_query: str | None = None, explicit_location: dict | None = None
) -> dict:
    """
    Resolve the observer's location with no back-and-forth with Claude/the user.

    Resolution order:
        1. explicit_location (already has lon/lat) — used as-is.
        2. location_query — geocoded via Nominatim, elevation via OpenTopoData.
    """
    if explicit_location and "lon" in explicit_location and "lat" in explicit_location:
        return {
            "lon": float(explicit_location["lon"]),
            "lat": float(explicit_location["lat"]),
            "elevation": float(explicit_location.get("elevation", 0.0)),
        }

    if location_query:
        geo = geocode_location(location_query)
        if not geo:
            raise ValueError(f"Could not resolve location via OpenStreetMap: '{location_query}'")
        elevation = get_elevation(geo["lat"], geo["lon"])
        return {
            "lon": geo["lon"],
            "lat": geo["lat"],
            "elevation": elevation,
            "display_name": geo["display_name"],
            "country": geo["country"],
        }

    raise ValueError(
        "Observer location required: provide 'location_query' (place name, e.g. 'Kolkata, India') "
        "or input_body['location'] with explicit lon/lat/elevation"
    )
