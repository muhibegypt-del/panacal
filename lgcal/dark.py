"""Extend the calibrated curve into levels too dark for the meter.

The worker cannot steer a patch the meter cannot read: on a Spyder5 the
readings at 5% (0.16 cd/m2) fell while the TV was driven brighter, and the
solver doubled the 5% LUT value. Below the dimmest trustworthy level
(steps.dark_threshold) the worker is told to take one look and keep its
seed, but its seed pins 2.3% at the uncorrected value. This module replaces
that dark end, on the final commit only, with the calibrated curve carried
down to black: per channel, a power law fitted to the LUT at the lowest
calibrated levels, as a calibrator extends a curve past a meter's floor.
"""
from __future__ import annotations

import math

from .steps import code_for_slot

# meter_lg_autocal.pl: the legal-domain 10-bit codes where the panel samples
# its 1D DPG, and the matching table indexes (hardware-probed by the author).
LADDER_CODES = (84, 92, 100, 108, 124, 152, 196, 240, 284, 328, 372, 416, 460, 504, 544, 588,
                632, 676, 720, 764, 808, 852, 896, 932, 984, 1023)
LADDER_INDEXES = (21, 30, 38, 47, 64, 94, 141, 188, 235, 282, 329, 375, 422, 469, 512, 559,
                  606, 653, 700, 747, 794, 841, 888, 926, 981, 1023)
# Calibrated levels the fit uses: the lowest trusted one and the next three.
FIT_LADDER = (5, 7, 10, 15, 20, 25, 30, 35)
FIT_POINTS = 4
# Identity is 1.0; a native gamma 2.0-2.4 panel taken to 2.4 needs 1.0-1.2.
EXPONENT_RANGE = (0.85, 1.4)


def sample_index(slot: float, limited: bool = False) -> int:
    """Table index the worker adjusts for a ladder slot (8-bit patterns)."""
    code = code_for_slot(slot, limited)
    legal = 64 + (code - 16) * 876 / 219 if limited else 64 + (code << 2) * 876 / 1023
    if legal <= LADDER_CODES[0]:
        slope = (LADDER_INDEXES[1] - LADDER_INDEXES[0]) / (LADDER_CODES[1] - LADDER_CODES[0])
        return max(1, int(LADDER_INDEXES[0] + (legal - LADDER_CODES[0]) * slope + 0.5))
    for k in range(len(LADDER_CODES) - 1):
        if LADDER_CODES[k] <= legal <= LADDER_CODES[k + 1]:
            f = (legal - LADDER_CODES[k]) / (LADDER_CODES[k + 1] - LADDER_CODES[k])
            return int(LADDER_INDEXES[k] + (LADDER_INDEXES[k + 1] - LADDER_INDEXES[k]) * f + 0.5)
    return LADDER_INDEXES[-1]


def fit_exponent(points: list[tuple[int, float]]) -> float:
    """Least-squares slope of log(value) against log(index), clamped."""
    usable = [(math.log(i), math.log(v)) for i, v in points if i > 0 and v > 0]
    if len(usable) < 2:
        return 1.0
    mx = sum(x for x, _ in usable) / len(usable)
    my = sum(y for _, y in usable) / len(usable)
    sxx = sum((x - mx) ** 2 for x, _ in usable)
    if sxx <= 0:
        return 1.0
    slope = sum((x - mx) * (y - my) for x, y in usable) / sxx
    return min(EXPONENT_RANGE[1], max(EXPONENT_RANGE[0], slope))


def extend_dark_end(dpg: list, threshold: float, limited: bool = False) -> tuple[list[int], dict]:
    """Return (table, report) with every index below the threshold slot's
    index rebuilt from the calibrated values at and above it."""
    if not isinstance(dpg, list) or len(dpg) != 3072:
        raise ValueError("a 1D DPG table has 3072 values")
    fit_slots = [s for s in FIT_LADDER if s >= threshold][:FIT_POINTS]
    if len(fit_slots) < 2:
        raise ValueError(f"no calibrated levels above {threshold}% to extend from")
    base = sample_index(fit_slots[0], limited)
    out = [int(v) for v in dpg]
    report = {"threshold": threshold, "from_index": base, "fit_slots": fit_slots, "exponents": []}
    for channel in range(3):
        table = out[channel * 1024:(channel + 1) * 1024]
        points = [(sample_index(s, limited), float(table[sample_index(s, limited)])) for s in fit_slots]
        k = fit_exponent(points)
        report["exponents"].append(round(k, 4))
        top = table[base]
        for i in range(1, base):
            table[i] = min(top, int(round(top * (i / base) ** k)))
        table[0] = 0
        for i in range(1, base):
            table[i] = max(table[i], table[i - 1])
        out[channel * 1024:(channel + 1) * 1024] = table
    return out, report


def is_final_commit(payload: dict) -> bool:
    """The worker's single-socket commit: the upload that also ends
    calibration mode (every upload during the run keeps it held)."""
    return not payload.get("keep_calibration_mode") and not payload.get("calibration_mode_active")
