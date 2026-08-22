"""
tools/get_current_orientation.py

MCP tool: get_current_orientation
Returns the telescope's current pointing in RA/Dec and the constellation it's in.
No arguments needed — just reads live mount state from the backend.
"""

import json
from fastmcp import FastMCP
import backend_client


def register(mcp: FastMCP) -> None:
    """Attach the get_current_orientation tool to the MCP server instance."""

    @mcp.tool()
    def get_current_orientation() -> str:
        """
        Get the telescope's current pointing direction.

        Returns right ascension (RA), declination (Dec), and the constellation
        the telescope is currently aimed at.
        """
        try:
            result = backend_client.get_current_orientation()
            return json.dumps(result, indent=2)
        except RuntimeError as e:
            return f"Error: {e}"
