"""
demo.py — Watch the whole thing work, end to end, with Stellarium on screen.

Drives the harness over HTTP, exactly the way the MCP server will. If this
passes, the MCP side is just thin wrappers over these same calls.

    # terminal 1
    uv run uvicorn harness.main:app --port 8000

    # terminal 2 (put Stellarium somewhere you can see it first)
    uv run python -m harness.demo
    uv run python -m harness.demo "M42" deep_sky
    uv run python -m harness.demo Saturn planet

Point Stellarium at your second monitor and watch the view jump to the park
position, walk in to the target over ~8 iterations, then zoom right down onto
it once the loop reports a lock.

MEMORY: the default tetra3 backend needs ~320 MB, and Stellarium itself holds
~1.1 GB. On a machine with little free RAM an OOM killer (earlyoom, systemd-oomd)
will pick Stellarium as its victim and SIGKILL it mid-run -- which looks like
Stellarium "crashing" but is not. If free memory is tight, run light mode:

    CCE_SOLVER_BACKEND=hint uv run uvicorn harness.main:app --port 8000

That skips the plate solve and the starfield render, dropping the harness to
~65 MB. The loop, the API and the dashboard stream are all identical; only the
solve step is bypassed.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import sys

import httpx
import websockets

from . import stellarium

# Override when the harness is on another port:  ASTRA_HARNESS_URL=http://localhost:8001
BASE = os.environ.get("ASTRA_HARNESS_URL", "http://localhost:8000").rstrip("/")
WS = BASE.replace("http://", "ws://").replace("https://", "wss://") + "/ws"

# Somewhere far from anything we would plausibly ask for, so the first slew is
# an obvious jump on screen rather than a subtle nudge.
PARK_RA, PARK_DEC = 300.0, -40.0

# Purely cosmetic: 60 deg (Stellarium's default) makes the target a speck.
# Zoom is a presentation concern, not telescope control, so it is not part of
# the MCP surface -- the demo calls Stellarium directly for it.
DEMO_FOV_DEG = 20.0

# Once we are locked on, zoom down onto the target the way you would swap in a
# high-power eyepiece. Values are display choices: tight enough that the object
# fills a useful part of the frame, wide enough that it still reads as a disk
# and not a wall of pixels.
SHOWCASE_FOV_DEG = {
    "planet": 0.02,  # Jupiter's disk is ~40" -- roughly half the frame
    "star": 0.20,  # a point source; tighter just makes a bigger blob
    "deep_sky": 1.50,  # M42 is ~1 deg across
    "comet": 0.50,
    "asteroid": 0.20,
}
SHOWCASE_FOV_DEFAULT = 0.50
# The Moon and Sun are half a degree wide -- nothing like the point-like planets.
SHOWCASE_FOV_BY_NAME = {"moon": 1.0, "sun": 1.0}

# Never zoom past the point where the loop's own pointing residual would push
# the target out of frame: hold the field at >= 6x the worst-case error.
#
# Worst case is not the reported error. The loop stops when its *plate-solved
# estimate* of the error drops below the threshold, and that estimate carries
# the solver's own residual -- so a run reporting 25" can sit 38" off the mark.
# Budget for error + residual or the zoom lands tighter than the pointing
# actually justifies.
SHOWCASE_ERROR_MARGIN = 6.0

# Stellarium's REST zoom is a jump cut. Walking it down in steps is what makes
# it read as a zoom rather than a scene change.
ZOOM_STEPS = 30
ZOOM_STEP_DELAY_S = 0.06

BAR_WIDTH = 34
DIM, BOLD, GREEN, RED, YELLOW, CYAN, RESET = (
    "\033[2m",
    "\033[1m",
    "\033[32m",
    "\033[31m",
    "\033[33m",
    "\033[36m",
    "\033[0m",
)


def rule(title: str = "") -> None:
    print(f"\n{DIM}{'─' * 68}{RESET}")
    if title:
        print(f"{BOLD}{title}{RESET}")


def error_bar(err: float, first: float) -> str:
    """Log-scale bar — error spans ~5 orders of magnitude over a run."""
    if first <= 0 or err <= 0:
        return ""
    frac = math.log10(max(err, 1e-6)) / math.log10(max(first, 10))
    filled = max(0, min(BAR_WIDTH, int(frac * BAR_WIDTH)))
    colour = GREEN if err < 60 else YELLOW if err < 3600 else RED
    return f"{colour}{'█' * filled}{DIM}{'·' * (BAR_WIDTH - filled)}{RESET}"


def available_memory_mb() -> float | None:
    """MemAvailable from /proc, or None off Linux."""
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    return None


async def preflight(client: httpx.AsyncClient) -> bool:
    rule("1 · Preflight")
    try:
        status = (await client.get(f"{BASE}/status")).json()
    except Exception:
        print(f"  {RED}✗{RESET} Harness not reachable at {BASE}")
        print(f"    Start it:  {CYAN}uv run uvicorn harness.main:app --port 8000{RESET}")
        return False

    ok = status["stellarium_reachable"] and status["solver_ready"]
    mark = f"{GREEN}✓{RESET}" if ok else f"{RED}✗{RESET}"
    print(f"  {mark} harness up · simulation={status['simulation']}")
    print(f"    solver:     {status['solver_backend']} (ready={status['solver_ready']})")
    print(f"    stellarium: reachable={status['stellarium_reachable']}")
    print(f'    threshold:  {status["converge_threshold_arcsec"]}"')

    if not status["stellarium_reachable"]:
        print(f"  {RED}✗{RESET} Stellarium is not answering on :8090")
        print("    Enable Plugins → Remote Control → Load at startup, then restart it.")
        return False
    if not status["solver_ready"]:
        print(f"  {RED}✗{RESET} tetra3 failed to load its database")
        return False

    obs = (await client.get(f"{BASE}/observer")).json()
    print(f"   observer: lat={obs['latitude']:.4f} lon={obs['longitude']:.4f}")

    # Stellarium holds ~1.1 GB and is the fattest process on the box, so it is
    # what an OOM killer reaches for first. Warn before we make things worse.
    avail = available_memory_mb()
    if avail is not None:
        tight = avail < 1200 and status["solver_backend"] == "tetra3"
        mark = f"{YELLOW}!{RESET}" if tight else f"{GREEN}✓{RESET}"
        print(f"  {mark} memory:     {avail:.0f} MB available")
        if tight:
            print(f"    {YELLOW}Low. The OOM killer may SIGKILL Stellarium mid-run.{RESET}")
            print("    Restart the harness in light mode (~65 MB instead of ~320 MB):")
            print(
                f"    {CYAN}CCE_SOLVER_BACKEND=hint uv run uvicorn harness.main:app "
                f"--port 8000{RESET}"
            )
    return True


async def show_sky(client: httpx.AsyncClient) -> None:
    rule("2 · What's up right now")
    vis = (await client.get(f"{BASE}/objects/visible", params={"category": "all"})).json()
    print(f"  {vis['count']} objects above {vis['min_altitude_deg']}° at {vis['observed_at']}")
    for o in vis["objects"][:8]:
        print(
            f"    {o['name']:<12} {DIM}{o['category']:<9}{RESET} "
            f"alt={o['altitude_deg']:5.1f}°  mag={o['magnitude']}"
        )


async def park(client: httpx.AsyncClient) -> None:
    rule("3 · Parking the view somewhere obviously wrong")
    try:
        # The previous run ends with the clock frozen so the target holds
        # still. Undo that here, or stage 2's altitudes drift away from what
        # is actually drawn on screen.
        await stellarium.do_action("actionReturn_To_Current_Time")
        await stellarium.set_time_rate(stellarium.REAL_TIME_RATE)
        # Resyncing the clock pays back the whole time we were frozen in one
        # jump, and the view swings 15" per second of it. Take that swing now,
        # while the view is deliberately parked in the wrong place anyway.
        await stellarium.wait_until_view_still()
        print(f"  {DIM}sky resumed at real time{RESET}")
    except Exception as exc:
        print(f"  {DIM}(could not resume the clock: {exc}){RESET}")
    try:
        await stellarium.zoom(DEMO_FOV_DEG)
        print(f"  {DIM}zoomed Stellarium to {DEMO_FOV_DEG:.0f}° so the target is visible{RESET}")
    except Exception as exc:
        print(f"  {DIM}(could not set zoom: {exc}){RESET}")
    print(f"  {CYAN}→ watch Stellarium jump{RESET}")
    await client.post(f"{BASE}/motor/step", params={"ra_deg": PARK_RA, "dec_deg": PARK_DEC})
    await asyncio.sleep(1.5)
    solved = (await client.post(f"{BASE}/capture/solve")).json()
    how = (
        f"plate solved from {solved['stars_matched']} stars in {solved['solve_time_ms']:.0f}ms"
        if solved["backend"] == "tetra3"
        else "read from Stellarium — light mode, no plate solve"
    )
    print(f"  parked at RA={solved['ra']:.3f}° Dec={solved['dec']:.3f}°  {DIM}({how}){RESET}")


async def calibrate(client: httpx.AsyncClient, target: str, category: str) -> bool:
    rule(f"4 · Calibrating to {target}")

    async with websockets.connect(WS) as ws:
        await ws.recv()  # snapshot on connect

        resp = await client.post(f"{BASE}/calibrate", json={"target": target, "category": category})
        if resp.status_code != 200:
            print(f"  {RED}✗{RESET} {resp.status_code}: {resp.json().get('detail')}")
            return False
        job = resp.json()
        print(
            f"  resolved via {job['source']}: "
            f"RA={job['target_ra']:.4f}° Dec={job['target_dec']:.4f}°"
        )
        print(f"  job {job['job_id']}\n")
        print(f"  {DIM}iter        error          convergence{RESET}")

        first_err = None
        last_shown = 0
        while True:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=90))
            except TimeoutError:
                print(f"  {RED}✗{RESET} timed out waiting for updates")
                return False
            if msg["type"] != "calibration_update" or msg["total_error_arcsec"] is None:
                continue

            err = msg["total_error_arcsec"]
            first_err = first_err or err
            # The converging iteration broadcasts twice (once running, once
            # done) -- print each iteration only the first time we see it.
            if msg["iteration"] > last_shown:
                last_shown = msg["iteration"]
                detail = (
                    f'solver {msg["solver_residual_arcsec"]}"'
                    if msg["solver_backend"] == "tetra3"
                    else "hint"
                )
                print(
                    f'   {msg["iteration"]:2d}   {err:12.1f}"  {error_bar(err, first_err)}  '
                    f"{DIM}{detail}{RESET}"
                )
            if msg["status"] != "running":
                break

    final = (await client.get(f"{BASE}/calibrate/{job['job_id']}")).json()
    rule("5 · Result")
    if final["status"] == "done":
        print(
            f"  {GREEN}✓ LOCKED ON {target.upper()}{RESET} — "
            f"{final['iteration']} iterations in {final['elapsed_seconds']}s"
        )
        print(
            f'    final error:     {final["total_error_arcsec"]}" '
            f'(threshold {final["threshold_arcsec"]:.0f}")'
        )
        if final["solver_backend"] == "tetra3":
            print(
                f'    solver residual: {final["solver_residual_arcsec"]}" '
                f"({final['stars_matched']} stars, {final['solve_time_ms']:.0f}ms/solve)"
            )
        else:
            print(f"    solver:          {final['solver_backend']} — no plate solve performed")
        print(f"\n  {CYAN}Stellarium is now centred on {target}.{RESET}")
        await showcase(target, category, final)
        return True

    print(f"  {RED}✗ {final['status'].upper()}{RESET} after {final['iteration']} iterations")
    print(f"    reason:  {final['reason']}")
    print(f"    message: {final['message']}")
    return False


def angular_sep(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Separation between two (RA, Dec) pairs, in arcseconds."""
    d_ra = (b[0] - a[0] + 180) % 360 - 180
    return math.hypot(d_ra * math.cos(math.radians(a[1])), b[1] - a[1]) * 3600


async def true_offset(target: str) -> float | None:
    """How far the boresight really is from the target, per the simulator.

    The harness only ever sees the plate solver's opinion, and after the final
    correction that opinion is dominated by the solver's own residual -- it
    reports ~18" while sitting half an arcsecond off. Stellarium knows where it
    actually drew the object, so ask it. Demo-only: this is the answer sheet,
    and nothing in the control loop is allowed to read it.
    """
    try:
        drawn = await stellarium.get_object_position(target)
        if drawn is None:
            return None
        return angular_sep(await stellarium.get_view(), drawn)
    except Exception:
        return None


def worst_case_error(final: dict) -> float | None:
    """Upper bound on how far off we are, going only on what the loop can see."""
    err = final.get("total_error_arcsec")
    if err is None:
        return None
    return err + (final.get("solver_residual_arcsec") or 0.0)


def showcase_fov(target: str, category: str, worst_arcsec: float | None) -> float:
    """How far to zoom in, given what we are looking at and how well we hit it."""
    fov = SHOWCASE_FOV_BY_NAME.get(
        target.lower(), SHOWCASE_FOV_DEG.get(category, SHOWCASE_FOV_DEFAULT)
    )
    if worst_arcsec:
        fov = max(fov, worst_arcsec / 3600 * SHOWCASE_ERROR_MARGIN)
    return fov


async def zoom_to(target_fov: float, steps: int = ZOOM_STEPS) -> None:
    """Ease the field of view down to target_fov over ~2s."""
    try:
        start = await stellarium.get_fov()
    except Exception:
        start = DEMO_FOV_DEG

    if target_fov >= start:
        await stellarium.zoom(target_fov)
        return

    # Geometric, not linear: the field spans three orders of magnitude, and
    # equal *ratios* per step are what feel like a constant zoom rate.
    ratio = (target_fov / start) ** (1 / steps)
    fov = start
    for _ in range(steps):
        fov *= ratio
        await stellarium.zoom(fov)
        await asyncio.sleep(ZOOM_STEP_DELAY_S)
    await stellarium.zoom(target_fov)


async def hold_sky(attempts: int = 4) -> bool:
    """Freeze the clock and make it stick.

    The calibrator hands the clock back the instant its loop exits, and that
    restore is still in flight while we get here -- we are racing it. Losing
    the race leaves the sky running and the target visibly creeping away under
    the zoom, so set the rate and read it back until it holds.
    """
    for attempt in range(attempts):
        try:
            await stellarium.set_time_rate(0.0)
            if await stellarium.get_time_rate() == 0.0:
                return True
        except Exception:
            pass
        await asyncio.sleep(0.25 * (attempt + 1))
    return False


async def showcase(target: str, category: str, final: dict) -> None:
    rule(f"6 \u00b7 Zooming in on {target}")
    # Freeze before measuring, not after. At this magnification a live sky
    # visibly walks the target off centre while anyone is still looking at it
    # -- the Moon at ~0.9"/s crosses its own width in ten minutes -- and a
    # measurement taken while it is still moving picks the framing for a
    # position the target has already left.
    held = await hold_sky()
    if not held:
        print(f"  {DIM}(could not hold the sky still){RESET}")

    # Prefer the measured offset: the solver's estimate is floored by its own
    # residual and would hold the zoom ~40x wider than the pointing warrants.
    measured = await true_offset(target)
    worst = worst_case_error(final)
    fov = showcase_fov(target, category, measured if measured is not None else worst)

    print(f"  {CYAN}\u2192 watch Stellarium zoom{RESET}")
    try:
        await zoom_to(fov)
    except Exception as exc:
        print(f"  {DIM}(could not zoom: {exc}){RESET}")
        return

    print(f"  FOV {fov:.3f}\u00b0 \u2014 {DEMO_FOV_DEG / fov:.0f}x tighter than the search field")
    if measured is not None:
        print(
            f'    {target} is {measured:.1f}" off centre '
            f"= {measured / 3600 / fov * 100:.1f}% of the field width"
        )
        est = final["total_error_arcsec"]
        resid = final.get("solver_residual_arcsec") or 0
        print(
            f'    {DIM}the loop only knows the solver estimate, {est:.0f}"; its residual '
            f"alone is {resid:.0f}\", so it has hit the solver's noise floor{RESET}"
        )
    elif worst:
        print(f'    pointing is within {worst:.0f}" = {worst / 3600 / fov * 100:.0f}% of the field')
    if held:
        print(f"  {GREEN}\u2713{RESET} sky frozen \u2014 {target} will stay exactly where it is")
    print(
        f"  {DIM}the next run resets the zoom to {DEMO_FOV_DEG:.0f}\u00b0 and resumes "
        f"the clock (or press K in Stellarium){RESET}"
    )


async def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else "Jupiter"
    category = sys.argv[2] if len(sys.argv) > 2 else "planet"

    print(f"{BOLD}Astra — end-to-end demo{RESET}  {DIM}target={target} category={category}{RESET}")

    async with httpx.AsyncClient(timeout=30.0) as client:
        if not await preflight(client):
            return 1
        await show_sky(client)
        await park(client)
        ok = await calibrate(client, target, category)

    rule()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
