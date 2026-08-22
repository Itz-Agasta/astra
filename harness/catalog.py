"""
catalog.py — What's up right now.

Backs GET /objects/visible (MCP: list_visible_objects). Deliberately offline:
a curated static catalog for stars and deep sky, plus astropy's builtin
ephemeris for planets. No Horizons, no Simbad, no IERS download — this
endpoint must answer instantly even when the venue wifi is dead.

Coordinates here are approximate J2000 and are for *browsing only*. Once the
user picks a target, point_to() re-resolves it properly through resolver.py.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Planets we can compute from astropy's builtin (analytic, offline) ephemeris.
# The Sun is excluded on purpose — nobody points a telescope at it.
PLANETS: tuple[str, ...] = (
    "moon",
    "mercury",
    "venus",
    "mars",
    "jupiter",
    "saturn",
    "uranus",
    "neptune",
)

_PLANET_MAG = {
    "moon": -12.7,
    "mercury": -0.4,
    "venus": -4.2,
    "mars": 0.7,
    "jupiter": -2.2,
    "saturn": 0.6,
    "uranus": 5.7,
    "neptune": 7.8,
}


@dataclass(frozen=True)
class CatalogEntry:
    name: str
    category: str  # "star" | "deep_sky"
    ra: float  # J2000 degrees
    dec: float  # J2000 degrees
    magnitude: float
    description: str


# Bright naked-eye stars — the ones people actually ask for by name.
STARS: tuple[CatalogEntry, ...] = (
    CatalogEntry("Sirius", "star", 101.2875, -16.7161, -1.46, "Brightest star in the night sky"),
    CatalogEntry("Canopus", "star", 95.9880, -52.6957, -0.74, "Second brightest star"),
    CatalogEntry("Arcturus", "star", 213.9153, 19.1824, -0.05, "Orange giant in Bootes"),
    CatalogEntry("Vega", "star", 279.2347, 38.7837, 0.03, "Summer Triangle, in Lyra"),
    CatalogEntry("Capella", "star", 79.1723, 45.9980, 0.08, "Yellow giant in Auriga"),
    CatalogEntry("Rigel", "star", 78.6345, -8.2017, 0.13, "Blue supergiant, Orion's foot"),
    CatalogEntry("Procyon", "star", 114.8255, 5.2250, 0.34, "In Canis Minor"),
    CatalogEntry("Betelgeuse", "star", 88.7929, 7.4071, 0.50, "Red supergiant, Orion's shoulder"),
    CatalogEntry("Altair", "star", 297.6958, 8.8683, 0.77, "Summer Triangle, in Aquila"),
    CatalogEntry("Aldebaran", "star", 68.9802, 16.5093, 0.85, "Red giant, eye of Taurus"),
    CatalogEntry("Antares", "star", 247.3519, -26.4320, 1.09, "Red supergiant, heart of Scorpius"),
    CatalogEntry("Spica", "star", 201.2983, -11.1613, 1.04, "Blue giant in Virgo"),
    CatalogEntry("Pollux", "star", 116.3290, 28.0262, 1.14, "Orange giant in Gemini"),
    CatalogEntry("Deneb", "star", 310.3580, 45.2803, 1.25, "Summer Triangle, in Cygnus"),
    CatalogEntry("Regulus", "star", 152.0930, 11.9672, 1.40, "Heart of Leo"),
    CatalogEntry("Castor", "star", 113.6495, 31.8883, 1.58, "Six-star system in Gemini"),
    CatalogEntry("Polaris", "star", 37.9545, 89.2641, 1.98, "The North Star"),
    CatalogEntry("Albireo", "star", 292.6804, 27.9597, 3.05, "Gold and blue double in Cygnus"),
)

# Messier highlights — the crowd-pleasers that look good in a small scope.
DEEP_SKY: tuple[CatalogEntry, ...] = (
    CatalogEntry("M42", "deep_sky", 83.8221, -5.3911, 4.0, "Orion Nebula — stellar nursery"),
    CatalogEntry("M31", "deep_sky", 10.6847, 41.2687, 3.4, "Andromeda Galaxy — nearest spiral"),
    CatalogEntry("M45", "deep_sky", 56.7500, 24.1167, 1.6, "Pleiades — young open cluster"),
    CatalogEntry("M13", "deep_sky", 250.4235, 36.4613, 5.8, "Hercules Globular Cluster"),
    CatalogEntry("M44", "deep_sky", 130.1000, 19.6667, 3.7, "Beehive Cluster in Cancer"),
    CatalogEntry("M8", "deep_sky", 270.9042, -24.3867, 6.0, "Lagoon Nebula in Sagittarius"),
    CatalogEntry("M27", "deep_sky", 299.9016, 22.7212, 7.5, "Dumbbell Nebula — planetary"),
    CatalogEntry("M57", "deep_sky", 283.3962, 33.0292, 8.8, "Ring Nebula in Lyra"),
    CatalogEntry("M51", "deep_sky", 202.4696, 47.1952, 8.4, "Whirlpool Galaxy"),
    CatalogEntry("M81", "deep_sky", 148.8882, 69.0653, 6.9, "Bode's Galaxy in Ursa Major"),
    CatalogEntry("M104", "deep_sky", 189.9976, -11.6231, 8.0, "Sombrero Galaxy"),
    CatalogEntry("M7", "deep_sky", 268.4633, -34.7928, 3.3, "Ptolemy Cluster in Scorpius"),
    CatalogEntry("M22", "deep_sky", 279.0999, -23.9047, 5.1, "Sagittarius Globular Cluster"),
    CatalogEntry("NGC 869", "deep_sky", 34.7417, 57.1339, 3.7, "Double Cluster in Perseus"),
)


def _configure_astropy_offline() -> None:
    """Stop astropy from reaching for IERS tables mid-demo.

    Without this the first alt/az transform blocks on a finals2000A download.
    We are pointing a telescope at things degrees wide — sub-arcsecond Earth
    orientation corrections are noise.
    """
    from astropy.utils import iers

    iers.conf.auto_download = False
    iers.conf.iers_degraded_accuracy = "ignore"


def list_visible(
    category: str = "all",
    min_altitude_deg: float = 10.0,
    observer_lat: float | None = None,
    observer_lon: float | None = None,
    observer_elevation_km: float | None = None,
) -> tuple[list[dict], str, float, float]:
    """
    Return everything currently above `min_altitude_deg`, brightest first.

    Args:
        category: "planet" | "star" | "deep_sky" | "all"
        min_altitude_deg: Horizon cutoff. 10 deg skips the murky, tree-filled part.
        observer_*: Location override; falls back to config.

    Returns:
        (objects, observed_at_iso, lat_used, lon_used)
    """
    import astropy.units as u
    from astropy.coordinates import AltAz, EarthLocation, SkyCoord, get_body
    from astropy.time import Time

    from .config import observer

    _configure_astropy_offline()

    lat = observer_lat if observer_lat is not None else observer.latitude
    lon = observer_lon if observer_lon is not None else observer.longitude
    elev = observer_elevation_km if observer_elevation_km is not None else observer.elevation_km

    location = EarthLocation(lat=lat * u.deg, lon=lon * u.deg, height=elev * 1000 * u.m)
    now = Time.now()
    frame = AltAz(obstime=now, location=location)

    wanted = _expand_category(category)
    results: list[dict] = []

    #  Static catalog (stars + deep sky) — one vectorised transform
    static = [e for e in (*STARS, *DEEP_SKY) if e.category in wanted]
    if static:
        coords = SkyCoord(
            ra=[e.ra for e in static] * u.deg,
            dec=[e.dec for e in static] * u.deg,
            frame="icrs",
        )
        altaz = coords.transform_to(frame)
        for entry, alt, az in zip(static, altaz.alt.deg, altaz.az.deg, strict=True):
            if alt >= min_altitude_deg:
                results.append(
                    {
                        "name": entry.name,
                        "category": entry.category,
                        "ra": round(entry.ra, 4),
                        "dec": round(entry.dec, 4),
                        "altitude_deg": round(float(alt), 2),
                        "azimuth_deg": round(float(az), 2),
                        "magnitude": entry.magnitude,
                        "description": entry.description,
                    }
                )

    #  Planets — positions move, so compute them from the builtin ephemeris
    if "planet" in wanted:
        for body in PLANETS:
            try:
                coord = get_body(body, now, location)
            except Exception as exc:  # ephemeris hiccup shouldn't kill the listing
                log.warning(f"Could not compute position for {body}: {exc}")
                continue
            altaz = coord.transform_to(frame)
            alt = float(altaz.alt.deg)
            if alt < min_altitude_deg:
                continue
            icrs = coord.icrs
            results.append(
                {
                    "name": body.capitalize(),
                    "category": "planet",
                    "ra": round(float(icrs.ra.deg), 4),
                    "dec": round(float(icrs.dec.deg), 4),
                    "altitude_deg": round(alt, 2),
                    "azimuth_deg": round(float(altaz.az.deg), 2),
                    "magnitude": _PLANET_MAG.get(body),
                    "description": f"Solar system body — {body.capitalize()}",
                }
            )

    # Brightest first; unknown magnitude sinks to the bottom.
    results.sort(key=lambda o: o["magnitude"] if o["magnitude"] is not None else 99.0)

    log.info(f"Visible listing: {len(results)} objects above {min_altitude_deg} deg")
    return results, now.utc.isot + "Z", lat, lon


def _expand_category(category: str) -> set[str]:
    """Map a browse category to the internal category tags, tolerating plurals."""
    key = category.strip().lower()
    if key in ("all", ""):
        return {"planet", "star", "deep_sky"}
    # Accept "planets"/"stars" as well as the singular canonical forms.
    key = {"planets": "planet", "stars": "star", "deep_skies": "deep_sky"}.get(key, key)
    if key not in ("planet", "star", "deep_sky"):
        raise ValueError(f"Invalid category '{category}'. Use planet, star, deep_sky, or all.")
    return {key}
