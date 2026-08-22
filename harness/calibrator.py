"""
calibrator.py — The closed-loop calibration algorithm.

Core equations:
  err     = √(ΔRA² + ΔDec²) × 3600          (arcseconds)
  errₙ    = err₀ × (1 − k)ⁿ                 (geometric convergence)
  ra'     = current_ra  + k × ΔRA            (damped correction)
  dec'    = current_dec + k × ΔDec

k = 0.8 (damping factor) — prevents oscillation from worm gear backlash.
"""

from __future__ import annotations

import asyncio
import logging
import math
import uuid

from .capture import capture_frame
from .config import cfg
from .motor import halt, slew
from .solver import solve
from .state import CalibrationJob, store

log = logging.getLogger(__name__)


#  Public entry points


async def start_calibration(
    target: str,
    target_ra: float,
    target_dec: float,
    source: str,
) -> str:
    """
    Create a job and launch the calibration loop as a background asyncio Task.
    Returns job_id immediately — caller polls get_calibration_status().
    """
    job_id = f"cal_{uuid.uuid4().hex[:6]}"
    job = CalibrationJob(
        job_id=job_id,
        target=target,
        target_ra=target_ra,
        target_dec=target_dec,
        source=source,
        threshold_arcsec=cfg.calibration.converge_threshold_arcsec,
        message=f"Resolved {target} via {source} · starting loop",
    )
    store.add_job(job)
    store.status.active_job = job_id

    task = asyncio.create_task(_calibration_loop(job_id))
    store.register_task(job_id, task)
    await store.broadcast_job(job, event="calibration_started")
    log.info(f"Calibration job {job_id} started for '{target}'")
    return job_id


async def abort_job(job_id: str) -> bool:
    """Abort a running job. Returns True if job existed."""
    job = store.get_job(job_id)
    if not job or job.status != "running":
        return False
    store.update_job(job_id, status="aborted", message="Aborted by user")
    store.cancel_task(job_id)  # stop mid-slew, not just at the next status check
    await halt()
    await store.broadcast_job(job)
    return True


#  Core loop


async def _calibration_loop(job_id: str) -> None:
    job = store.get_job(job_id)
    if not job:
        return

    k = cfg.calibration.damping  # 0.8
    thr = cfg.calibration.converge_threshold_arcsec  # 30.0
    max_iter = cfg.calibration.max_iterations  # 30
    max_fails = 3  # plate solve retries

    fail_count = 0

    for iteration in range(1, max_iter + 1):
        #  Abort check
        job = store.get_job(job_id)
        if job is None or job.status in ("aborted", "failed"):
            return

        store.update_job(
            job_id, iteration=iteration, message=f"Iteration {iteration} — capturing frame"
        )

        #  1. Capture
        try:
            image, hint = await capture_frame(frame_index=iteration)
            store.status.camera_connected = True
        except Exception as exc:
            fail_count += 1
            log.warning(f"Capture failed ({fail_count}/{max_fails}): {exc}")
            if fail_count >= max_fails:
                await _fail(job_id, "capture_failed", f"Camera failed {max_fails} times: {exc}")
                return
            store.update_job(
                job_id,
                message=f"Iteration {iteration} — capture failed "
                f"({fail_count}/{max_fails}), retrying: {exc}",
            )
            await store.broadcast_job(job)
            await asyncio.sleep(1.0)
            continue

        #  2. Plate solve
        try:
            result = solve(image, hint=hint)
            store.status.solver_ready = True
            current_ra = result.ra
            current_dec = result.dec
            store.update_job(
                job_id,
                solver_backend=result.backend,
                solver_residual_arcsec=result.residual_arcsec,
                stars_matched=result.stars_matched,
                solve_time_ms=result.solve_time_ms,
            )
            fail_count = 0  # reset on success
        except Exception as exc:
            fail_count += 1
            log.warning(f"Solve failed ({fail_count}/{max_fails}): {exc}")
            if fail_count >= max_fails:
                await _fail(
                    job_id, "plate_solve_failed", f"Plate solver failed {max_fails} times: {exc}"
                )
                return
            store.update_job(
                job_id,
                message=f"Iteration {iteration} — solve failed "
                f"({fail_count}/{max_fails}), retrying: {exc}",
            )
            await store.broadcast_job(job)
            await asyncio.sleep(1.0)
            continue

        #  3. Compute offset
        job = store.get_job(job_id)
        if job is None:
            return
        delta_ra = job.target_ra - current_ra
        delta_dec = job.target_dec - current_dec

        # Wrap RA delta to [-180, 180]
        if delta_ra > 180:
            delta_ra -= 360
        if delta_ra < -180:
            delta_ra += 360

        # err = √(ΔRA² + ΔDec²) × 3600  [arcsec]
        error_ra_arcsec = abs(delta_ra) * 3600
        error_dec_arcsec = abs(delta_dec) * 3600
        total_error = math.sqrt(delta_ra**2 + delta_dec**2) * 3600

        store.update_job(
            job_id,
            current_ra=round(current_ra, 6),
            current_dec=round(current_dec, 6),
            error_ra_arcsec=round(error_ra_arcsec, 2),
            error_dec_arcsec=round(error_dec_arcsec, 2),
            total_error_arcsec=round(total_error, 2),
            message=f'Iteration {iteration} — error {total_error:.1f}" — converging',
        )

        # One row per iteration — this is what the dashboard graphs.
        job.history.append(
            {
                "iteration": iteration,
                "error_arcsec": round(total_error, 2),
                "ra": round(current_ra, 6),
                "dec": round(current_dec, 6),
                "solver_residual_arcsec": result.residual_arcsec,
                "elapsed_seconds": job.elapsed_seconds,
            }
        )

        await store.broadcast_job(job)
        log.info(
            f'[{job_id}] iter={iteration} err={total_error:.1f}" '
            f'ΔRA={delta_ra * 3600:.1f}" ΔDec={delta_dec * 3600:.1f}"'
        )

        # 4. Convergence check
        if total_error < thr:
            store.update_job(
                job_id,
                status="done",
                message=f'Locked on {job.target}. Final error {total_error:.1f}"',
            )
            await store.broadcast_job(job)
            log.info(f'[{job_id}] LOCKED — {total_error:.1f}" after {iteration} iterations')
            return

        # 5. Apply damped correction  ra' = ra + k·ΔRA
        corrected_ra = current_ra + k * delta_ra
        corrected_dec = current_dec + k * delta_dec

        store.update_job(job_id, message=f"Iteration {iteration} — slewing (0.8 × Δ)")

        try:
            await slew(corrected_ra, corrected_dec)
            store.status.mount_connected = True
        except Exception as exc:
            await _fail(job_id, "mount_timeout", str(exc))
            return

    # Safety cap exceeded
    await _fail(
        job_id,
        "max_iterations_exceeded",
        f"Did not converge in {max_iter} iterations. "
        "Check for physical obstruction or large initial offset.",
    )


async def _fail(job_id: str, reason: str, message: str) -> None:
    store.update_job(job_id, status="failed", reason=reason, message=message)
    await halt()
    job = store.get_job(job_id)
    if job:
        await store.broadcast_job(job)
    log.error(f"[{job_id}] FAILED — {reason}: {message}")
