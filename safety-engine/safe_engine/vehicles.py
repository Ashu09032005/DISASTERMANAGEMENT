"""Vehicle passability profiles.

Default thresholds are conservative engineering assumptions for *moving or
still* shallow water and should be calibrated locally. Public flood-safety
guidance (e.g. US NWS "Turn Around Don't Drown") notes that about 30 cm of
moving water can float many cars and about 60 cm can carry away SUVs and
trucks; two-wheelers become unstable in much shallower water.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class VehicleProfile:
    key: str
    label: str
    max_water_m: float      # flood intensity threshold
    max_wind_kmh: float     # storm intensity threshold
    landslide_ok: bool = False

    def threshold(self, hclass: str) -> float:
        if hclass == "flood":
            return self.max_water_m
        if hclass == "storm":
            return self.max_wind_kmh
        return 0.5  # landslide / closure intensity is 1.0 (blocked)

    def to_dict(self):
        return asdict(self)


PROFILES: dict[str, VehicleProfile] = {p.key: p for p in [
    VehicleProfile("two_wheeler", "Two-wheeler", 0.15, 50),
    VehicleProfile("car", "Car / auto-rickshaw", 0.25, 80),
    VehicleProfile("suv", "SUV / jeep", 0.40, 90),
    VehicleProfile("ambulance", "Ambulance", 0.30, 80),
    VehicleProfile("bus", "Bus", 0.50, 70),
    VehicleProfile("truck", "Truck / relief vehicle", 0.60, 70),
]}


def get_profile(key: str | None) -> VehicleProfile:
    return PROFILES.get((key or "car").lower(), PROFILES["car"])
