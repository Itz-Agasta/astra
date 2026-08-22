from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import numpy as np

from .config import cfg

log = logging.getLogger(__name__)

_t3 = None  # Cached tetra3 instance (lazy-loaded)


def _get_tetra3():
    """Lazy-load tetra3 to avoid import at module level."""
    global _t3
    if _t3 is None:
        try:
            import tetra3
        except ImportError:
            raise RuntimeError("tetra3 not installed")
        _t3 = tetra3.Tetra3()  # Loads database once
    return _t3


@dataclass
class SolveResult:
    ra: float  # degrees
    dec: float  # degrees
    roll: float = 0.0  # camera rotation degrees
    fov: float = 0.0  # field of view degrees
    stars_matched: int = 0
    confidence: float = 1.0
    solve_time_ms: float = 0.0
    backend: str = "hint"  # "tetra3" | "hint"


def solve(
    image: np.ndarray,
    hint: tuple[float, float] | None = None,
) -> SolveResult:
    """
    Plate solve an image to get RA/Dec.

    Args:
        image: Image array (any dtype)
        hint: (ra, dec) from Stellarium in simulation mode.
              When provided, skip real plate solving.

    Returns:
        SolveResult with coordinates and metadata

    Raises:
        RuntimeError: If tetra3 fails to solve
    """
    if hint is not None:
        log.info(f"Using coord hint (simulation): RA={hint[0]:.4f} Dec={hint[1]:.4f}")
        return SolveResult(
            ra=hint[0],
            dec=hint[1],
            stars_matched=42,
            confidence=0.99,
            solve_time_ms=12.0,
            backend="hint",
        )

    return _solve_tetra3(image)


# TODO: Add ASTAP fallback if tetra3 proves unreliable in practice
def _solve_tetra3(image: np.ndarray) -> SolveResult:
    """Plate solve using tetra3 star matching."""
    try:
        import tetra3
    except ImportError:
        raise RuntimeError("tetra3 not installed: pip install tetra3")

    t0 = time.perf_counter()
    t3 = _get_tetra3()

    # Normalise image to uint8 for tetra3
    img_norm = _normalise(image)

    # Extract star centroids, then solve (both accept numpy arrays)
    centroids = tetra3.get_centroids_from_image(img_norm)
    if len(centroids) < 4:
        raise RuntimeError(f"Too few stars detected ({len(centroids)}), need at least 4")

    result = t3.solve_from_centroids(
        centroids,
        size=img_norm.shape[:2],
        fov_estimate=cfg.solver.fov_estimate_deg,
        fov_max_error=0.5,
        return_matches=True,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000

    if result is None or result.get("RA") is None:  # type: ignore[index]
        raise RuntimeError("tetra3 failed to plate solve image")

    # tetra3's return type annotations are wrong, using type-ignores :(
    solve = SolveResult(
        ra=float(result["RA"]),  # type: ignore[index]
        dec=float(result["Dec"]),  # type: ignore[index]
        roll=float(result.get("Roll", 0)),  # type: ignore[index]
        fov=float(result.get("FOV", cfg.solver.fov_estimate_deg)),  # type: ignore[index]
        stars_matched=len(result.get("Matches", [])),  # type: ignore[index]
        confidence=float(result.get("Prob", 0.95)),  # type: ignore[index]
        solve_time_ms=round(elapsed_ms, 1),
        backend="tetra3",
    )
    log.info(f"tetra3 solved: RA={solve.ra:.4f} Dec={solve.dec:.4f} in {elapsed_ms:.1f}ms")
    return solve


# Helpers
def _normalise(image: np.ndarray) -> np.ndarray:
    """Stretch to 0–255 uint8 for tetra3."""
    img = image.astype(np.float32)
    lo, hi = np.percentile(img, [1, 99])
    if hi == lo:
        return np.zeros_like(img, dtype=np.uint8)
    img = np.clip((img - lo) / (hi - lo) * 255, 0, 255)
    return img.astype(np.uint8)
