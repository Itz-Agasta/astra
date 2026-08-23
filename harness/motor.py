from __future__ import annotations

import asyncio
import logging

from . import esp32, mount
from .config import cfg

log = logging.getLogger(__name__)


async def slew(ra_deg: float, dec_deg: float) -> None:
    """
    Point mount to (ra_deg, dec_deg).
    Blocks until the mount reports arrival, plus a settle margin.
    """
    await mount.goto(ra_deg, dec_deg)
    await _signal_esp32(ra_deg, dec_deg)

    # An INDI mount has already told us it arrived -- the vector went Busy ->
    # Ok -- so this is only the mechanical ring-down after the servos stop.
    # The Stellarium backend moves instantly and settle_time_s is the whole
    # of its wait.
    await asyncio.sleep(cfg.calibration.settle_time_s)
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
    # Relative to what the mount *claims*, not to any ground truth: this is a
    # human saying "a bit left of wherever you are", and the mount's own frame
    # is the only one available on real hardware.
    prev_ra, prev_dec = await mount.reported_position()
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
    """Stop mount immediately -- called on abort.

    The mount is the pointing authority, so it stops first; the steppers are
    told afterwards and are not allowed to keep an abort from completing.
    """
    await mount.abort()
    try:
        await esp32.signal_halt()
    except Exception as exc:
        log.warning(f"ESP32 halt not delivered: {exc}")


#  ESP32 stepper mirror


async def _signal_esp32(ra_deg: float, dec_deg: float) -> None:
    """
    Tell the ESP32 stepper driver where the mount went.

    Delegates to esp32.signal_slew(), which converts RA/Dec -> Alt/Az ->
    servo angles and sends the command over USB serial.

    The board mirrors the mount rather than being the mount: the pointing
    authority is Stellarium or INDI, and the steppers follow so there is
    something physical to watch. That is why every failure here is logged and
    swallowed -- an unplugged board raises from _send_command on every write,
    and must not be able to fail a calibration.
    """
    try:
        await esp32.signal_slew(ra_deg, dec_deg)
    except Exception as exc:
        log.warning(f"ESP32 slew not delivered: {exc}")
