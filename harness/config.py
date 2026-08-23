from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class SimulationConfig:
    enabled: bool = field(
        default_factory=lambda: os.getenv("CCE_SIMULATION", "true").lower() in ("1", "true", "yes")
    )
    stellarium_host: str = field(
        default_factory=lambda: os.getenv("CCE_STELLARIUM_HOST", "localhost")
    )
    stellarium_port: int = field(
        default_factory=lambda: int(os.getenv("CCE_STELLARIUM_PORT", "8090"))
    )

    @property
    def url(self) -> str:
        return f"http://{self.stellarium_host}:{self.stellarium_port}"


_OBSERVER_ENV_VARS = ("CCE_OBSERVER_LON", "CCE_OBSERVER_LAT", "CCE_OBSERVER_ELEVATION_KM")


@dataclass(frozen=True)
class ObserverConfig:
    # Override via env vars for testing. In production, Agent sends these.
    longitude: float = field(
        default_factory=lambda: float(os.getenv("CCE_OBSERVER_LON", "85.679443"))
    )
    latitude: float = field(
        default_factory=lambda: float(os.getenv("CCE_OBSERVER_LAT", "25.924018"))
    )
    elevation_km: float = field(
        default_factory=lambda: float(os.getenv("CCE_OBSERVER_ELEVATION_KM", "0.093"))
    )
    # True when the operator named a location explicitly. Simulation mode
    # adopts Stellarium's location on startup, but must never override a
    # choice someone made on purpose.
    explicitly_set: bool = field(
        default_factory=lambda: any(os.getenv(v) is not None for v in _OBSERVER_ENV_VARS)
    )

    @property
    def location_dict(self) -> dict[str, float]:
        """Format for JPL Horizons API."""
        return {
            "lon": self.longitude,
            "lat": self.latitude,
            "elevation": self.elevation_km,
        }


@dataclass(frozen=True)
class SolverConfig:
    # Must stay inside tetra3's bundled default_database range (10-30 deg).
    # 12 measured best across the sky: 8/8 solves, ~15" median error, which
    # leaves ~2x margin under the 30" convergence threshold. Wider fields give
    # a coarser pixel scale and land too close to the threshold to converge.
    fov_estimate_deg: float = field(
        default_factory=lambda: float(os.getenv("CCE_SOLVER_FOV_ESTIMATE", "12.0"))
    )
    fov_max_error_deg: float = field(
        default_factory=lambda: float(os.getenv("CCE_SOLVER_FOV_MAX_ERROR", "1.0"))
    )
    # Rendered frame size in px. Solve accuracy is ~0.7 x pixel scale, so this
    # directly sets the loop's noise floor: 2048px @ 12 deg -> 21"/px -> ~15".
    # Drop to 1024 for ~4x faster solves, but the error doubles to ~30" and the
    # loop will stall at the threshold -- raise CCE_CONVERGE_ARCSEC if you do.
    image_size_px: int = field(default_factory=lambda: int(os.getenv("CCE_IMAGE_SIZE", "2048")))
    # "tetra3" does a genuine plate solve of the rendered frame.
    # "hint" trusts the Stellarium view coords directly -- a zero-noise escape
    # hatch if the solver misbehaves mid-demo.
    backend: str = field(default_factory=lambda: os.getenv("CCE_SOLVER_BACKEND", "tetra3").lower())


@dataclass(frozen=True)
class CalibrationConfig:
    # How long to wait for mechanical/electronic settle after a slew.
    # 1s is safe for Stellarium + esp32 sim . we need to tune it.
    settle_time_s: float = field(default_factory=lambda: float(os.getenv("CCE_SETTLE_TIME_S", "1")))
    # Iteration cap to prevent infinite loops
    max_iterations: int = field(default_factory=lambda: int(os.getenv("CCE_MAX_ITERATIONS", "30")))
    # Error threshold in arcseconds -- convergence target
    converge_threshold_arcsec: float = field(
        default_factory=lambda: float(os.getenv("CCE_CONVERGE_ARCSEC", "30.0"))
    )
    # Damping factor (0.8 avoids oscillation) -- we need to tune it too
    damping: float = field(default_factory=lambda: float(os.getenv("CCE_DAMPING", "0.8")))


@dataclass(frozen=True)
class MountConfig:
    """Which thing actually moves, and how we talk to it.

    Kept apart from SimulationConfig on purpose: a real INDI mount driving a
    simulated camera is the normal state of affairs during hardware bring-up,
    and it is the only configuration that can be demonstrated on a laptop.
    """

    # "stellarium" moves the simulated sky. "indi" drives a real mount driver
    # over the wire -- the simulator and an observatory mount are the same
    # path, differing only in which driver indiserver loaded.
    backend: str = field(default_factory=lambda: os.getenv("CCE_MOUNT", "stellarium").lower())
    indi_host: str = field(default_factory=lambda: os.getenv("CCE_INDI_HOST", "localhost"))
    indi_port: int = field(default_factory=lambda: int(os.getenv("CCE_INDI_PORT", "7624")))
    indi_device: str = field(
        default_factory=lambda: os.getenv("CCE_INDI_DEVICE", "Telescope Simulator")
    )
    # With an INDI mount, Stellarium stops being the telescope and becomes the
    # display: it follows the mount so there is still a sky to look at.
    mirror_to_stellarium: bool = field(
        default_factory=lambda: (
            os.getenv("CCE_MIRROR_STELLARIUM", "true").lower() in ("1", "true", "yes")
        )
    )

    @property
    def indi_url(self) -> str:
        return f"{self.indi_host}:{self.indi_port}"


@dataclass(frozen=True)
class ESP32Config:
    # ESP32 is wired via USB, not WiFi -- this is a serial device path, not a host.
    port: str = field(default_factory=lambda: os.getenv("CCE_ESP32_PORT", "/dev/ttyUSB0"))
    baud_rate: int = field(default_factory=lambda: int(os.getenv("CCE_ESP32_BAUD", "115200")))
    timeout_s: float = field(default_factory=lambda: float(os.getenv("CCE_ESP32_TIMEOUT_S", "5.0")))
    reconnect_delay_s: float = field(
        default_factory=lambda: float(os.getenv("CCE_ESP32_RECONNECT_DELAY_S", "2.0"))
    )

    # --- Mock-mount servo calibration (approximate is fine -- eyeball these) ---
    az_offset_deg: float = field(
        default_factory=lambda: float(os.getenv("CCE_ESP32_AZ_OFFSET_DEG", "0.0"))
    )
    alt_offset_deg: float = field(
        default_factory=lambda: float(os.getenv("CCE_ESP32_ALT_OFFSET_DEG", "0.0"))
    )
    invert_az: bool = field(
        default_factory=lambda: (
            os.getenv("CCE_ESP32_INVERT_AZ", "false").lower() in ("1", "true", "yes")
        )
    )
    invert_alt: bool = field(
        default_factory=lambda: (
            os.getenv("CCE_ESP32_INVERT_ALT", "false").lower() in ("1", "true", "yes")
        )
    )


@dataclass(frozen=True)
class HarnessConfig:
    simulation: SimulationConfig = field(default_factory=SimulationConfig)
    observer: ObserverConfig = field(default_factory=ObserverConfig)
    solver: SolverConfig = field(default_factory=SolverConfig)
    mount: MountConfig = field(default_factory=MountConfig)
    calibration: CalibrationConfig = field(default_factory=CalibrationConfig)
    esp32: ESP32Config = field(default_factory=ESP32Config)


cfg = HarnessConfig()


class RuntimeObserver:
    """The observer location actually in use, which can change at startup.

    Stellarium keeps its own location (often IP-geolocated, and often wrong).
    If the harness and Stellarium disagree, `/objects/visible` reports
    altitudes that contradict the sky on screen, and Moon parallax throws the
    target off by several arcminutes -- 566" for a 1000 km disagreement,
    against a 30" convergence threshold. Planets and stars are unaffected
    (<1"), so this only bites the Moon and the visible-object listing.

    Simulation mode therefore adopts Stellarium's location at startup unless
    CCE_OBSERVER_* was set explicitly. Real hardware always uses config, or
    whatever coordinates the agent passes per request.
    """

    def __init__(self, base: ObserverConfig) -> None:
        self.latitude = base.latitude
        self.longitude = base.longitude
        self.elevation_km = base.elevation_km
        self.source = "config"

    def adopt(self, latitude: float, longitude: float, elevation_km: float, source: str) -> None:
        self.latitude = latitude
        self.longitude = longitude
        self.elevation_km = elevation_km
        self.source = source

    @property
    def location_dict(self) -> dict[str, float]:
        """Format for JPL Horizons API."""
        return {"lon": self.longitude, "lat": self.latitude, "elevation": self.elevation_km}


observer = RuntimeObserver(cfg.observer)
