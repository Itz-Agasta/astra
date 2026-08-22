from __future__ import annotations

import json
import logging

import httpx

from .config import cfg

log = logging.getLogger(__name__)


async def slew(ra_deg: float, dec_deg: float) -> None:
    """Move Stellarium view to exact RA/Dec via POST /api/main/view.

    Steps:
      1. Disable tracking (view snaps back if tracking=true)
      2. POST J2000 vector to /api/main/view
    """
    import math

    base = cfg.simulation.url

    async with httpx.AsyncClient(timeout=5.0) as client:
        # 1. Disable tracking -- without this, POST returns 200 but view snaps back
        try:
            await client.post(
                f"{base}/api/stelproperty/set",
                data={"id": "StelMovementMgr.tracking", "value": "false"},
            )
        except Exception as exc:
            log.warning(f"Could not disable tracking: {exc}")

        # 2. POST J2000 vector
        ra_rad = math.radians(ra_deg)
        dec_rad = math.radians(dec_deg)
        vec = [
            math.cos(dec_rad) * math.cos(ra_rad),
            math.cos(dec_rad) * math.sin(ra_rad),
            math.sin(dec_rad),
        ]
        resp = await client.post(
            f"{base}/api/main/view",
            data={"j2000": json.dumps(vec)},
        )
        if resp.status_code not in (200, 204):
            raise RuntimeError(
                f"Stellarium POST /api/main/view failed ({resp.status_code}): {resp.text[:200]}"
            )

    log.info(f"Stellarium slew → RA={ra_deg:.4f}° Dec={dec_deg:.4f}°")


async def zoom(fov_deg: float) -> None:
    """Set Stellarium field of view (zoom level) in degrees."""
    base = cfg.simulation.url
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(f"{base}/api/main/fov", data={"fov": fov_deg})
        if resp.status_code not in (200, 204):
            log.warning(f"Stellarium POST /api/main/fov returned {resp.status_code}")
        else:
            log.info(f"Stellarium zoom → FOV={fov_deg}°")


async def get_view() -> tuple[float, float]:
    """Get current Stellarium view direction as (RA, Dec) in degrees."""
    import math

    base = cfg.simulation.url
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(f"{base}/api/main/view")
        resp.raise_for_status()
        data = resp.json()

        j2000_raw = data.get("j2000", data)
        if isinstance(j2000_raw, str):
            j2000_raw = json.loads(j2000_raw)
        x, y, z = float(j2000_raw[0]), float(j2000_raw[1]), float(j2000_raw[2])

        ra_rad = math.atan2(y, x)
        dec_rad = math.asin(max(-1.0, min(1.0, z)))
        ra_deg = math.degrees(ra_rad) % 360
        dec_deg = math.degrees(dec_rad)
        return ra_deg, dec_deg


async def get_location() -> tuple[float, float, float]:
    """Stellarium's own observer location as (latitude, longitude, elevation_km).

    Stellarium reports altitude in metres; everything here works in km.
    """
    base = cfg.simulation.url
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(f"{base}/api/main/status")
        resp.raise_for_status()
        loc = resp.json()["location"]
        return (
            float(loc["latitude"]),
            float(loc["longitude"]),
            float(loc.get("altitude", 0.0)) / 1000.0,
        )


async def get_fov() -> float:
    """Get current Stellarium field of view in degrees."""
    base = cfg.simulation.url
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(f"{base}/api/main/status")
        resp.raise_for_status()
        data = resp.json()
        return float(data["view"]["fov"])


# Stellarium measures simulation speed in Julian days per second.
REAL_TIME_RATE = 1.0 / 86400.0


async def get_time_rate() -> float:
    """Current simulation speed in JD/s. 0 means the clock is paused."""
    base = cfg.simulation.url
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(f"{base}/api/main/status")
        resp.raise_for_status()
        return float(resp.json()["time"]["timerate"])


async def set_time_rate(rate: float) -> None:
    """Set simulation speed. 0 freezes the sky, REAL_TIME_RATE resumes it."""
    base = cfg.simulation.url
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(f"{base}/api/main/time", data={"timerate": rate})
        if resp.status_code not in (200, 204):
            raise RuntimeError(
                f"Stellarium POST /api/main/time failed ({resp.status_code}): {resp.text[:200]}"
            )
    log.info(f"Stellarium time rate → {rate} JD/s")


async def do_action(action_id: str) -> None:
    """Trigger a Stellarium action -- the things bound to keyboard shortcuts.

    Useful ones: actionReturn_To_Current_Time (jump the clock to now, though
    it leaves the time rate alone), actionSet_Time_Rate_Zero.
    """
    base = cfg.simulation.url
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(f"{base}/api/stelaction/do", data={"id": action_id})
        if resp.status_code not in (200, 204):
            raise RuntimeError(
                f"Stellarium action '{action_id}' failed ({resp.status_code}): {resp.text[:200]}"
            )
    log.info(f"Stellarium action → {action_id}")


async def get_object_position(name: str) -> tuple[float, float] | None:
    """Where Stellarium actually draws an object, as J2000 (RA, Dec) degrees.

    Simulator ground truth -- only meaningful for checking our own work, never
    as an input to the control loop. Returns None if Stellarium does not know
    the object.
    """
    base = cfg.simulation.url
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(f"{base}/api/objects/info", params={"name": name, "format": "json"})
        if resp.status_code != 200:
            return None
        data = resp.json()
        if "raJ2000" not in data or "decJ2000" not in data:
            return None
        return float(data["raJ2000"]) % 360.0, float(data["decJ2000"])
