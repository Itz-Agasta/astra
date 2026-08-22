"""
tools/abort_calibration.py

MCP tool: abort_calibration
Sends an abort signal for a running calibration or alignment job.
Useful when calibration is taking too long or the user wants to stop it.
"""

import json
from fastmcp import FastMCP
import backend_client


def register(mcp: FastMCP) -> None:
    """Attach the abort_calibration tool to the MCP server instance."""

    @mcp.tool()
    def abort_calibration(job_id: str) -> str:
        """
        Abort a running calibration or alignment job.

        Sends a stop signal to the backend. The mount will halt its current
        operation. Use get_calibration_status afterward to confirm the abort.

        Args:
            job_id: The job ID of the calibration to abort.
        """
        if not job_id.strip():
            return "Error: job_id cannot be empty."

        try:
            result = backend_client.abort_calibration(job_id.strip())
            return json.dumps(result, indent=2)
        except RuntimeError as e:
            return f"Error: {e}"
