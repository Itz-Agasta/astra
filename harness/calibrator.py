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

from . import stellarium
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


#  Holding the sky still


async def _freeze_sky() -> float | None:
    """Pause Stellarium's clock for the duration of a run.

    We resolve the target once, then spend several iterations driving the
    mount to those fixed coordinates. With the clock running, the object walks
    away from them underneath us -- the Moon moves ~0.9"/s (about 19" over a
    20s run, mostly topocentric parallax as the Earth turns) against a 30"
    convergence threshold, so a live sky alone can hold the loop above its
    target. Planets manage ~0.03"/s and stars none at all, but freezing costs
    nothing and makes the target genuinely stationary for every category.

    Returns the previous rate so it can be handed back, or None if we could
    not read it. Never fatal: a moving sky degrades accuracy, it does not
    break the loop.
    """
    if not cfg.simulation.enabled:
        return None
    try:
        previous = await stellarium.get_time_rate()
        await stellarium.set_time_rate(0.0)
    except Exception as exc:
        log.warning(f"Could not pause Stellarium's clock, sky keeps moving: {exc}")
        return None
    log.info(f"Stellarium clock paused (was {previous} JD/s)")
    return previous


async def _thaw_sky(previous: float | None) -> None:
    """Hand the clock back exactly as we found it."""
    if previous is None:
        return
    try:
        await stellarium.set_time_rate(previous)
    except Exception as exc:
        log.warning(f"Could not restore Stellarium time rate to {previous}: {exc}")


#  Core loop


async def _calibration_loop(job_id: str) -> None:
    """Freeze the sky, run the loop, and always give the clock back."""
    previous_rate = await _freeze_sky()
    try:
        await _run_calibration(job_id)
    finally:
        # Safe to await even when we land here because the task was cancelled:
        # Task.cancel() delivers CancelledError once, so this await is not
        # itself cancelled and the user never gets left with a frozen sim.
        await _thaw_sky(previous_rate)


async def _run_calibration(job_id: str) -> None:
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
            # The estimate is under threshold, but the view is still parked
            # where the previous 0.8x step left it, so what is actually on the
            # sky is this error *plus* the solver's own residual -- exactly the
            # offset you see when you zoom in on the target. Spend one more
            # slew closing the whole remaining gap, and what is left is the
            # solver residual by itself.
            total_error = await _final_correction(
                job_id, current_ra, current_dec, delta_ra, delta_dec, total_error
            )
            job = store.get_job(job_id)
            if job is None or job.status in ("aborted", "failed"):
                return
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


async def _final_correction(
    job_id: str,
    current_ra: float,
    current_dec: float,
    delta_ra: float,
    delta_dec: float,
    total_error: float,
) -> float:
    """Close the last of the offset undamped, then re-measure where we ended up.

    Damping exists to keep the loop from oscillating while it is chasing a
    large offset. On the final step there is nothing left to oscillate into,
    so applying the full delta is safe and removes the one term the loop
    otherwise leaves on the sky.

    Returns the re-measured error, or the original if anything goes wrong --
    the job has already met its threshold, so a failure here is cosmetic and
    must never turn a successful lock into a failure.
    """
    try:
        await slew(current_ra + delta_ra, current_dec + delta_dec)
        image, hint = await capture_frame(frame_index=0)
        result = solve(image, hint=hint)
    except Exception as exc:
        log.warning(f"[{job_id}] final correction failed, keeping earlier fix: {exc}")
        return total_error

    job = store.get_job(job_id)
    if job is None:
        return total_error

    d_ra = (job.target_ra - result.ra + 180) % 360 - 180
    d_dec = job.target_dec - result.dec
    refined = math.sqrt(d_ra**2 + d_dec**2) * 3600

    store.update_job(
        job_id,
        current_ra=round(result.ra, 6),
        current_dec=round(result.dec, 6),
        error_ra_arcsec=round(abs(d_ra) * 3600, 2),
        error_dec_arcsec=round(abs(d_dec) * 3600, 2),
        total_error_arcsec=round(refined, 2),
        solver_residual_arcsec=result.residual_arcsec,
        stars_matched=result.stars_matched,
        solve_time_ms=result.solve_time_ms,
    )
    job.history.append(
        {
            "iteration": job.iteration,
            "error_arcsec": round(refined, 2),
            "ra": round(result.ra, 6),
            "dec": round(result.dec, 6),
            "solver_residual_arcsec": result.residual_arcsec,
            "elapsed_seconds": job.elapsed_seconds,
            "final_correction": True,
        }
    )
    log.info(f'[{job_id}] final correction: {total_error:.1f}" -> {refined:.1f}"')
    return refined


async def _fail(job_id: str, reason: str, message: str) -> None:
    store.update_job(job_id, status="failed", reason=reason, message=message)
    await halt()
    job = store.get_job(job_id)
    if job:
        await store.broadcast_job(job)
    log.error(f"[{job_id}] FAILED — {reason}: {message}")
