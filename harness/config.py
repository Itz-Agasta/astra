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
    fov_estimate_deg: float = field(
        default_factory=lambda: float(os.getenv("CCE_SOLVER_FOV_ESTIMATE", "15.0"))
    )


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
class ESP32Config:
    host: str = field(default_factory=lambda: os.getenv("CCE_ESP32_HOST", "localhost"))
    port: int = field(default_factory=lambda: int(os.getenv("CCE_ESP32_PORT", "80")))

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"


@dataclass(frozen=True)
class EngineConfig:
    simulation: SimulationConfig = field(default_factory=SimulationConfig)
    observer: ObserverConfig = field(default_factory=ObserverConfig)
    solver: SolverConfig = field(default_factory=SolverConfig)
    calibration: CalibrationConfig = field(default_factory=CalibrationConfig)
    esp32: ESP32Config = field(default_factory=ESP32Config)


cfg = EngineConfig()
