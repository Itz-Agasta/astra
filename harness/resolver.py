from __future__ import annotations

import logging

from .config import observer

log = logging.getLogger(__name__)

# Valid categories for the resolver
CATEGORIES = ("planet", "comet", "asteroid", "star", "deep_sky", "auto")

# NAIF IDs -- NASA's standard identifiers for solar system objects
# (Navigation and Ancillary Information Facility / SPICE system)
# https://naif.jpl.nasa.gov/pub/naif/toolkit_docs/C/req/naif_ids.html
#
# JPL Horizons returns "Ambiguous target name" for most planet names
# because both the planet and its barycenter exist. For example, querying
# "Jupiter" returns both "Jupiter Barycenter" (ID 5) and "Jupiter"
# (ID 599). NAIF IDs are the only unambiguous way to specify which one.
#
# Agents can send either names ("Jupiter") or NAIF IDs ("599").
# Names get mapped here; IDs (or unknown names) pass through directly.
NAIF_IDS: dict[str, str] = {
    "sun": "10",
    "mercury": "199",
    "venus": "299",
    "earth": "399",
    "moon": "301",
    "mars": "499",
    "jupiter": "599",
    "saturn": "699",
    "uranus": "799",
    "neptune": "899",
    "pluto": "999",
    "ceres": "2000001",
    "vesta": "2000004",
    "io": "501",
    "europa": "502",
    "ganymede": "503",
    "callisto": "504",
    "titan": "606",
    "triton": "801",
}


# TODO: Stellarium demo fallback
# If Horizons/Simbad are slow or unavailable during demo, fall back to
# Stellarium's /api/objects/info endpoint for instant response.
# This only works in simulation mode (Stellarium running).
# Real hardware path always uses Horizons/Simbad.
async def resolve(
    target: str,
    category: str = "auto",
    observer_lon: float | None = None,
    observer_lat: float | None = None,
    observer_elevation_km: float | None = None,
) -> tuple[float, float, str]:
    """
    Resolve an astronomical object name to (RA, Dec, source).

    Args:
        target: Object name (e.g., "Jupiter", "Betelgeuse", "M42")
        category: Object type for routing (planet/comet/asteroid/star/deep_sky/auto)
        observer_lon: Observer longitude in degrees. If None, uses config default.
        observer_lat: Observer latitude in degrees. If None, uses config default.
        observer_elevation_km: Observer elevation in km. If None, uses config default.

    Returns:
        (ra_deg, dec_deg, source) where source is 'horizons', 'simbad', or 'stellarium'

    Raises:
        ValueError: If object cannot be resolved
    """
    import asyncio

    if category not in CATEGORIES:
        raise ValueError(f"Invalid category '{category}'. Must be one of: {CATEGORIES}")

    # Build observer location dict for Horizons
    # For dev override with env vars CCE_OBSERVER_LON/LAT/ELEVATION_KM
    # In prod we expect Agent to send this info.
    observer_location = {
        "lon": observer_lon if observer_lon is not None else observer.longitude,
        "lat": observer_lat if observer_lat is not None else observer.latitude,
        "elevation": (
            observer_elevation_km if observer_elevation_km is not None else observer.elevation_km
        ),
    }

    loop = asyncio.get_running_loop()

    if category in ("planet", "comet", "asteroid"):
        return await loop.run_in_executor(None, _resolve_horizons, target, observer_location)

    if category in ("star", "deep_sky"):
        return await loop.run_in_executor(None, _resolve_simbad, target, observer_location)

    # category == "auto": try Simbad first, fall back to Horizons
    try:
        return await loop.run_in_executor(None, _resolve_simbad, target, observer_location)
    except ValueError:
        log.info(f"Simbad failed for '{target}', trying Horizons")
        return await loop.run_in_executor(None, _resolve_horizons, target, observer_location)


def _apparent_place(
    ra_deg: float, dec_deg: float, observer_location: dict[str, float]
) -> tuple[float, float]:
    """Astrometric ICRS -> apparent place: where the object is actually seen.

    Horizons and Simbad both hand back *astrometric* coordinates, but a
    telescope -- and Stellarium -- sees the *apparent* position, displaced by
    annual aberration as the Earth carries the observer sideways at 30 km/s.
    That displacement reaches 20.5", and the plate solver cannot absorb it:
    tetra3 matches against raw catalog positions, so it reports the boresight
    in the catalog frame. Skipping this correction leaves the loop converging
    to a point ~20" from the object and reporting the error as if it had not,
    which quietly spends two thirds of a 30" threshold.

    No parallax is applied here and none is double-counted: a SkyCoord with no
    distance is treated as infinitely far, so the GCRS transform contributes
    aberration alone. Horizons already handled light-time and topocentric
    parallax using the observer location it was given.
    """
    import astropy.units as u
    from astropy.coordinates import GCRS, EarthLocation, SkyCoord
    from astropy.time import Time

    now = Time.now()
    site = EarthLocation(
        lat=observer_location["lat"] * u.deg,
        lon=observer_location["lon"] * u.deg,
        height=observer_location["elevation"] * 1000 * u.m,
    )
    # Observer velocity, so we pick up diurnal aberration (~0.3") as well.
    pos, vel = site.get_gcrs_posvel(now)
    apparent = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg, frame="icrs").transform_to(
        GCRS(obstime=now, obsgeoloc=pos, obsgeovel=vel)
    )
    return float(apparent.ra.deg) % 360.0, float(apparent.dec.deg)


def _resolve_horizons(target: str, observer_location: dict[str, float]) -> tuple[float, float, str]:
    """Resolve via JPL Horizons. Works for planets, comets, asteroids."""
    # Imported here, not at module scope: astroquery pulls in astropy and costs
    # ~47 MB of RSS. A harness that never resolves should never pay for it.
    from astropy.time import Time
    from astroquery.jplhorizons import Horizons

    # Use NAIF ID if available to avoid ambiguity
    # for ~15 most common ambiguous query names
    key = target.strip().lower()
    horizons_id = NAIF_IDS.get(key, target)

    try:
        obj = Horizons(
            id=horizons_id,
            location=observer_location,
            epochs=Time.now().jd,
        )
        eph = obj.ephemerides()  # type: ignore[operator]
        ra = float(eph["RA"][0])
        dec = float(eph["DEC"][0])
        ra, dec = _apparent_place(ra, dec, observer_location)
        log.info(f"Horizons resolved '{target}': RA={ra:.4f} Dec={dec:.4f} (apparent)")
        return ra, dec, "horizons"

    except Exception as exc:
        raise ValueError(f"JPL Horizons could not resolve '{target}': {exc}") from exc


def _apply_proper_motion(ra_deg: float, dec_deg: float, row) -> tuple[float, float]:
    """Carry a J2000 catalog position forward to today, if Simbad gave us a PM.

    Deep-sky objects have no proper motion and nearby stars have a lot of it;
    both come back through the same path, so a missing or masked column is
    normal and simply means "leave it where it is".
    """
    import astropy.units as u
    import numpy as np
    from astropy.coordinates import SkyCoord
    from astropy.time import Time

    try:
        pm_ra = float(row["pmra"][0])
        pm_dec = float(row["pmdec"][0])
    except KeyError, TypeError, ValueError:
        return ra_deg, dec_deg
    if not (np.isfinite(pm_ra) and np.isfinite(pm_dec)) or (pm_ra == 0.0 and pm_dec == 0.0):
        return ra_deg, dec_deg

    moved = SkyCoord(
        ra=ra_deg * u.deg,
        dec=dec_deg * u.deg,
        pm_ra_cosdec=pm_ra * u.mas / u.yr,  # Simbad's pmra already carries cos(dec)
        pm_dec=pm_dec * u.mas / u.yr,
        obstime=Time("J2000"),
        frame="icrs",
    ).apply_space_motion(new_obstime=Time.now())
    return float(moved.ra.deg) % 360.0, float(moved.dec.deg)


def _resolve_simbad(target: str, observer_location: dict[str, float]) -> tuple[float, float, str]:
    """Resolve via Simbad. Works for stars, nebulae, galaxies, clusters."""
    from astroquery.simbad import Simbad

    try:
        query = Simbad()
        # Simbad quotes positions at epoch J2000. Fast movers have gone a long
        # way since: Vega's 350 mas/yr is 9" by 2026, a third of the threshold.
        try:
            query.add_votable_fields("pmra", "pmdec")
        except Exception as exc:  # older astroquery, different field names
            log.warning(f"Simbad proper-motion fields unavailable: {exc}")

        result = query.query_object(target)
        if result is None:
            raise ValueError(f"Object '{target}' not found in Simbad catalog")

        # Simbad returns RA/Dec in degrees (lowercase column names)
        ra = float(result["ra"][0])
        dec = float(result["dec"][0])
        ra, dec = _apply_proper_motion(ra, dec, result)
        ra, dec = _apparent_place(ra, dec, observer_location)

        log.info(f"Simbad resolved '{target}': RA={ra:.4f} Dec={dec:.4f} (apparent)")
        return ra, dec, "simbad"

    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Simbad could not resolve '{target}': {exc}") from exc
