"""Small immutable domain types shared by the AutoCal core."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Mapping


@dataclass(frozen=True)
class XYZ:
    X: float
    Y: float
    Z: float

    @classmethod
    def from_tuple(cls, value: tuple[float, float, float]) -> "XYZ":
        return cls(*map(float, value))

    def as_tuple(self) -> tuple[float, float, float]:
        return self.X, self.Y, self.Z


@dataclass(frozen=True)
class Measurement:
    level: int
    xyz: XYZ
    x: float
    y: float
    u: float
    v: float
    read_count: int
    uv_noise: float = 0.0
    y_noise: float = 0.0
    # Fraction of full scale actually drawn (8-bit code), None = level/100.
    signal: float | None = None

    @property
    def signal_fraction(self) -> float:
        return self.level / 100.0 if self.signal is None else self.signal

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ErrorMetrics:
    total_delta_e: float
    chroma_delta_e: float
    target_y: float
    log_y_error: float


@dataclass(frozen=True)
class TintScore:
    """u'v' distance from D65 over a set of readings."""
    rms: float
    maximum: float


@dataclass(frozen=True)
class MovePlan:
    values: Mapping[str, int]
    move: tuple[int, ...]
    predicted_error: tuple[float, float]
