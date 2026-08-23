"""
main.py — The harness HTTP API.

Two audiences:
  * the MCP server, which maps its 7 tools onto the external endpoints
  * the dashboard, which polls /status and streams /ws

Internal endpoints (/objects/resolve, /capture/raw, /motor/step) exist for
debugging and for the UI's frame preview. They are not part of the MCP surface.

Run with:  uv run uvicorn harness.main:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from contextlib import asynccontextmanager

import httpx
import numpy as np
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, Field

from . import capture, catalog, esp32, motor, mount, solver, stellarium
from .calibrator import abort_job, start_calibration
from .config import cfg, observer
from .resolver import CATEGORIES, resolve
from .state import store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger(__name__)

# Nudges bigger than this should go through /calibrate, where the closed loop
# can verify where the mount actually ended up.
MAX_NUDGE_ARCSEC = 3600.0


#  Request bodies


class CalibrateRequest(BaseModel):
    target: str = Field(..., description="Object name, e.g. 'Jupiter', 'M42', 'Betelgeuse'")
    # Claude classifies the user's phrasing into a category; "auto" tries
    # Simbad then falls back to Horizons.
    category: str = Field("auto", description=f"One of {CATEGORIES}")
    observer_lon: float | None = None
    observer_lat: float | None = None
    observer_elevation_km: float | None = None


class SlewRequest(BaseModel):
    delta_ra_arcsec: float = Field(..., ge=-MAX_NUDGE_ARCSEC, le=MAX_NUDGE_ARCSEC)
    delta_dec_arcsec: float = Field(..., ge=-MAX_NUDGE_ARCSEC, le=MAX_NUDGE_ARCSEC)


#  Lifespan


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(
        f"Astra harness starting (simulation={cfg.simulation.enabled}, mount={cfg.mount.backend})"
    )

    # Load the tetra3 database off the event loop -- it takes ~1s and we do not
    # want the first calibration to eat that.
    ready = await asyncio.get_running_loop().run_in_executor(None, solver.warmup)
    store.status.solver_ready = ready
    log.info(f"Solver ready: {ready} (backend={cfg.solver.backend})")

    mount_ok = await mount.connect()
    store.status.mount_connected = mount_ok
    if mount.is_indi():
        log.info(
            f"Mount backend: INDI at {cfg.mount.indi_url} "
            f"device='{cfg.mount.indi_device}' (connected={mount_ok})"
        )
        if not mount_ok:
            log.error(
                "Start one with:  indiserver -v indi_simulator_telescope"
                "  (or set CCE_MOUNT=stellarium)"
            )

    if cfg.simulation.enabled and await _stellarium_reachable():
        store.status.camera_connected = True
        if not mount.is_indi():
            store.status.mount_connected = True
        # Tracking makes Stellarium snap back after every slew, so kill it once
        # here rather than fighting it on each iteration.
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                await client.post(
                    f"{cfg.simulation.url}/api/stelproperty/set",
                    data={"id": "StelMovementMgr.tracking", "value": "false"},
                )
            log.info("Stellarium connected, tracking disabled")
        except Exception as exc:
            log.warning(f"Could not disable Stellarium tracking: {exc}")

        await _adopt_stellarium_location()
    else:
        log.warning(f"Stellarium not reachable at {cfg.simulation.url}")

    if cfg.simulation.enabled:
        try:
            await esp32.connect()
            log.info("ESP32 connected -- mock mount will move alongside Stellarium")
        except Exception as exc:
            log.warning(
                f"Could not connect to ESP32 on {cfg.esp32.port}: {exc}. "
                "Stellarium will still move; the physical model will not."
            )

    yield
    if cfg.simulation.enabled:
        await esp32.disconnect()
    await mount.disconnect()
    log.info("Astra harness shutting down")


app = FastAPI(title="Astra Calibration Harness", version="0.1.0", lifespan=lifespan)

# The dashboard runs on its own dev server, so it is always cross-origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # local-only tool; tighten if this is ever exposed
    allow_methods=["*"],
    allow_headers=["*"],
)


#  External — MCP surface


@app.post("/calibrate")
async def post_calibrate(req: CalibrateRequest) -> dict:
    """point_to() — resolve the target and kick off the loop. Returns immediately."""
    if store.status.active_job:
        raise HTTPException(
            409,
            f"Job {store.status.active_job} is already running. "
            "Abort it first or wait for it to finish.",
        )
    # Checked up front so a bad category reads as a validation error rather
    # than being reported as an unresolvable target.
    if req.category not in CATEGORIES:
        raise HTTPException(422, f"Invalid category '{req.category}'. Must be one of {CATEGORIES}")
    try:
        ra, dec, source = await resolve(
            req.target,
            req.category,
            req.observer_lon,
            req.observer_lat,
            req.observer_elevation_km,
        )
    except ValueError as exc:
        raise HTTPException(404, f"Could not resolve '{req.target}': {exc}") from exc

    job_id = await start_calibration(req.target, ra, dec, source)
    job = store.get_job(job_id)
    if job is None:  # only reachable if the job was evicted between the two calls
        raise HTTPException(500, f"Job {job_id} vanished immediately after creation")
    return {
        "job_id": job_id,
        "target": req.target,
        "target_ra": round(ra, 6),
        "target_dec": round(dec, 6),
        "source": source,
        "status": job.status,
        "message": job.message,
        "poll_hint": "GET /calibrate/{job_id} every 2-3s until status != 'running'",
    }


@app.get("/calibrate/{job_id}")
async def get_calibrate(job_id: str) -> dict:
    """get_calibration_status() — poll a job's progress."""
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(404, f"No such job: {job_id}")
    return job.to_dict()


@app.delete("/calibrate/{job_id}")
async def delete_calibrate(job_id: str) -> dict:
    """abort_calibration() — stop a running loop."""
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(404, f"No such job: {job_id}")
    aborted = await abort_job(job_id)
    return {
        "job_id": job_id,
        "status": job.status,
        "aborted": aborted,
        "message": job.message if aborted else f"Job was already {job.status}",
    }


@app.post("/capture/solve")
async def post_capture_solve() -> dict:
    """get_current_orientation() — capture and plate solve right now."""
    try:
        image, hint = await capture.capture_frame()
        result = await asyncio.get_running_loop().run_in_executor(
            None, lambda: solver.solve(image, hint=hint)
        )
    except Exception as exc:
        raise HTTPException(503, f"Capture/solve failed: {exc}") from exc

    store.status.camera_connected = True
    store.status.solver_ready = True
    store.status.last_known_ra = round(result.ra, 6)
    store.status.last_known_dec = round(result.dec, 6)
    return {
        "ra": round(result.ra, 6),
        "dec": round(result.dec, 6),
        "roll": round(result.roll, 4),
        "fov": round(result.fov, 4),
        "stars_matched": result.stars_matched,
        "confidence": round(result.confidence, 6),
        "rmse_arcsec": result.rmse_arcsec,
        "residual_arcsec": result.residual_arcsec,
        "solve_time_ms": result.solve_time_ms,
        "backend": result.backend,
    }


@app.get("/objects/visible")
async def get_visible(
    category: str = Query("all", description="planet | star | deep_sky | all"),
    min_altitude_deg: float = Query(10.0, ge=-90.0, le=90.0),
) -> dict:
    """list_visible_objects() — what is above the horizon right now."""
    try:
        objects, observed_at, lat, lon = await asyncio.get_running_loop().run_in_executor(
            None, lambda: catalog.list_visible(category, min_altitude_deg)
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "objects": objects,
        "count": len(objects),
        "category": category,
        "min_altitude_deg": min_altitude_deg,
        "observed_at": observed_at,
        "observer_lat": lat,
        "observer_lon": lon,
    }


@app.post("/slew")
async def post_slew(req: SlewRequest) -> dict:
    """manual_slew() — nudge the mount by a coordinate offset in arcseconds."""
    if store.status.active_job:
        raise HTTPException(409, f"Cannot nudge while job {store.status.active_job} is running.")
    try:
        ra, dec, prev_ra, prev_dec = await motor.nudge(req.delta_ra_arcsec, req.delta_dec_arcsec)
    except NotImplementedError as exc:
        raise HTTPException(501, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(503, f"Slew failed: {exc}") from exc

    store.status.last_known_ra = round(ra, 6)
    store.status.last_known_dec = round(dec, 6)
    return {
        "ra": round(ra, 6),
        "dec": round(dec, 6),
        "previous_ra": round(prev_ra, 6),
        "previous_dec": round(prev_dec, 6),
        "delta_ra_arcsec": req.delta_ra_arcsec,
        "delta_dec_arcsec": req.delta_dec_arcsec,
        "message": f"Nudged to RA={ra:.4f} Dec={dec:.4f}",
    }


#  MCP activity feed (populated by astra-mcp, rendered by the Stellarium HUD)


@app.post("/mcp/activity")
async def post_mcp_activity(call: dict) -> dict:
    """Record the latest MCP tool call for on-screen display."""
    raw = call.get("fields") or []
    fields: list[list[str]] = []
    if isinstance(raw, list):
        for item in raw[:5]:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                fields.append([str(item[0])[:24], str(item[1])[:40]])
    store.last_mcp_call = {
        "tool": str(call.get("tool", "unknown")),
        "fields": fields,
        "ts": time.time(),
    }
    return {"ok": True}


@app.get("/mcp/activity")
async def get_mcp_activity() -> dict:
    """Latest MCP tool call (or null)."""
    return store.last_mcp_call or {"tool": None}


@app.get("/status")
async def get_status() -> dict:
    """get_telescope_status() — system health."""
    reachable = await _stellarium_reachable() if cfg.simulation.enabled else False
    if reachable:
        store.status.camera_connected = True
        if not mount.is_indi():
            store.status.mount_connected = True
    return {
        "simulation": cfg.simulation.enabled,
        "mount_backend": cfg.mount.backend,
        "camera_connected": store.status.camera_connected,
        "solver_ready": store.status.solver_ready,
        "solver_backend": cfg.solver.backend,
        "mount_connected": store.status.mount_connected,
        "stellarium_reachable": reachable,
        "active_job": store.status.active_job,
        "last_known_ra": store.status.last_known_ra,
        "last_known_dec": store.status.last_known_dec,
        "uptime_seconds": store.status.uptime_seconds,
        "total_jobs": len(store.jobs),
        "converge_threshold_arcsec": cfg.calibration.converge_threshold_arcsec,
    }


@app.get("/observer")
async def get_observer() -> dict:
    """get_observer_location() — where the harness thinks it is."""
    return {
        "latitude": observer.latitude,
        "longitude": observer.longitude,
        "elevation_km": observer.elevation_km,
        "source": observer.source,
    }


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    """Dashboard stream. Sends a snapshot on connect, then every job update."""
    await store.connect(ws)
    try:
        while True:
            # Nothing is expected from the client; this just parks the
            # coroutine and notices the disconnect.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.warning(f"WebSocket error: {exc}")
    finally:
        store.disconnect(ws)


#  Internal — debugging and the UI frame preview, not the MCP surface


@app.get("/objects/resolve")
async def get_resolve(name: str, category: str = "auto") -> dict:
    """Resolve a name to RA/Dec without starting a calibration."""
    if category not in CATEGORIES:
        raise HTTPException(422, f"Invalid category '{category}'. Must be one of {CATEGORIES}")
    try:
        ra, dec, source = await resolve(name, category)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {
        "target": name,
        "ra": round(ra, 6),
        "dec": round(dec, 6),
        "source": source,
        "category": category,
    }


@app.post("/capture/raw")
async def post_capture_raw() -> Response:
    """The current frame as a PNG — what the solver actually sees."""
    from PIL import Image

    if cfg.solver.backend == "hint":
        # Light mode never renders a frame, so there is nothing real to return.
        # Better an explicit 503 than a 1x1 placeholder the dashboard would
        # display as a black square and treat as a genuine capture.
        raise HTTPException(
            503,
            "No frame available: solver backend is 'hint', which skips rendering. "
            "Restart without CCE_SOLVER_BACKEND=hint to enable the frame preview.",
        )

    try:
        image, _ = await capture.capture_frame()
    except Exception as exc:
        raise HTTPException(503, f"Capture failed: {exc}") from exc

    buf = io.BytesIO()
    Image.fromarray(_display_stretch(image), mode="L").save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


def _display_stretch(image) -> np.ndarray:
    """Stretch a frame for human eyes: background to black, stars bright.

    Deliberately not the solver's stretch -- that one spans the 1st-99th
    percentile, which is right for centroiding but renders as a grey field of
    noise. Clipping at the median instead puts the sky background at black.
    """
    import numpy as np

    img = image.astype(np.float32)
    lo = float(np.percentile(img, 50))  # background median -> black
    hi = float(np.percentile(img, 99.99))  # star cores -> white
    if hi <= lo:
        return np.zeros(img.shape, dtype=np.uint8)
    return np.clip((img - lo) / (hi - lo) * 255, 0, 255).astype(np.uint8)


@app.post("/motor/step")
async def post_motor_step(ra_deg: float, dec_deg: float) -> dict:
    """Absolute slew. Bypasses the loop — for bring-up and debugging only."""
    try:
        await motor.slew(ra_deg, dec_deg)
    except Exception as exc:
        raise HTTPException(503, f"Slew failed: {exc}") from exc
    return {"ra": ra_deg, "dec": dec_deg, "message": "Slew complete"}


#  Helpers


async def _adopt_stellarium_location() -> None:
    """Match the harness observer to Stellarium's, so the two agree.

    Skipped when CCE_OBSERVER_* was set -- an explicit choice always wins.
    """
    if cfg.observer.explicitly_set:
        log.info(
            "Observer pinned by CCE_OBSERVER_*: "
            f"lat={observer.latitude:.4f}, lon={observer.longitude:.4f}"
        )
        return
    try:
        lat, lon, elev_km = await stellarium.get_location()
    except Exception as exc:
        log.warning(f"Could not read Stellarium location, keeping config: {exc}")
        return

    moved_km = _rough_distance_km(observer.latitude, observer.longitude, lat, lon)
    observer.adopt(lat, lon, elev_km, source="stellarium")
    log.info(
        f"Observer location adopted from Stellarium: "
        f"lat={lat:.4f}, lon={lon:.4f}, elev_km={elev_km:.3f}km"
    )
    if moved_km > 50:
        log.warning(
            f"Stellarium's location is {moved_km:.0f} km from the configured default. "
            "Using Stellarium's so /objects/visible matches the on-screen sky. "
            "Set CCE_OBSERVER_LAT/LON to override."
        )


def _rough_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance, good enough to decide whether to warn."""
    import math

    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(a)))


async def _stellarium_reachable() -> bool:
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{cfg.simulation.url}/api/main/status")
            return resp.status_code == 200
    except Exception:
        return False


def main() -> None:
    import uvicorn

    uvicorn.run("harness.main:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    main()
