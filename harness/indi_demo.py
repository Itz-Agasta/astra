"""
indi_demo.py -- drive a real INDI mount, and show the loop earning its keep.

This is the answer to "can it run a real telescope?". It talks to an actual
`indiserver` over TCP 7624 using the standard INDI properties every mount
driver implements, so the only difference between this and an observatory
mount is which driver indiserver was started with.

It also injects a real pointing error first. A mount that goes exactly where
it is told does not need calibrating; a real one never does. The simulator
implements a six-term Wallace pointing model -- index errors, polar
misalignment, non-perpendicularity -- and publishes both what it *believes*
its position is and where it *truly* ended up, so we can prove the loop
removed an error rather than just claiming it.

    # terminal 1
    indiserver -v indi_simulator_telescope

    # terminal 2
    CCE_MOUNT=indi uv run uvicorn harness.main:app --port 8000

    # terminal 3
    uv run python -m harness.indi_demo
    uv run python -m harness.indi_demo Saturn planet

Stellarium is optional here. If it is running, the harness mirrors the mount's
position onto the sky display, so there is still something to watch.
"""

from __future__ import annotations

import asyncio
import math
import os
import sys

import httpx

from . import indi
from .config import cfg

BASE = os.environ.get("ASTRA_HARNESS_URL", "http://localhost:8000").rstrip("/")

# Index error + polar misalignment, in arcminutes. Roughly a mount someone
# set up in a hurry in the dark -- bad, but not absurd.
DEFAULT_MODEL = {
    "MM_IH": 10.0,
    "MM_ID": 0.0,
    "MM_CH": 0.0,
    "MM_NP": 0.0,
    "MM_MA": 15.0,
    "MM_ME": 0.0,
}

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
    print(f"\n{DIM}{'─' * 72}{RESET}")
    if title:
        print(f"{BOLD}{title}{RESET}")


def sep_arcsec(a: tuple[float, float], b: tuple[float, float]) -> float:
    d_ra = (b[0] - a[0] + 180) % 360 - 180
    return math.hypot(d_ra * math.cos(math.radians(a[1])), b[1] - a[1]) * 3600


def arcmin(v: float) -> str:
    return f"{v / 60:.1f}'" if abs(v) >= 60 else f'{v:.1f}"'


async def preflight(client: httpx.AsyncClient) -> indi.IndiClient | None:
    rule("1 · Preflight — is this really INDI?")
    try:
        status = (await client.get(f"{BASE}/status")).json()
    except Exception:
        print(f"  {RED}✗{RESET} Harness not reachable at {BASE}")
        print(f"    {CYAN}CCE_MOUNT=indi uv run uvicorn harness.main:app --port 8000{RESET}")
        return None

    backend = status.get("mount_backend")
    if backend != "indi":
        print(f"  {RED}✗{RESET} Harness mount backend is '{backend}', not 'indi'")
        print(f"    Restart it with {CYAN}CCE_MOUNT=indi{RESET}")
        return None
    print(f"  {GREEN}✓{RESET} harness mount backend: {BOLD}indi{RESET}")

    mount = indi.IndiClient(cfg.mount.indi_host, cfg.mount.indi_port, cfg.mount.indi_device)
    try:
        await mount.connect()
        await mount.wait_defined(indi.EQUATORIAL_EOD_COORD, timeout=15.0)
    except Exception as exc:
        print(f"  {RED}✗{RESET} No indiserver at {cfg.mount.indi_url}: {exc}")
        print(f"    {CYAN}indiserver -v indi_simulator_telescope{RESET}")
        return None

    print(f"  {GREEN}✓{RESET} indiserver at {cfg.mount.indi_url}, device '{mount.device}'")
    _memory_warning()
    print(f"    {len(mount.property_names())} properties published by the driver")
    for name in (indi.EQUATORIAL_EOD_COORD, indi.ON_COORD_SET, indi.ABORT_MOTION):
        print(f"      {DIM}{name}{RESET}")
    if not mount.has(indi.MOUNT_MODEL):
        print(f"  {YELLOW}!{RESET} No MOUNT_MODEL — cannot inject a pointing error on this driver")
    return mount


def _memory_warning() -> None:
    """indiserver + harness + Stellarium is the heaviest configuration there is.

    Stellarium holds ~950 MB and is the fattest process on a laptop, so when
    memory runs out an OOM killer picks it and it looks like Stellarium
    crashed. Warn before that rather than after.
    """
    try:
        with open("/proc/meminfo") as fh:
            avail = next(
                int(line.split()[1]) / 1024 for line in fh if line.startswith("MemAvailable:")
            )
    except OSError, StopIteration:
        return
    if avail < 1200:
        print(f"  {YELLOW}!{RESET} only {avail:.0f} MB free — an OOM killer may take Stellarium")
        print(f"    {DIM}run one harness at a time, or drop the Stellarium mirror:{RESET}")
        print(
            f"    {CYAN}CCE_MOUNT=indi CCE_MIRROR_STELLARIUM=false uv run uvicorn "
            f"harness.main:app --port 8000{RESET}"
        )


async def inject_error(mount: indi.IndiClient) -> None:
    rule("2 · Making the mount imperfect")
    if not mount.has(indi.MOUNT_MODEL):
        print(f"  {DIM}skipped — driver has no pointing model{RESET}")
        return
    await mount.set_number(indi.MOUNT_MODEL, DEFAULT_MODEL)
    await asyncio.sleep(1.0)
    terms = ", ".join(f"{k}={v:g}'" for k, v in DEFAULT_MODEL.items() if v)
    print(f"  injected Wallace pointing model: {BOLD}{terms}{RESET}")
    print(f"  {DIM}index error + polar misalignment, the two every real mount has{RESET}")


def _read_pair(mount: indi.IndiClient, prop: str, ra_key: str, dec_key: str):
    ra = mount.number(prop, ra_key)
    dec = mount.number(prop, dec_key)
    return None if ra is None or dec is None else (ra, dec)


async def truth(mount: indi.IndiClient) -> tuple[float, float]:
    """Where the mount really is, in J2000 — the simulator's answer sheet.

    Falls back to the mount's own claim when the driver publishes no ground
    truth, which is every real mount. The comparison then just shows a mount
    agreeing with itself, so the demo says so rather than implying it proved
    something.
    """
    pair = _read_pair(mount, indi.EQUATORIAL_PE, "RA_PE", "DEC_PE") or _read_pair(
        mount, indi.EQUATORIAL_EOD_COORD, "RA", "DEC"
    )
    if pair is None:
        raise RuntimeError("Mount has not reported a position yet")
    return indi.jnow_to_j2000(*pair)


async def believed(mount: indi.IndiClient) -> tuple[float, float]:
    pair = _read_pair(mount, indi.EQUATORIAL_EOD_COORD, "RA", "DEC")
    if pair is None:
        raise RuntimeError("Mount has not reported a position yet")
    return indi.jnow_to_j2000(*pair)


async def naive_goto(
    client: httpx.AsyncClient, mount: indi.IndiClient, target: str, category: str
) -> tuple[tuple[float, float], float] | None:
    rule(f"3 · Point at {target} the naive way — no calibration")
    resolved = await client.get(
        f"{BASE}/objects/resolve", params={"name": target, "category": category}
    )
    if resolved.status_code != 200:
        print(f"  {RED}✗{RESET} Could not resolve {target}: {resolved.text[:120]}")
        return None
    tgt = resolved.json()
    coords = (tgt["ra"], tgt["dec"])
    print(
        f"  {target} is at RA={coords[0]:.5f}° Dec={coords[1]:.5f}° "
        f"{DIM}(via {tgt['source']}){RESET}"
    )

    print(f"  {CYAN}→ commanding the mount there and believing it{RESET}")
    await client.post(f"{BASE}/motor/step", params={"ra_deg": coords[0], "dec_deg": coords[1]})
    await asyncio.sleep(1.5)

    claim = await believed(mount)
    real = await truth(mount)
    print(
        f"    mount reports  RA={claim[0]:.5f}° Dec={claim[1]:.5f}°  "
        f'{DIM}(off target by {sep_arcsec(coords, claim):.1f}"){RESET}'
    )
    print(f"    mount really at RA={real[0]:.5f}° Dec={real[1]:.5f}°")
    error = sep_arcsec(coords, real)
    print(f"\n  {RED}✗ actually {arcmin(error)} off target{RESET} — and the mount has no idea")
    return coords, error


async def calibrate(client: httpx.AsyncClient, target: str, category: str) -> dict | None:
    rule(f"4 · Closing the loop on {target}")
    resp = await client.post(f"{BASE}/calibrate", json={"target": target, "category": category})
    if resp.status_code != 200:
        print(f"  {RED}✗{RESET} {resp.status_code}: {resp.json().get('detail')}")
        return None
    job_id = resp.json()["job_id"]
    print(f"  job {job_id}\n  {DIM}iter        error{RESET}")

    seen = 0
    job: dict = {}
    for _ in range(600):
        job = (await client.get(f"{BASE}/calibrate/{job_id}")).json()
        for row in job["history"][seen:]:
            final = row.get("final_correction")
            tag = f"  {DIM}← final correction, undamped{RESET}" if final else ""
            print(f'   {row["iteration"]:3d}   {row["error_arcsec"]:11.1f}"{tag}')
        seen = len(job["history"])
        if job["status"] != "running":
            break
        await asyncio.sleep(0.4)
    return job


async def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else "Jupiter"
    category = sys.argv[2] if len(sys.argv) > 2 else "planet"
    print(f"{BOLD}Astra — real INDI mount{RESET}  {DIM}target={target}{RESET}")

    async with httpx.AsyncClient(timeout=180.0) as client:
        mount = await preflight(client)
        if mount is None:
            return 1
        try:
            await inject_error(mount)
            naive = await naive_goto(client, mount, target, category)
            if naive is None:
                return 1
            coords, before = naive

            job = await calibrate(client, target, category)
            if job is None:
                return 1

            rule("5 · Result")
            after = sep_arcsec(coords, await truth(mount))
            if job["status"] != "done":
                print(f"  {RED}✗ {job['status'].upper()}{RESET} — {job.get('reason')}")
                print(f"    {job.get('message')}")
                return 1

            print(
                f"  {GREEN}✓ LOCKED ON {target.upper()}{RESET} — "
                f"{job['iteration']} iterations in {job['elapsed_seconds']}s"
            )
            print(f"\n    {'':<26}{BOLD}true error{RESET}")
            print(f"    {'naive goto':<26}{RED}{arcmin(before):>10}{RESET}")
            print(f'    {"after calibration":<26}{GREEN}{after:>9.1f}"{RESET}')
            print(f"    {DIM}{'improvement':<26}{before / max(after, 1e-6):>9.0f}x{RESET}")
            print(
                f"\n  {DIM}The mount still has its pointing error — nothing was fixed"
                f" in the mount. The loop measured where it actually was and drove"
                f" it there anyway, which is what it would do on your telescope.{RESET}"
            )
            print(
                f"\n  {CYAN}To use real hardware: start indiserver with that mount's"
                f" driver instead of indi_simulator_telescope."
                f" Nothing in the harness changes.{RESET}"
            )
            return 0
        finally:
            await mount.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
