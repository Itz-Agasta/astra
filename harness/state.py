from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field

from fastapi import WebSocket

#  Job state machine


@dataclass
class CalibrationJob:
    job_id: str
    target: str
    target_ra: float
    target_dec: float
    source: str  # "horizons" | "simbad"
    status: str = "running"  # running | done | failed | aborted
    iteration: int = 0
    current_ra: float | None = None
    current_dec: float | None = None
    solver_backend: str = "tetra3"
    solver_residual_arcsec: float | None = None  # solve error vs. truth (sim only)
    stars_matched: int = 0
    solve_time_ms: float = 0.0
    total_error_arcsec: float | None = None
    error_ra_arcsec: float | None = None
    error_dec_arcsec: float | None = None
    threshold_arcsec: float = 30.0
    reason: str | None = None  # failure reason
    message: str = "Starting calibration"
    started_at: float = field(default_factory=time.time)
    # Every iteration's error, for the dashboard's convergence graph.
    history: list[dict] = field(default_factory=list)

    @property
    def elapsed_seconds(self) -> float:
        return round(time.time() - self.started_at, 2)

    def to_dict(self) -> dict:
        return {**asdict(self), "elapsed_seconds": self.elapsed_seconds}


# System health snapshot


@dataclass
class SystemStatus:
    camera_connected: bool = False
    solver_ready: bool = False
    mount_connected: bool = False
    active_job: str | None = None
    last_known_ra: float | None = None
    last_known_dec: float | None = None
    uptime_start: float = field(default_factory=time.time)

    @property
    def uptime_seconds(self) -> int:
        return int(time.time() - self.uptime_start)


# Singleton store


class StateStore:
    def __init__(self):
        self.jobs: dict[str, CalibrationJob] = {}
        self.status: SystemStatus = SystemStatus()
        self.last_mcp_call: dict | None = None
        self._ws_clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        # Strong refs to running loop tasks. asyncio only holds weak refs, so
        # without this a calibration can be garbage collected mid-flight.
        self._tasks: dict[str, asyncio.Task] = {}

    #  Job management
    def add_job(self, job: CalibrationJob) -> None:
        self.jobs[job.job_id] = job
        self.status.active_job = job.job_id

    def get_job(self, job_id: str) -> CalibrationJob | None:
        return self.jobs.get(job_id)

    def update_job(self, job_id: str, **kwargs) -> None:
        job = self.jobs.get(job_id)
        if job:
            for k, v in kwargs.items():
                setattr(job, k, v)
            if job.current_ra is not None:
                self.status.last_known_ra = job.current_ra
                self.status.last_known_dec = job.current_dec
            if kwargs.get("status") in ("done", "failed", "aborted"):
                self.status.active_job = None
                self._tasks.pop(job_id, None)

    #  Task lifecycle
    def register_task(self, job_id: str, task: asyncio.Task) -> None:
        self._tasks[job_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(job_id, None))

    def cancel_task(self, job_id: str) -> None:
        task = self._tasks.get(job_id)
        if task and not task.done():
            task.cancel()

    #  WebSocket clients
    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._ws_clients.add(ws)
        # Send a full snapshot straight away -- a dashboard that joins mid-job
        # would otherwise show nothing until the next iteration ticks.
        await ws.send_text(json.dumps(self.snapshot()))

    def snapshot(self) -> dict:
        """Complete current state, for a newly connected dashboard."""
        active = self.jobs.get(self.status.active_job) if self.status.active_job else None
        return {
            "type": "snapshot",
            "active_job": active.to_dict() if active else None,
            "jobs": [j.to_dict() for j in self.jobs.values()],
            "system": {
                "camera_connected": self.status.camera_connected,
                "solver_ready": self.status.solver_ready,
                "mount_connected": self.status.mount_connected,
                "last_known_ra": self.status.last_known_ra,
                "last_known_dec": self.status.last_known_dec,
                "uptime_seconds": self.status.uptime_seconds,
            },
            "timestamp": _now_iso(),
        }

    def disconnect(self, ws: WebSocket) -> None:
        self._ws_clients.discard(ws)

    async def broadcast(self, payload: dict) -> None:
        """Push JSON payload to all connected dashboard clients."""
        msg = json.dumps(payload)
        dead: set[WebSocket] = set()
        for ws in self._ws_clients:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.add(ws)
        self._ws_clients -= dead

    async def broadcast_job(self, job: CalibrationJob, event: str = "calibration_update") -> None:
        payload = {
            "type": event,
            **job.to_dict(),
            "camera_connected": self.status.camera_connected,
            "solver_ready": self.status.solver_ready,
            "mount_connected": self.status.mount_connected,
            "timestamp": _now_iso(),
        }
        await self.broadcast(payload)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


store = StateStore()
