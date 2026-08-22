"""
solver.py — Plate solving via tetra3.

tetra3 ships a bundled `default_database.npz` (Hipparcos, mag <= 7, built for
10-30 degree fields), so there is no index to generate -- it solves out of the
box. Measured on this database: 8/8 solves across the sky including the pole,
with error ~= 0.7 x the image pixel scale.

That accuracy is the loop's noise floor, so it constrains the convergence
threshold. At the defaults (2048 px, 15 deg FOV -> 26"/px) expect ~18" error,
which sits comfortably under a 30" threshold. Coarser frames will not converge.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass

import numpy as np

from .config import cfg

log = logging.getLogger(__name__)

_t3 = None  # Cached Tetra3 instance -- loading the database costs ~1s
_catalog: tuple[np.ndarray, np.ndarray] | None = None


def _patch_numpy_compat() -> None:
    """tetra3 master still calls `np.math.factorial`, removed in NumPy 2.0.

    Exactly one call site (tetra3.py, in solve_from_centroids), so aliasing the
    stdlib module onto numpy is enough to make it work. Without this every
    solve raises AttributeError.
    """
    if not hasattr(np, "math"):
        # setattr, not `np.math = ...`, so type checkers don't flag an
        # attribute numpy no longer declares.
        setattr(np, "math", math)  # noqa: B010


def _get_tetra3():
    """Lazy-load tetra3 and its bundled database."""
    global _t3
    if _t3 is None:
        try:
            import tetra3
        except ImportError:
            raise RuntimeError("tetra3 not installed") from None
        _patch_numpy_compat()
        _t3 = tetra3.Tetra3()  # loads default_database.npz
        log.info(f"tetra3 database loaded: {dict(_t3.database_properties)}")
    return _t3


def get_star_catalog() -> tuple[np.ndarray, np.ndarray]:
    """
    The solver's own star catalog, for capture.py to render from.

    `star_table` columns are [ra_rad, dec_rad, x, y, z, magnitude].

    Returns:
        (unit_vectors Nx3 float64, magnitudes N float64)
    """
    global _catalog
    if _catalog is None:
        table = _get_tetra3().star_table
        _catalog = (
            table[:, 2:5].astype(np.float64),
            table[:, 5].astype(np.float64),
        )
        log.info(f"Star catalog ready: {len(_catalog[1])} stars")
    return _catalog


def warmup() -> bool:
    """Preload the database at startup so the first solve isn't slow. Never raises."""
    if cfg.solver.backend == "hint":
        # Light mode: the hint backend never plate solves and capture.py never
        # renders, so loading tetra3's 162 MB database would be pure waste.
        log.info("Solver backend is 'hint' — skipping tetra3 database load")
        return True
    try:
        get_star_catalog()
        return True
    except Exception as exc:
        log.error(f"Solver warmup failed: {exc}")
        return False


@dataclass
class SolveResult:
    ra: float  # degrees
    dec: float  # degrees
    roll: float = 0.0  # camera rotation degrees
    fov: float = 0.0  # field of view degrees
    stars_matched: int = 0
    confidence: float = 1.0
    solve_time_ms: float = 0.0
    rmse_arcsec: float = 0.0  # tetra3's own fit residual across matched stars
    backend: str = "tetra3"  # "tetra3" | "hint"
    residual_arcsec: float | None = None  # vs. hint, when one is available


def solve(
    image: np.ndarray,
    hint: tuple[float, float] | None = None,
) -> SolveResult:
    """
    Plate solve a frame to get RA/Dec.

    Args:
        image: Grayscale frame.
        hint: Ground-truth (ra, dec) when known. With the default "tetra3"
              backend this is *not* used to solve -- it only measures the
              solver's residual. With backend="hint" it is returned directly.

    Raises:
        RuntimeError: If the solve fails.
    """
    if cfg.solver.backend == "hint":
        if hint is None:
            raise RuntimeError("Solver backend is 'hint' but no hint was supplied")
        log.info(f"Hint bypass (no plate solve): RA={hint[0]:.4f} Dec={hint[1]:.4f}")
        # No star matching happened, so report zeroes rather than inventing
        # plausible-looking numbers -- the dashboard graphs these, and fake
        # telemetry is worse than absent telemetry. `backend` says why.
        return SolveResult(
            ra=hint[0],
            dec=hint[1],
            fov=cfg.solver.fov_estimate_deg,
            stars_matched=0,
            confidence=1.0,
            solve_time_ms=0.0,
            backend="hint",
            residual_arcsec=0.0,
        )

    result = _solve_tetra3(image)
    if hint is not None:
        result.residual_arcsec = round(_angular_sep_arcsec(result.ra, result.dec, *hint), 2)
        log.info(f'Solver residual vs truth: {result.residual_arcsec}"')
    return result


# TODO: Add ASTAP fallback if tetra3 proves unreliable in practice
def _solve_tetra3(image: np.ndarray) -> SolveResult:
    """Plate solve using tetra3 star matching."""
    try:
        import tetra3
    except ImportError:
        raise RuntimeError("tetra3 not installed: pip install tetra3") from None

    t0 = time.perf_counter()
    t3 = _get_tetra3()

    img_norm = _normalise(image)

    centroids = tetra3.get_centroids_from_image(img_norm)
    if len(centroids) < 4:
        raise RuntimeError(f"Too few stars detected ({len(centroids)}), need at least 4")

    result = t3.solve_from_centroids(
        centroids,
        size=img_norm.shape[:2],
        fov_estimate=cfg.solver.fov_estimate_deg,
        fov_max_error=cfg.solver.fov_max_error_deg,
        return_matches=True,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000

    if result is None or result.get("RA") is None:  # type: ignore[index]
        raise RuntimeError(
            f"tetra3 found no match among {len(centroids)} centroids "
            f"(fov_estimate={cfg.solver.fov_estimate_deg} deg)"
        )

    # tetra3's return type annotations are wrong, using type-ignores :(
    solved = SolveResult(
        ra=float(result["RA"]),  # type: ignore[index]
        dec=float(result["Dec"]),  # type: ignore[index]
        roll=float(result.get("Roll") or 0.0),  # type: ignore[index]
        fov=float(result.get("FOV") or cfg.solver.fov_estimate_deg),  # type: ignore[index]
        stars_matched=int(result.get("Matches") or 0),  # count, not a list  # type: ignore[index]
        confidence=1.0 - float(result.get("Prob") or 0.0),  # type: ignore[index]
        solve_time_ms=round(elapsed_ms, 1),
        rmse_arcsec=round(float(result.get("RMSE") or 0.0), 2),  # type: ignore[index]
        backend="tetra3",
    )
    log.info(
        f"tetra3 solved: RA={solved.ra:.4f} Dec={solved.dec:.4f} "
        f"({solved.stars_matched} stars, {elapsed_ms:.0f}ms)"
    )
    return solved


#  Helpers


def _angular_sep_arcsec(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
    """Great-circle separation in arcseconds (haversine -- stable at small angles)."""
    p1, p2 = math.radians(dec1), math.radians(dec2)
    dp = p2 - p1
    dl = math.radians(ra2 - ra1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return math.degrees(2 * math.asin(min(1.0, math.sqrt(a)))) * 3600


def _normalise(image: np.ndarray) -> np.ndarray:
    """Stretch to 0-255 uint8 for tetra3."""
    img = image.astype(np.float32)
    lo, hi = np.percentile(img, [1, 99])
    if hi == lo:
        return np.zeros_like(img, dtype=np.uint8)
    img = np.clip((img - lo) / (hi - lo) * 255, 0, 255)
    return img.astype(np.uint8)
