"""
target_classifier.py

Classification (solar system vs. deep sky) is handled by Claude itself at
the tool-call level, guided by the point_to tool's docstring. This module
just defines the two possible resolver types for reference/typing.
"""

from enum import Enum


class TargetType(Enum):
    SOLAR_SYSTEM = "solar_system"  # → JPL Horizons
    DEEP_SKY = "deep_sky"  # → Simbad
