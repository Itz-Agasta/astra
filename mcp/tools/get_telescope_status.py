"""
tools/get_telescope_status.py

MCP tool: get_telescope_status
Returns the health of every subsystem: camera, plate solver, mount, and
whether a job is currently running. Good first call to confirm the system
is ready before issuing other commands.
"""

import json
from fastmcp import FastMCP
import backend_client


def register(mcp: FastMCP) -> None:
    """Attach the get_telescope_status tool to the MCP server instance."""

    @mcp.tool()
    def get_telescope_status() -> str:
        """
        Get the current status of all telescope subsystems.

        Returns the state of the camera, plate solver, mount, and any
        active job. Use this to verify the system is ready before
        issuing slew or calibration commands.
        """
        try:
            result = backend_client.get_telescope_status()
            return json.dumps(result, indent=2)
        except RuntimeError as e:
            return f"Error: {e}"
