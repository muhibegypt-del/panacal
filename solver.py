"""Pure proposal calculations. Hardware access belongs in the coordinator.

The response model is a 2x2 matrix: column i is the u'v' change produced by
one step of control i. It is learned from probes, inherited from a
neighbouring point, and refined after every measured move.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from domain import MovePlan

Column = tuple[float, float]
Model = tuple[Column, Column]


def predict_error(error: Column, model: Model, move: Sequence[int]) -> Column:
    return (error[0] + model[0][0] * move[0] + model[1][0] * move[1],
            error[1] + model[0][1] * move[0] + model[1][1] * move[1])


def best_integer_move(current: Mapping[str, int], codes: tuple[str, str],
                      error: Column, model: Model, cap: int,
                      limits: tuple[int, int] = (-50, 50)) -> MovePlan:
    """Try every whole-step move within `cap` and keep the one whose
    predicted u'v' error is smallest; ties go to the smaller move."""
    low, high = limits
    best = None
    for first in range(-cap, cap + 1):
        for second in range(-cap, cap + 1):
            values = (int(current[codes[0]]) + first, int(current[codes[1]]) + second)
            if not all(low <= value <= high for value in values):
                continue
            predicted = predict_error(error, model, (first, second))
            key = (round(math.hypot(*predicted), 9), abs(first) + abs(second))
            if best is None or key < best[0]:
                best = (key, (first, second), predicted, values)
    _, move, predicted, values = best
    return MovePlan(values=dict(zip(codes, values)), move=move, predicted_error=predicted)


def update_model(model: Model, move: Sequence[int], observed_change: Column,
                 weight: float = 0.5) -> Model:
    """Damped Broyden update: correct the model along the direction moved so
    it reproduces the observed u'v' change, blended by `weight`."""
    norm = move[0] * move[0] + move[1] * move[1]
    if norm == 0:
        return model
    predicted = predict_error((0.0, 0.0), model, move)
    residual = (observed_change[0] - predicted[0], observed_change[1] - predicted[1])
    scale = weight / norm
    return tuple(
        (model[i][0] + scale * residual[0] * move[i],
         model[i][1] + scale * residual[1] * move[i])
        for i in range(2)
    )


def white_balance_improved(before: float, after: float, threshold: float) -> bool:
    return before - after > max(0.0, threshold)
