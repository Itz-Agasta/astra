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
    total_error_arcsec: float | None = None
    error_ra_arcsec: float | None = None
    error_dec_arcsec: float | None = None
    threshold_arcsec: float = 30.0
    reason: str | None = None  # failure reason
    message: str = "Starting calibration"
    started_at: float = field(default_factory=time.time)


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
        self._ws_clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

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
            if kwargs.get("status") in ("done", "failed", "aborted"):
                self.status.active_job = None

    #  WebSocket clients
    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._ws_clients.add(ws)

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

    async def broadcast_job(self, job: CalibrationJob) -> None:
        payload = {
            "type": "calibration_update",
            **asdict(job),
            "camera_connected": self.status.camera_connected,
            "solver_ready": self.status.solver_ready,
            "mount_connected": self.status.mount_connected,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        await self.broadcast(payload)


store = StateStore()
