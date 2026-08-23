"""
calibrator.py — The closed-loop calibration algorithm.

Core equations:
  err     = √(ΔRA² + ΔDec²) × 3600          (arcseconds)
  errₙ    = err₀ × (1 − k)ⁿ                 (geometric convergence)
  cmd'    = cmd + k × Δ                      (damped correction)

k = 0.8 (damping factor) — prevents oscillation from worm gear backlash.

We correct the *commanded* coordinate, not the measured one. A mount with a
pointing error E lands at cmd + E for whatever it is told, so re-deriving the
command from the plate solve — cmd' = solved + k·Δ — reaches a steady state
where Δ = E/k and simply stops there, converging to a fixed 1.25·E offset
instead of to the target. Nudging the command carries E on both sides of the
equation and it cancels, which is also how a real mount is corrected.

With Stellarium as the mount E is zero and the two forms agree exactly, which
is why this only shows up against real hardware.
"""

from __future__ import annotations

import asyncio
import logging
import math
import uuid

from . import mount, stellarium
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
    """Pause Stellarium's clock and confirm it stuck.

    A single POST can lose a race with a previous restore still in flight
    (same issue demo.py's hold_sky documents). Retry until timerate reads 0.
    """
    if not cfg.simulation.enabled:
        return None
    previous: float | None = None
    try:
        previous = await stellarium.get_time_rate()
    except Exception as exc:
        log.warning(f"Could not read Stellarium time rate: {exc}")
    for attempt in range(4):
        try:
            await stellarium.set_time_rate(0.0)
            if await stellarium.get_time_rate() == 0.0:
                log.info(f"Stellarium clock paused (was {previous} JD/s)")
                return previous
        except Exception as exc:
            log.warning(f"Could not pause Stellarium's clock (try {attempt + 1}): {exc}")
        await asyncio.sleep(0.25 * (attempt + 1))
    log.warning("Stellarium clock did not stay at 0 — sky may keep moving")
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

# Wider than the showcase zoom, narrow enough that the target is visible.
_SEARCH_FOV_DEG = 20.0


async def _calibration_loop(job_id: str) -> None:
    """Freeze the sky, run the loop, keep it frozen on lock."""
    # A previous locked run leaves the sky frozen and the FOV at showcase
    # level.  Undo both before the new loop starts — same as demo.py's park().
    if cfg.simulation.enabled:
        try:
            await stellarium.zoom(_SEARCH_FOV_DEG)
        except Exception as exc:
            log.warning(f"Could not reset FOV from previous run: {exc}")
    previous_rate = await _freeze_sky()
    if cfg.simulation.enabled and not mount.is_indi():
        # The clock was just resynchronised, and Stellarium may still owe us
        # the resulting swing. Take it here, where the loop is about to
        # measure everything anyway, rather than after it has finished.
        await stellarium.wait_until_view_still()
    try:
        await _run_calibration(job_id)
    finally:
        # On a successful lock the showcase zoom is already done and we want
        # the sky to stay frozen so the target holds its position at high
        # magnification.  Only resume the clock on abort / failure / crash.
        job = store.get_job(job_id)
        if job is None or job.status != "done":
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
            total_error = await _final_correction(job_id, delta_ra, delta_dec, total_error)
            total_error = await _settle_and_verify(job_id, total_error)
            job = store.get_job(job_id)
            if job is None or job.status in ("aborted", "failed"):
                return

            # The settle check above confirmed the lock at search FOV.  The
            # showcase zoom is purely cosmetic — no need to re-verify through
            # it.  Doing so would capture and solve at 0.02° FOV and, if the
            # error somehow exceeds threshold, slew the mount at showcase
            # magnification — a violent on-screen jump that ruins the framing.
            await mount.refresh_display()
            store.update_job(
                job_id,
                status="done",
                message=f'Locked on {job.target}. Final error {total_error:.1f}"',
            )
            await store.broadcast_job(job)
            log.info(f'[{job_id}] LOCKED — {total_error:.1f}" after {iteration} iterations')
            await _showcase_zoom(job.target, job.source, total_error)
            return

        # 5. Nudge the command by the damped error.
        # The mount's own idea of where it is *is* the last command it
        # accepted, so this is "go a bit further in this direction" rather
        # than "you are here, go there".
        try:
            commanded_ra, commanded_dec = await mount.reported_position()
        except Exception as exc:
            await _fail(job_id, "mount_timeout", f"Could not read mount position: {exc}")
            return

        corrected_ra = commanded_ra + k * delta_ra
        corrected_dec = commanded_dec + k * delta_dec

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
    delta_ra: float,
    delta_dec: float,
    total_error: float,
) -> float:
    """Close the last of the offset undamped, then re-measure where we ended up.

    Damping exists to keep the loop from oscillating while it is chasing a
    large offset. On the final step there is nothing left to oscillate into,
    so applying the full delta is safe and removes the one term the loop
    otherwise leaves on the sky.

    The refinement is only kept if it actually leaves us inside the
    convergence threshold. A bad solve, a mount that did not go where it was
    told, or a simulator hiccup can all make this step worse rather than
    better, and accepting it unconditionally would let the job report success
    while sitting hundreds of arcseconds off. On a bad outcome we put the
    mount back where it was and keep the measurement we already trusted.

    Returns the error to report, which is never worse than the one passed in.
    """
    thr = cfg.calibration.converge_threshold_arcsec
    try:
        commanded_ra, commanded_dec = await mount.reported_position()
        await slew(commanded_ra + delta_ra, commanded_dec + delta_dec)
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

    if refined > thr:
        log.warning(
            f'[{job_id}] final correction left us {refined:.1f}" out, worse than the '
            f'{thr:.0f}" threshold -- reverting to the {total_error:.1f}" fix'
        )
        try:
            await slew(commanded_ra, commanded_dec)
        except Exception as exc:
            log.error(f"[{job_id}] could not revert the final correction: {exc}")
        return total_error

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


# The sky is held still before each re-check, and the pointing is only
# accepted once a fresh solve agrees. Three tries is enough for a clock that
# jumped once; a mount that cannot hold position at all is a different fault.
_SETTLE_ATTEMPTS = 3


async def _settle_and_verify(job_id: str, total_error: float) -> float:
    """Hold the sky still, then confirm the pointing survived being held.

    Stellarium's view is anchored so that an advancing simulation clock walks
    it east at the sidereal rate -- about 15" per simulated second, purely in
    RA, with Dec untouched. The clock is paused for the whole loop, but that
    pause is a *request* to another process: Stellarium can apply it late and
    then catch up on the backlog in a single jump. When the jump lands after
    the last solve, every number the loop can see still looks perfect while
    the target has quietly walked a degree off the frame -- the same failure
    the arrival wait in mount.py exists to catch, arriving from the other end.

    The plate solver is both the victim and the cure. It reports where we
    actually are, so one more solve *after* the sky is genuinely still says
    whether the lock is real. Nothing here reads the simulator's answer sheet:
    it is the same measurement the loop has used all along, and on real
    hardware it is simply "check the pointing once the mount has stopped".
    """
    thr = cfg.calibration.converge_threshold_arcsec
    job = store.get_job(job_id)
    if job is None:
        return total_error

    measured = total_error
    for attempt in range(_SETTLE_ATTEMPTS):
        await _freeze_sky()
        if cfg.simulation.enabled and not mount.is_indi():
            # With an INDI mount the Stellarium view is a display, not the
            # boresight, so its settling says nothing about the pointing.
            await stellarium.wait_until_view_still()
        try:
            image, hint = await capture_frame(frame_index=0)
            result = solve(image, hint=hint)
        except Exception as exc:
            log.warning(f'[{job_id}] settle check failed, keeping {total_error:.1f}": {exc}')
            return total_error

        d_ra = (job.target_ra - result.ra + 180) % 360 - 180
        d_dec = job.target_dec - result.dec
        measured = math.hypot(d_ra, d_dec) * 3600

        store.update_job(
            job_id,
            current_ra=round(result.ra, 6),
            current_dec=round(result.dec, 6),
            error_ra_arcsec=round(abs(d_ra) * 3600, 2),
            error_dec_arcsec=round(abs(d_dec) * 3600, 2),
            total_error_arcsec=round(measured, 2),
            solver_residual_arcsec=result.residual_arcsec,
            stars_matched=result.stars_matched,
            solve_time_ms=result.solve_time_ms,
        )

        if measured <= thr:
            if attempt:
                log.info(f'[{job_id}] settled after {attempt} extra correction(s): {measured:.1f}"')
            return measured

        log.warning(
            f'[{job_id}] the sky moved after lock -- {measured:.1f}" off once held still '
            f"(attempt {attempt + 1}/{_SETTLE_ATTEMPTS}); correcting again"
        )
        job.history.append(
            {
                "iteration": job.iteration,
                "error_arcsec": round(measured, 2),
                "ra": round(result.ra, 6),
                "dec": round(result.dec, 6),
                "solver_residual_arcsec": result.residual_arcsec,
                "elapsed_seconds": job.elapsed_seconds,
                "settle_correction": True,
            }
        )
        try:
            commanded_ra, commanded_dec = await mount.reported_position()
            await slew(commanded_ra + d_ra, commanded_dec + d_dec)
        except Exception as exc:
            log.error(f"[{job_id}] could not correct after the sky moved: {exc}")
            return measured

    log.warning(f'[{job_id}] still {measured:.1f}" out after {_SETTLE_ATTEMPTS} settle attempts')
    return measured


_SHOWCASE_FOV = {
    "horizons": 0.02,
    "simbad": 1.50,
    "planet": 0.02,
    "deep_sky": 1.50,
    "star": 0.20,
}
_SHOWCASE_BY_NAME = {"moon": 1.0, "sun": 1.0}
# Stellarium's own object type, which is the only thing that can tell Vega
# apart from the Andromeda Galaxy -- both come back from the resolver as
# "simbad", and they want fields nearly ten times apart.
_SHOWCASE_BY_TYPE = {
    "star": 0.20,
    "planet": 0.02,
    "galaxy": 1.50,
    "nebula": 1.50,
    "open cluster": 1.50,
    "globular cluster": 0.50,
}
_SHOWCASE_MARGIN = 6.0
_ZOOM_STEPS = 30
_ZOOM_STEP_S = 0.06


async def _showcase_zoom(target: str, source: str, error_arcsec: float) -> None:
    """Cosmetic FOV walk after lock — same numbers as demo.py. Never fails the job."""
    if not cfg.simulation.enabled:
        return
    kind = None
    try:
        kind = await stellarium.get_object_type(target)
    except Exception as exc:
        log.debug(f"Could not read Stellarium's type for {target}: {exc}")
    fov = _SHOWCASE_BY_NAME.get(target.lower()) or (
        _SHOWCASE_BY_TYPE.get(kind or "") or _SHOWCASE_FOV.get(source, 0.50)
    )
    if error_arcsec > 0:
        fov = max(fov, error_arcsec / 3600 * _SHOWCASE_MARGIN)
    try:
        start = await stellarium.get_fov()
        if fov >= start:
            await stellarium.zoom(fov)
            return
        ratio = (fov / start) ** (1 / _ZOOM_STEPS)
        current = start
        for _ in range(_ZOOM_STEPS):
            current *= ratio
            await stellarium.zoom(current)
            await asyncio.sleep(_ZOOM_STEP_S)
        await stellarium.zoom(fov)
    except Exception as exc:
        log.warning(f"showcase zoom failed: {exc}")


async def _fail(job_id: str, reason: str, message: str) -> None:
    store.update_job(job_id, status="failed", reason=reason, message=message)
    await halt()
    job = store.get_job(job_id)
    if job:
        await store.broadcast_job(job)
    log.error(f"[{job_id}] FAILED — {reason}: {message}")
