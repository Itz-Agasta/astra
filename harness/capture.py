"""
capture.py — Produce a frame for the plate solver.

Simulation mode renders a *real* starfield: we read where Stellarium is
looking, then project the actual Hipparcos stars around that direction onto
a synthetic frame using a gnomonic (TAN) projection. tetra3 then solves that
frame for real -- it is genuinely matching star patterns, not being handed
the answer.

The catalog comes from tetra3's own loaded database (`star_table`), so the
stars we draw are exactly the stars the solver knows about. No network, no
separate catalog file, no index build.
"""

from __future__ import annotations

import logging
import math

import numpy as np

from . import stellarium
from .config import cfg

log = logging.getLogger(__name__)

# Gaussian PSF width in px. ~1.6 gives tetra3's centroider a clean, well
# sampled star without bleeding into its neighbours.
_PSF_SIGMA = 1.6
_PSF_RADIUS = 6  # px half-window to stamp; beyond ~4 sigma contributes nothing
_BACKGROUND_MEAN = 120.0
_BACKGROUND_NOISE = 8.0

# Stand-in for the frame when the solver is not going to read it.
_PLACEHOLDER_FRAME = np.zeros((1, 1), dtype=np.uint16)


async def capture_frame(frame_index: int = 0) -> tuple[np.ndarray, tuple[float, float] | None]:
    """
    Returns:
        image -- grayscale uint16 frame for the solver
        hint  -- (ra, dec) truth from Stellarium, or None on real hardware.
                 solver.py only uses this when backend == "hint"; with the
                 tetra3 backend it is carried purely so the harness can
                 report solver residual against ground truth.
    """
    if cfg.simulation.enabled:
        return await _capture_stellarium()
    return _capture_indi(frame_index), None


#  Simulation -- Stellarium view + rendered starfield


async def _capture_stellarium() -> tuple[np.ndarray, tuple[float, float]]:
    """Read the Stellarium view direction and render the sky there."""
    try:
        ra_deg, dec_deg = await stellarium.get_view()
    except Exception as exc:
        raise RuntimeError(f"Cannot reach Stellarium at {cfg.simulation.url}: {exc}") from exc

    log.info(f"Stellarium view: RA={ra_deg:.4f} Dec={dec_deg:.4f}")

    # The hint backend returns the Stellarium coords verbatim and never looks
    # at the frame, so rendering one would burn ~75 MB and load tetra3's 162 MB
    # database for nothing. Skipping it is what makes light mode light.
    if cfg.solver.backend == "hint":
        return _PLACEHOLDER_FRAME, (ra_deg, dec_deg)

    return render_starfield(ra_deg, dec_deg), (ra_deg, dec_deg)


def render_starfield(
    ra_deg: float,
    dec_deg: float,
    fov_deg: float | None = None,
    size_px: int | None = None,
) -> np.ndarray:
    """
    Gnomonic projection of the real sky around (ra_deg, dec_deg).

    Star positions and magnitudes come from tetra3's loaded database, so what
    we draw is by construction solvable by tetra3 -- the loop measures the
    solver's true accuracy rather than a made-up number.
    """
    from .solver import get_star_catalog

    fov = fov_deg if fov_deg is not None else cfg.solver.fov_estimate_deg
    size = size_px if size_px is not None else cfg.solver.image_size_px

    vectors, magnitudes = get_star_catalog()

    ra0, dec0 = math.radians(ra_deg), math.radians(dec_deg)
    # Camera basis: boresight plus the local east/north tangent directions.
    boresight = np.array(
        [math.cos(dec0) * math.cos(ra0), math.cos(dec0) * math.sin(ra0), math.sin(dec0)]
    )
    east = np.array([-math.sin(ra0), math.cos(ra0), 0.0])
    north = np.array(
        [-math.sin(dec0) * math.cos(ra0), -math.sin(dec0) * math.sin(ra0), math.cos(dec0)]
    )

    # Keep only stars inside the FOV cone -- cheap dot-product cull.
    cos_along = vectors @ boresight
    inside = cos_along > math.cos(math.radians(fov))
    if not inside.any():
        log.warning(f"No catalog stars within {fov} deg of RA={ra_deg:.2f} Dec={dec_deg:.2f}")
        return _blank_frame(size)

    vis = vectors[inside]
    depth = cos_along[inside]
    mags = magnitudes[inside]

    # Gnomonic (TAN): divide the tangent-plane components by the depth.
    x_tan = (vis @ east) / depth
    y_tan = (vis @ north) / depth

    focal_px = (size / 2) / math.tan(math.radians(fov) / 2)
    px = size / 2 + x_tan * focal_px
    py = size / 2 - y_tan * focal_px  # image rows run downward, north runs up

    image = _blank_frame(size)
    drawn = 0
    for x, y, mag in zip(px, py, mags, strict=True):
        if not (_PSF_RADIUS <= x < size - _PSF_RADIUS and _PSF_RADIUS <= y < size - _PSF_RADIUS):
            continue
        _stamp_star(image, float(x), float(y), float(mag))
        drawn += 1

    log.debug(f"Rendered {drawn} stars at {size}px / {fov} deg FOV")
    np.clip(image, 0, 65535, out=image)  # in place -- a copy here costs 33 MB at 2048px
    return image.astype(np.uint16)


def _blank_frame(size: int) -> np.ndarray:
    """Background with read noise, so the centroider has a realistic floor.

    Returns float64 so stars can be accumulated in place by the caller.
    """
    rng = np.random.default_rng(seed=42)  # fixed: frame-to-frame noise adds nothing
    noise = rng.normal(_BACKGROUND_MEAN, _BACKGROUND_NOISE, (size, size))
    np.clip(noise, 0, 65535, out=noise)
    return noise


def _stamp_star(image: np.ndarray, x: float, y: float, mag: float) -> None:
    """Add one sub-pixel-positioned Gaussian PSF, scaled by magnitude."""
    # Pogson: each magnitude step is 10^0.4 in flux. Offset by +1.5 so Sirius
    # (mag -1.46) lands near saturation instead of blowing past it.
    peak = 40000.0 * (10 ** (-0.4 * (mag + 1.5)))
    height, width = image.shape
    xi, yi = int(round(x)), int(round(y))

    # Clip the stamp window to the frame. Rounding can push it one pixel past
    # the caller's bounds check, so clip here rather than trusting the caller.
    y0, y1 = max(0, yi - _PSF_RADIUS), min(height, yi + _PSF_RADIUS + 1)
    x0, x1 = max(0, xi - _PSF_RADIUS), min(width, xi + _PSF_RADIUS + 1)
    if y0 >= y1 or x0 >= x1:
        return

    # Offsets measured from the true sub-pixel centre -- this is what gives
    # the centroider better-than-one-pixel accuracy.
    dy = np.arange(y0, y1) - y
    dx = np.arange(x0, x1) - x
    gauss = np.exp(-(dy[:, None] ** 2 + dx[None, :] ** 2) / (2 * _PSF_SIGMA**2))
    image[y0:y1, x0:x1] += peak * gauss


#  Real hardware -- INDI camera


def _capture_indi(frame_index: int) -> np.ndarray:
    """
    NOT IMPLEMENTED -- real INDI camera capture.
    Requires: pyindi-client, astropy, and a running INDI server with a CCD driver.
    """
    log.critical("We are broke.... couldn't afford a real telescope for testing.")
    raise NotImplementedError("Not implemented yet. Use simulation mode (CCE_SIMULATION=true)")
