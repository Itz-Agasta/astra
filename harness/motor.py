from __future__ import annotations

import asyncio
import logging

from . import stellarium
from .config import cfg

log = logging.getLogger(__name__)


async def slew(ra_deg: float, dec_deg: float) -> None:
    """
    Point mount to (ra_deg, dec_deg).
    Blocks until settle_time_s elapses after the move.
    """
    if cfg.simulation.enabled:
        # Simulation: move Stellarium AND signal ESP32 (our demo shows both)
        await stellarium.slew(ra_deg, dec_deg)
        await _signal_esp32(ra_deg, dec_deg)
    else:
        # Hardware: INDI mount control
        await _slew_indi(ra_deg, dec_deg)

    # Wait for mechanical/electronic settle
    await asyncio.sleep(
        cfg.calibration.settle_time_s
    )  # idk its correct value.. lets wait for esp32 implementation.
    log.info(f"Slew complete → RA={ra_deg:.4f}° Dec={dec_deg:.4f}°")


async def nudge(
    delta_ra_arcsec: float, delta_dec_arcsec: float
) -> tuple[float, float, float, float]:
    """
    Move relative to where the mount is pointing right now.

    Deltas are *coordinate* offsets, not on-sky angles -- no cos(dec) factor.
    This matches how calibrator.py measures error, so a manual nudge and a
    loop correction mean the same thing by the same number.

    Returns:
        (new_ra, new_dec, previous_ra, previous_dec) in degrees.
    """
    if not cfg.simulation.enabled:
        raise NotImplementedError("Relative slew needs INDI; use simulation mode")

    prev_ra, prev_dec = await stellarium.get_view()
    new_ra = (prev_ra + delta_ra_arcsec / 3600.0) % 360.0
    # Clamp rather than wrap: rolling over the pole would flip RA by 180 deg.
    new_dec = max(-90.0, min(90.0, prev_dec + delta_dec_arcsec / 3600.0))

    await slew(new_ra, new_dec)
    log.info(
        f'Nudge {delta_ra_arcsec:+.1f}" RA, {delta_dec_arcsec:+.1f}" Dec -> '
        f"RA={new_ra:.4f} Dec={new_dec:.4f}"
    )
    return new_ra, new_dec, prev_ra, prev_dec


async def halt() -> None:
    """Stop mount immediately -- called on abort."""
    if cfg.simulation.enabled:
        return  # nothing to stop in simulation
    # TODO: Implement INDI abort when real telescope is connected
    log.warning("halt() not implemented for hardware mode")


# Hardware -- INDI
async def _slew_indi(ra_deg: float, dec_deg: float) -> None:
    """
    Move real mount via INDI protocol.
    """
    # TODO: Implement INDI mount
    # This will use pyindi-client to send commands to the mount
    log.critical("We are broke.... couldn't afford a real telescope for testing.")
    raise NotImplementedError("Not implemented yet. Use simulation mode (CCE_SIMULATION=true)")


# FIXME: ESP32 Stepper Motors
async def _signal_esp32(ra_deg: float, dec_deg: float) -> None:
    """
    Signal ESP32 stepper motor driver to move to (ra_deg, dec_deg).

    yooo...Suchetan implement this:
    Payload format:
      {
        "ra_deg": float,   # Target RA in degrees
        "dec_deg": float,  # Target Dec in degrees
      }

    Response format:
      {
        "status": "ok" | "error",
        "message": "optional description"
      }
    """
    # esp_url = f"{cfg.esp32.url}/slew"
    # payload = {
    #     "ra_deg": ra_deg,
    #     "dec_deg": dec_deg,
    # }
    log.warning("Suchetan will implement this")
