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
position, then walk in to the target over ~8 iterations.

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
    import math

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
    print(f"    observer:   lat={obs['latitude']:.4f} lon={obs['longitude']:.4f}")

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
        return True

    print(f"  {RED}✗ {final['status'].upper()}{RESET} after {final['iteration']} iterations")
    print(f"    reason:  {final['reason']}")
    print(f"    message: {final['message']}")
    return False


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
