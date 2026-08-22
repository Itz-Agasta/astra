"""
tools/point_to.py

MCP tool: point_to

Claude (the client running in Claude Desktop / Cursor) reads this tool's
docstring before calling it. The docstring carries all the instructions
needed for Claude to classify the target and populate resolver + input_body
correctly.

LOCATION: Observer location is resolved automatically via
OpenStreetMap (see location_resolver.py):
    1. An explicit input_body["location"] (lon/lat), if the caller passes one.
    2. A free-text 'location_query' (e.g. "Srirampur, West Bengal, India"),
       geocoded through Nominatim + elevation via OpenTopoData.
    3. If neither is provided, the tool returns an error — location is mandatory.
"""

import json

import backend_client
import location_resolver
from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    """Attach the point_to tool to the MCP server instance."""

    @mcp.tool()
    def point_to(
        target: str,
        resolver: str,
        input_body: dict,
        location_query: str | None = None,
    ) -> str:
        """
        Point the telescope at a named celestial target.

        Before calling this tool, YOU (Claude) must:
          1. Classify the target (see CLASSIFICATION RULES below)
          2. Build input_body in the correct format (see INPUT_BODY FORMAT below)

        OBSERVER LOCATION — HANDLED AUTOMATICALLY
        ───────────────────────────────────────────
        You do NOT need to ask the user for their location. In this order:
          1. If input_body["location"] already has lon/lat, that is used as-is.
          2. Else, if you pass 'location_query' (a place name, e.g. "Kolkata,
             India"), it is geocoded automatically (Nominatim + OpenTopoData).
          3. If neither is provided, the harness uses its own configured
             site (in simulation mode: Stellarium's location). This is the
             normal case — omit location entirely.

        Only pass 'location_query' if the user explicitly mentions observing
        from somewhere other than the telescope's usual site.

        CLASSIFICATION RULES
        ─────────────────────
        Solar system bodies → resolver = "horizons"
          Includes: planets (Mercury, Venus, Mars, Jupiter, Saturn, Uranus, Neptune),
          dwarf planets (Pluto, Ceres, Eris…), moons (Moon, Titan, Europa…),
          comets (Halley, 67P, Hale-Bopp…), asteroids (Apophis, Vesta…), the Sun.

        Everything else → resolver = "simbad"
          Includes: nebulae, galaxies, star clusters, individual stars (other than
          the Sun), and any object outside our solar system.
          Examples: Orion Nebula, M31, Andromeda Galaxy, Pleiades, Betelgeuse, NGC 224.

        INPUT_BODY FORMAT
        ──────────────────
        For resolver = "horizons" (solar system bodies):
          {
            "id": "<target name or JPL numeric ID>",
            "epochs": "Time.now().jd",
            "id_type": "majorbody"
          }
          Use id_type "smallbody" for asteroids and comets.
          Omit "location" entirely unless you have explicit lon/lat/elevation.

        For resolver = "simbad" (deep sky objects):
          {
            "name": "<target name>"
          }
          Simbad does not need a location — observer position is irrelevant
          for fixed deep sky coordinates.

        EXAMPLES
        ─────────
        Default site (most common case):
          "Jupiter" → resolver="horizons",
                      input_body={"id":"Jupiter","epochs":"Time.now().jd","id_type":"majorbody"}

        User observing from elsewhere:
          "Jupiter", location_query="London, UK"
            → resolver="horizons",
              input_body={"id":"Jupiter","epochs":"Time.now().jd","id_type":"majorbody"}
            → location auto-resolved from "London, UK" via OpenStreetMap

        Any location (deep sky):
          "Orion Nebula" → resolver="simbad", input_body={"name":"Orion Nebula"}
          "M31"          → resolver="simbad", input_body={"name":"M31"}

        Args:
            target:         Human-readable name of the object (used for job metadata).
            resolver:       "horizons" for solar system bodies, "simbad" for deep sky objects.
            input_body:     Pre-formatted dict for the chosen resolver (see formats above).
            location_query: Optional place name (e.g. "Kolkata, India"). Only needed
                             when observing somewhere other than the default site.
                             Auto-geocoded via OpenStreetMap — never ask the user to
                             type raw coordinates.

        AFTER CALLING THIS TOOL
        ────────────────────────
        If the response contains a job_id, YOU (Claude) must IMMEDIATELY call
        get_calibration_status with that job_id — without waiting for the user
        to ask. This starts the live calibration log automatically.
        """
        if not target.strip():
            return "Error: target name cannot be empty."

        if resolver not in ("horizons", "simbad"):
            return f"Error: resolver must be 'horizons' or 'simbad', got '{resolver}'."

        if not isinstance(input_body, dict) or not input_body:
            return "Error: input_body must be a non-empty dict."

        input_body = dict(input_body)  # avoid mutating caller's dict

        # Location is optional: with no query and no explicit coordinates the
        # harness falls back to its own configured site (Stellarium's in
        # simulation mode). Resolve only when the caller gave us something.
        has_explicit_location = (
            isinstance(input_body.get("location"), dict)
            and "lon" in input_body["location"]
            and "lat" in input_body["location"]
        )
        if resolver == "horizons" and (location_query or has_explicit_location):
            try:
                location = location_resolver.resolve_location(
                    location_query=location_query,
                    explicit_location=input_body.get("location"),
                )
            except ValueError as e:
                return f"Error: {e}"

            input_body["location"] = {
                "lon": location["lon"],
                "lat": location["lat"],
                "elevation": location.get("elevation", 0.0),
            }

        if resolver == "horizons" and "id_type" not in input_body:
            input_body["id_type"] = "majorbody"

        if resolver == "simbad" and "name" not in input_body:
            return "Error: input_body must contain 'name' for resolver='simbad'."

        try:
            result = backend_client.point_to(
                target=target.strip(),
                resolver=resolver,
                input_body=input_body,
            )
            return json.dumps(result, indent=2)
        except RuntimeError as e:
            return f"Error: {e}"
