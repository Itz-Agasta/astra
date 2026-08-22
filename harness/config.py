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
class EngineConfig:
    simulation: SimulationConfig = field(default_factory=SimulationConfig)


cfg = EngineConfig()
