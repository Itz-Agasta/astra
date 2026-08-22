"""
tools/get_calibration_status.py

MCP tool: get_calibration_status

Called ONCE at the start of a calibration job. Polls the backend every 1.5
seconds and streams a timestamped log line per tick until the job finishes
or 5 minutes elapse. The frontend can consume these logs line by line.
"""

import time

import backend_client
from fastmcp import FastMCP

POLL_INTERVAL = 1.5
MAX_WATCH_SECONDS = 300  # 5 minutes


def register(mcp: FastMCP) -> None:
    """Attach get_calibration_status to the MCP server instance."""

    @mcp.tool()
    def get_calibration_status(job_id: str) -> str:
        """
        Watch a calibration job from start to finish, logging every 1-2 seconds.

        Call this ONCE after a calibration job starts. It polls the backend
        every 1.5 seconds and produces a timestamped log line per tick until
        the job reaches a terminal state (completed, aborted, failed) or
        5 minutes elapse. No need to call it again mid-calibration.

        Args:
            job_id: The job ID returned by point_to or a calibration start call.
        """
        if not job_id.strip():
            return "Error: job_id cannot be empty."

        job_id = job_id.strip()
        logs: list[str] = []
        elapsed = 0.0

        logs.append(f"[watch] Calibration watch started for job {job_id}")
        logs.append(f"[watch] Polling every {POLL_INTERVAL}s  |  timeout: {MAX_WATCH_SECONDS}s\n")

        while elapsed < MAX_WATCH_SECONDS:
            try:
                result = backend_client.get_calibration_status(job_id)
            except RuntimeError as e:
                logs.append(f"[error] {e}")
                break

            iteration = result.get("iteration", "?")
            # The harness reports the pointing error as total_error_arcsec
            # (RA/Dec components in error_ra_arcsec / error_dec_arcsec).
            error_arcsec = result.get("total_error_arcsec", result.get("error_arcsec", "?"))
            status = result.get("status", "unknown")
            message = result.get("message", "")

            timestamp = time.strftime("%H:%M:%S")
            line = f'[{timestamp}]  iteration={iteration}  error={error_arcsec}"  status={status}'
            if message:
                line += f"  |  {message}"
            logs.append(line)

            if status in ("done", "failed", "aborted"):
                logs.append(f"\n[watch] Job {job_id} ended with status: {status}")
                break

            time.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL
        else:
            logs.append(f"\n[watch] Timeout after {MAX_WATCH_SECONDS}s — job may still be running.")

        return "\n".join(logs)
