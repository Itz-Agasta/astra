
from __future__ import annotations

import logging

import numpy as np

from . import stellarium
from .config import cfg

log = logging.getLogger(__name__)


async def capture_frame(frame_index: int = 0) -> tuple[np.ndarray, tuple[float, float] | None]:
    """
    Returns:
        image       -- numpy grayscale uint16 array (for solver)
        hint        -- (ra, dec) hint if simulation mode, else None
                      solver skips tetra3 and uses hint directly when provided
    """
    if cfg.simulation.enabled:
        return await _capture_stellarium()
    return _capture_indi(frame_index), None


# Simulation mode -- Stellarium HTTP API
async def _capture_stellarium() -> tuple[np.ndarray, tuple[float, float]]:
    """
    Stellarium Remote Control API (enable under Plugins -> Remote Control).

    In simulation mode we use the Stellarium view coords directly as the
    plate solve result -- skipping tetra3. The calibration loop maths are
    identical; only the image source changes.
    """
    try:
        ra_deg, dec_deg = await stellarium.get_view()
        log.info(f"Stellarium view: RA={ra_deg:.4f} Dec={dec_deg:.4f}")

        image = _synthetic_starfield(ra_deg, dec_deg)
        return image, (ra_deg, dec_deg)

    except Exception as exc:
        raise RuntimeError(f"Cannot reach Stellarium at {cfg.simulation.url}: {exc}") from exc


def _synthetic_starfield(ra: float, dec: float) -> np.ndarray:
    """
    Generates a fake star field image so solver.py has something to show.
    Stars are placed deterministically based on RA/Dec seed -- same pointing
    always produces same star pattern.
    """
    rng = np.random.default_rng(seed=int((ra * 1000 + dec * 100) % 2**32))
    img = np.zeros((1024, 1024), dtype=np.uint16)

    # Background noise
    img += rng.integers(100, 300, img.shape, dtype=np.uint16)

    # Place ~40 stars
    n_stars = rng.integers(35, 55)
    for _ in range(n_stars):
        x = int(rng.integers(20, 1004))
        y = int(rng.integers(20, 1004))
        brightness = int(rng.integers(3000, 60000))
        sigma = rng.uniform(1.5, 3.5)
        for dx in range(-8, 9):
            for dy in range(-8, 9):
                if 0 <= x + dx < 1024 and 0 <= y + dy < 1024:
                    val = int(brightness * np.exp(-(dx**2 + dy**2) / (2 * sigma**2)))
                    img[y + dy, x + dx] = min(65535, img[y + dy, x + dx] + val)
    return img


# Real hardware -- INDI camera
# TODO: This will be built when moving to real hardware.
def _capture_indi(frame_index: int) -> np.ndarray:
    """
    NOT IMPLEMENTED -- real INDI camera capture.
    Requires: pyindi-client, astropy, and a running INDI server with a CCD driver.
    """
    log.critical("We are broke.... couldn't afford a real telescope for testing.")
    raise NotImplementedError("Not implemented yet. Use simulation mode (CCE_SIMULATION=true)")