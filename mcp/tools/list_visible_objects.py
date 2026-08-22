"""
tools/list_visible_objects.py

MCP tool: list_visible_objects
Asks the backend for celestial objects currently above the horizon.
The backend handles all the astronomy (alt/az calculation, magnitude filtering)
using the telescope's configured observer location.
"""

import json
from fastmcp import FastMCP
import backend_client


def register(mcp: FastMCP) -> None:
    """Attach the list_visible_objects tool to the MCP server instance."""

    @mcp.tool()
    def list_visible_objects() -> str:
        """
        List celestial objects currently visible from the telescope's location.

        Returns a list of objects with their altitude, azimuth, and magnitude.
        The backend computes visibility based on the observer's coordinates and
        current time.
        """
        try:
            result = backend_client.list_visible_objects()
            return json.dumps(result, indent=2)
        except RuntimeError as e:
            return f"Error: {e}"
