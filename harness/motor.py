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
