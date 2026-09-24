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

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ErrorMetrics:
    total_delta_e: float
    chroma_delta_e: float
    target_y: float
    log_y_error: float


@dataclass(frozen=True)
class ControlProposal:
    values: Mapping[str, int]
    raw_move: tuple[float, ...]
    applied_move: tuple[int, ...]


@dataclass(frozen=True)
class Evaluation:
    chroma_score: float
    gamma_score: float
    maximum_chroma_delta_e: float

