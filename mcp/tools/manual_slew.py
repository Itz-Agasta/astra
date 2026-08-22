"""
tools/manual_slew.py

MCP tool: manual_slew
Sends a relative offset to the mount in arcseconds.
This tool owns the safety check (max 3600 arcsec per axis) so the backend
doesn't need to worry about runaway slew commands from Claude.
"""

import json

import backend_client
from fastmcp import FastMCP

# Maximum allowed offset per axis. Beyond this the mount could be damaged
# or the target lost entirely — the backend trusts us to enforce this.
MAX_SLEW_ARCSEC = 3600.0


def register(mcp: FastMCP) -> None:
    """Attach the manual_slew tool to the MCP server instance."""

    @mcp.tool()
    def manual_slew(dra_arcsec: float, ddec_arcsec: float) -> str:
        """
        Slew the telescope by a relative offset in arcseconds.

        Moves the mount by the given delta in right ascension and declination.
        Both values must be within ±3600 arcseconds (one degree) to prevent
        large accidental slews.

        Args:
            dra_arcsec:  RA offset in arcseconds  (positive = east,  negative = west).
            ddec_arcsec: Dec offset in arcseconds (positive = north, negative = south).
        """
        # Safety gate: reject anything larger than one degree per axis
        if abs(dra_arcsec) > MAX_SLEW_ARCSEC:
            return (
                f"Error: dra_arcsec ({dra_arcsec}) exceeds the maximum allowed "
                f"value of ±{MAX_SLEW_ARCSEC} arcseconds."
            )
        if abs(ddec_arcsec) > MAX_SLEW_ARCSEC:
            return (
                f"Error: ddec_arcsec ({ddec_arcsec}) exceeds the maximum allowed "
                f"value of ±{MAX_SLEW_ARCSEC} arcseconds."
            )

        try:
            result = backend_client.manual_slew(dra_arcsec, ddec_arcsec)
            return json.dumps(result, indent=2)
        except RuntimeError as e:
            return f"Error: {e}"
