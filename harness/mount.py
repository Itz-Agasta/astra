"""
mount.py -- whatever is actually pointing the telescope.

Two backends behind one interface:

  stellarium  the simulated sky is also the mount; a slew moves the view
  indi        a real INDI mount driver over TCP, which is what observatory
              hardware speaks. `indi_simulator_telescope` and a mount on a
              mountain are the same code path here -- the difference is which
              driver `indiserver` was started with.

`reported_position()` and `true_position()` are deliberately separate.
A mount tells you where it believes it is pointing, and on anything with a
pointing error that is a different place from where it really is -- which is
the entire reason the calibration loop exists. Only the simulated camera may
read the true position, to decide what light would be falling on the sensor.
Nothing in the control loop is allowed to: on real hardware there is no such
oracle, and the plate solver is the only measurement there is.
"""

from __future__ import annotations

import asyncio
import logging
import math

from . import indi, stellarium
from .config import cfg

log = logging.getLogger(__name__)

_client: indi.IndiClient | None = None

# A slew across the sky on a real mount is minutes, not seconds.
_SLEW_TIMEOUT_S = 180.0

# How close the view has to land before we call a Stellarium slew finished,
# and how long to keep asking.
_ARRIVAL_TOLERANCE_ARCSEC = 2.0
_ARRIVAL_TIMEOUT_S = 5.0


def backend() -> str:
    return cfg.mount.backend


def is_indi() -> bool:
    return cfg.mount.backend == "indi"


#  Connection


async def connect() -> bool:
    """Prepare the mount for commands. Returns True if it is usable."""
    if not is_indi():
        return True
    try:
        await _indi_client()
        return True
    except Exception as exc:
        log.error(f"INDI mount unavailable at {cfg.mount.indi_url}: {exc}")
        return False


async def disconnect() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None


async def _indi_client() -> indi.IndiClient:
    """The connected client, dialling in on first use."""
    global _client
    if _client is not None and _client.connected:
        return _client

    client = indi.IndiClient(cfg.mount.indi_host, cfg.mount.indi_port, cfg.mount.indi_device)
    await client.connect()
    await client.wait_defined(indi.CONNECTION)

    # A driver only defines its coordinate properties once the device is
    # connected, so this has to happen before anything can be read or moved.
    if client.switch(indi.CONNECTION, "CONNECT") is not True:
        await client.set_switch(indi.CONNECTION, {"CONNECT": True, "DISCONNECT": False})
        await client.wait_state(indi.CONNECTION, "Ok", timeout=30.0)
    await client.wait_defined(indi.EQUATORIAL_EOD_COORD, timeout=30.0)

    _client = client
    log.info(
        f"INDI mount '{cfg.mount.indi_device}' ready "
        f"({len(client.property_names())} properties, "
        f"ground truth={'yes' if client.has(indi.EQUATORIAL_PE) else 'no'})"
    )
    return client


#  Pointing


async def goto(ra_deg: float, dec_deg: float) -> None:
    """Point the mount at a J2000 coordinate and wait until it gets there."""
    if not is_indi():
        await stellarium.slew(ra_deg, dec_deg)
        await _await_stellarium_arrival(ra_deg, dec_deg)
        return

    client = await _indi_client()
    ra_hours, dec_jnow = indi.j2000_to_jnow(ra_deg, dec_deg)

    # ON_COORD_SET decides what the next coordinate write *means*. TRACK holds
    # the target as the sky turns, which is what keeps RA/Dec fixed between
    # our capture and our solve.
    await client.set_switch(indi.ON_COORD_SET, {"SLEW": False, "TRACK": True, "SYNC": False})
    await client.set_number(indi.EQUATORIAL_EOD_COORD, {"RA": ra_hours, "DEC": dec_jnow})
    await client.wait_state(indi.EQUATORIAL_EOD_COORD, "Ok", timeout=_SLEW_TIMEOUT_S)

    log.info(f"INDI slew -> RA={ra_deg:.4f}° Dec={dec_deg:.4f}° (JNow {ra_hours:.5f}h)")
    await _mirror_to_stellarium()


async def reported_position() -> tuple[float, float]:
    """Where the mount believes it is pointing, as J2000 degrees."""
    if not is_indi():
        return await stellarium.get_view()

    client = await _indi_client()
    ra_hours = client.number(indi.EQUATORIAL_EOD_COORD, "RA")
    dec_jnow = client.number(indi.EQUATORIAL_EOD_COORD, "DEC")
    if ra_hours is None or dec_jnow is None:
        raise RuntimeError("INDI mount has not reported a position yet")
    return indi.jnow_to_j2000(ra_hours, dec_jnow)


async def true_position() -> tuple[float, float]:
    """Where the mount is *really* pointing -- simulated optics only.

    Falls back to the reported position when no oracle exists, which is
    every real mount. The caller then measures a perfect mount, so any error
    the loop sees comes from the solver rather than the hardware.
    """
    if not is_indi():
        return await stellarium.get_view()

    client = await _indi_client()
    ra_hours = client.number(indi.EQUATORIAL_PE, "RA_PE")
    dec_jnow = client.number(indi.EQUATORIAL_PE, "DEC_PE")
    if ra_hours is None or dec_jnow is None:
        return await reported_position()
    return indi.jnow_to_j2000(ra_hours, dec_jnow)


async def abort() -> None:
    """Stop the mount now."""
    if not is_indi():
        return  # a Stellarium view move is instantaneous; nothing to stop
    try:
        client = await _indi_client()
        await client.set_switch(indi.ABORT_MOTION, {"ABORT": True})
        log.info("INDI abort sent")
    except Exception as exc:
        log.error(f"INDI abort failed: {exc}")


async def _await_stellarium_arrival(ra_deg: float, dec_deg: float) -> None:
    """Wait until Stellarium reports the view really is where we sent it.

    An INDI mount tells us it arrived by moving its coordinate vector from
    Busy to Ok. Stellarium has no such handshake -- the POST returns
    immediately -- so a read taken too soon can hand back the *previous*
    position. That is silent and it is nasty: the simulated camera renders the
    sky at whatever position it was told, so the plate solve agrees with the
    stale reading, the residual looks healthy, and the loop happily converges
    several hundred arcseconds from the target with every number it can see
    looking correct.

    So ask until the answer matches what we commanded.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _ARRIVAL_TIMEOUT_S
    worst = float("inf")
    while True:
        try:
            ra, dec = await stellarium.get_view()
        except Exception as exc:
            log.warning(f"Could not confirm Stellarium arrival: {exc}")
            return
        d_ra = (ra - ra_deg + 180) % 360 - 180
        worst = math.hypot(d_ra * math.cos(math.radians(dec_deg)), dec - dec_deg) * 3600
        if worst <= _ARRIVAL_TOLERANCE_ARCSEC:
            return
        if loop.time() >= deadline:
            log.warning(
                f'Stellarium still {worst:.1f}" from the commanded position after '
                f"{_ARRIVAL_TIMEOUT_S:.0f}s -- continuing, but this frame may be stale"
            )
            return
        await asyncio.sleep(0.05)


#  Display mirroring


async def _mirror_to_stellarium() -> None:
    """Show an INDI mount's position on the Stellarium sky.

    Purely a display: Stellarium is no longer the mount, it is the monitor.
    We mirror the *reported* position because that is what a real observatory
    display would draw -- the mount's own idea of where it is looking.

    Never fatal. A dead display must not be able to stop a working mount.
    """
    if not cfg.mount.mirror_to_stellarium:
        return
    try:
        ra, dec = await reported_position()
        await stellarium.slew(ra, dec)
    except Exception as exc:
        log.debug(f"Could not mirror mount position to Stellarium: {exc}")
