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
class HarnessConfig:
    simulation: SimulationConfig = field(default_factory=SimulationConfig)
    observer: ObserverConfig = field(default_factory=ObserverConfig)


cfg = HarnessConfig()
