"""
backend_client.py

Single place for all HTTP communication with the Astra harness backend.
Tools call methods here instead of using httpx directly — this makes
swapping out the backend URL or adding headers a one-line change.

The harness API surface (harness/main.py) that we map onto:
    point_to               -> POST /calibrate
    get_calibration_status -> GET  /calibrate/{job_id}
    abort_calibration      -> DELETE /calibrate/{job_id}
    get_current_orientation-> POST /capture/solve
    list_visible_objects   -> GET  /objects/visible
    manual_slew            -> POST /slew
    get_telescope_status   -> GET  /status
"""

import os

import httpx
from dotenv import load_dotenv

load_dotenv()

_base_url = os.getenv("BACKEND_BASE_URL")
if not _base_url:
    raise RuntimeError(
        "BACKEND_BASE_URL is not set. Copy mcp/.env.example to mcp/.env and point it at the "
        "harness (e.g. http://localhost:8000)."
    )

# Read backend URL once at import time
BACKEND_BASE_URL = _base_url.rstrip("/")

# Shared timeout so every request has a sensible limit.
_TIMEOUT = httpx.Timeout(10.0)
# /calibrate resolves the target synchronously via Simbad/Horizons before the
# job starts, which can legitimately take tens of seconds on a slow mirror.
_CALIBRATE_TIMEOUT = httpx.Timeout(90.0)
# /capture/solve captures and plate-solves a frame.
_SOLVE_TIMEOUT = httpx.Timeout(30.0)


def _client(timeout: httpx.Timeout = _TIMEOUT) -> httpx.Client:
    """Return a configured httpx client. Called per-request to stay simple."""
    return httpx.Client(base_url=BACKEND_BASE_URL, timeout=timeout)


def _request(
    method: str,
    path: str,
    *,
    json: dict | None = None,
    params: dict | None = None,
    timeout: httpx.Timeout = _TIMEOUT,
) -> dict:
    """
    Perform one backend call with consistent connection/timeout error handling.
    Returns parsed JSON on success; raises a readable RuntimeError otherwise.
    Every harness endpoint returns a JSON object.
    """
    with _client(timeout) as client:
        try:
            resp = client.request(method, path, json=json, params=params)
        except httpx.ConnectError:
            raise RuntimeError(f"Cannot reach backend at {BACKEND_BASE_URL}")
        except httpx.TimeoutException:
            raise RuntimeError("Backend request timed out")

    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"Backend returned {e.response.status_code}: {e.response.text}") from e
    return resp.json()


# ── Tool-specific methods ──────────────────────────────────────────────────


def point_to(target: str, resolver: str, input_body: dict) -> dict:
    """
    Tell the harness to start pointing the telescope at a named target.

    The MCP tool layer classifies the target ("horizons" vs "simbad") and
    builds input_body; we translate that vocabulary into the harness's
    POST /calibrate schema:

        resolver "horizons" -> category "planet"   (routes to JPL Horizons)
        resolver "simbad"   -> category "deep_sky" (routes to Simbad)

        input_body["location"] {lon, lat, elevation}
                            -> observer_lon / observer_lat / observer_elevation_km

    The harness resolves the name to RA/Dec, creates a calibration job and
    returns immediately with {"job_id": ..., "poll_hint": ...}.
    """
    category = "planet" if resolver == "horizons" else "deep_sky"

    body = dict(input_body)
    location = body.pop("location", None)

    payload: dict = {"target": target, "category": category}
    if isinstance(location, dict) and "lon" in location and "lat" in location:
        payload["observer_lon"] = float(location["lon"])
        payload["observer_lat"] = float(location["lat"])
        if location.get("elevation") is not None:
            payload["observer_elevation_km"] = float(location["elevation"])

    return _request("POST", "/calibrate", json=payload, timeout=_CALIBRATE_TIMEOUT)


def manual_slew(dra_arcsec: float, ddec_arcsec: float) -> dict:
    """
    Send a relative slew command to the mount (POST /slew).
    Argument validation (max 3600 arcsec) is done by the tool layer before
    this method is called, so we just forward cleanly.
    """
    return _request(
        "POST",
        "/slew",
        json={"delta_ra_arcsec": dra_arcsec, "delta_dec_arcsec": ddec_arcsec},
    )


def list_visible_objects(category: str = "all", min_altitude_deg: float = 10.0) -> dict:
    """Return celestial objects currently above the horizon (GET /objects/visible)."""
    return _request(
        "GET",
        "/objects/visible",
        params={"category": category, "min_altitude_deg": min_altitude_deg},
    )


def get_telescope_status() -> dict:
    """Return overall telescope system status (camera, solver, mount, active job)."""
    return _request("GET", "/status")


def get_current_orientation() -> dict:
    """
    Capture a frame and plate solve it right now (POST /capture/solve).
    Returns RA/Dec plus roll, fov, stars matched and solve quality metrics.
    """
    return _request("POST", "/capture/solve", timeout=_SOLVE_TIMEOUT)


def get_calibration_status(job_id: str) -> dict:
    """Poll the harness for the current state of a calibration job."""
    return _request("GET", f"/calibrate/{job_id}")


def abort_calibration(job_id: str) -> dict:
    """Abort a running calibration job (DELETE /calibrate/{job_id})."""
    return _request("DELETE", f"/calibrate/{job_id}")
