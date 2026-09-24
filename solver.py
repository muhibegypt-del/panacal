"""Pure proposal calculations. Hardware access belongs in the coordinator."""
from __future__ import annotations

import math
from collections.abc import Mapping

from domain import ControlProposal, Measurement


class UnusableResponse(RuntimeError):
    pass


def _solve_2x2(matrix: tuple[tuple[float, float], tuple[float, float]],
               rhs: tuple[float, float]) -> tuple[float, float]:
    (a, b), (c, d) = matrix
    determinant = a*d - b*c
    scale = max(abs(a), abs(b), abs(c), abs(d), 1e-12)
    if abs(determinant) < scale*scale*1e-4:
        raise UnusableResponse("Measured red/blue response matrix is ill-conditioned")
    return ((rhs[0]*d - b*rhs[1]) / determinant,
            (a*rhs[1] - rhs[0]*c) / determinant)


def propose_red_blue(current: Mapping[str, int], codes: tuple[str, str],
                     baseline: Measurement,
                     red_response_per_step: tuple[float, float],
                     blue_response_per_step: tuple[float, float],
                     target_uv: tuple[float, float], cap: int,
                     gain: float = 0.70) -> ControlProposal:
    matrix = ((red_response_per_step[0], blue_response_per_step[0]),
              (red_response_per_step[1], blue_response_per_step[1]))
    raw = _solve_2x2(matrix, (target_uv[0] - baseline.u, target_uv[1] - baseline.v))
    applied = tuple(max(-cap, min(cap, int(round(value * gain)))) for value in raw)
    values = {
        code: max(-50, min(50, int(current[code]) + move))
        for code, move in zip(codes, applied)
    }
    return ControlProposal(values=values, raw_move=raw, applied_move=applied)


def propose_gamma(current: int, baseline: Measurement, desired_y: float,
                  log_y_response_per_step: float, cap: int,
                  gain: float = 0.75) -> int:
    if abs(log_y_response_per_step) < 3e-4:
        raise UnusableResponse("Measured gamma response is too small")
    raw = math.log(max(desired_y, 1e-12) / max(baseline.xyz.Y, 1e-12)) / log_y_response_per_step
    move = max(-cap, min(cap, int(round(raw * gain))))
    return max(-50, min(50, current + move))


def white_balance_improved(before_chroma: float, after_chroma: float,
                           noise_floor: float = 0.03) -> bool:
    return before_chroma - after_chroma > max(0.0, noise_floor)


def gamma_improved(before_gamma: float, after_gamma: float,
                   before_chroma: float, after_chroma: float,
                   noise_floor: float = 0.004,
                   allowed_chroma_regression: float = 0.10) -> bool:
    return (before_gamma - after_gamma > max(0.0, noise_floor)
            and after_chroma <= before_chroma + allowed_chroma_regression)

