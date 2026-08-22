"""
backend_client.py

Single place for all HTTP communication with the Astra backend.
Tools call methods here instead of using httpx directly — this makes
swapping out the backend URL or adding headers a one-line change.
"""

import os
import httpx
from dotenv import load_dotenv

load_dotenv()

# Read backend URL once at import time; fail loudly if missing
BACKEND_BASE_URL = os.getenv("BACKEND_BASE_URL").rstrip("/")

# Shared timeout so every request has a sensible limit
_TIMEOUT = httpx.Timeout(10.0)


def _client() -> httpx.Client:
    """Return a configured httpx client. Called per-request to stay simple."""
    return httpx.Client(base_url=BACKEND_BASE_URL, timeout=_TIMEOUT)


def _handle_response(response: httpx.Response) -> dict | list:
    """
    Raise a readable error for non-2xx responses.
    Returns parsed JSON on success.
    """
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise RuntimeError(
            f"Backend returned {e.response.status_code}: {e.response.text}"
        ) from e
    return response.json()


# ── Tool-specific methods ──────────────────────────────────────────────────

def point_to(target: str, resolver: str, input_body: dict) -> dict:
    """
    Tell the backend to start pointing the telescope at a named target.

    The tool layer classifies the target and builds input_body before calling
    this — we just forward the ready-made payload so the backend knows exactly
    which resolver to use and what to pass to it.

    Args:
        target:     Human-readable name (for logging / job metadata).
        resolver:   "horizons" for solar system bodies, "simbad" for deep sky.
        input_body: Pre-formatted dict for the chosen resolver.
                    Horizons -> {id, location, epochs, id_type}
                    Simbad   -> {name}
    """
    payload = {
        "target": target,
        "resolver": resolver,
        "input_body": input_body,
    }
    with _client() as client:
        try:
            resp = client.post("/point", json=payload)
        except httpx.ConnectError:
            raise RuntimeError(f"Cannot reach backend at {BACKEND_BASE_URL}")
        except httpx.TimeoutException:
            raise RuntimeError("Backend request timed out")
    return _handle_response(resp)