"""Meter placement, signal-range detection and the closing verification.

Everything here is measured, so nothing has to be set by hand: the run
starts when the meter is seen on the patch, the pattern range follows what
the TV is actually expecting, and the final table is a fresh measurement of
the committed calibration rather than the solver's own best readings.
"""
from __future__ import annotations

import math
import time

from .meter import read_with_recovery, xyz_record
from .patterns import window_area
from .steps import code_for_slot, stimulus_for_code, target_yn

# PGMath.pm PQ constants (SMPTE ST 2084).
PQ_M1, PQ_M2 = 2610 / 16384, 2523 / 4096 * 128
PQ_C1, PQ_C2, PQ_C3 = 3424 / 4096, 2413 / 4096 * 32, 2392 / 4096 * 32
D65 = (0.3127, 0.3290)


def pq_encode(nits: float) -> float:
    if nits <= 0:
        return 0.0
    powered = (min(nits, 10000.0) / 10000) ** PQ_M1
    return ((PQ_C1 + PQ_C2 * powered) / (1 + PQ_C3 * powered)) ** PQ_M2


def ictcp(X: float, Y: float, Z: float) -> tuple[float, float, float]:
    """PGMath.pm xyz_to_ictcp (absolute cd/m2)."""
    R = max(0.0, 1.7166511880 * X - 0.3556707838 * Y - 0.2533662814 * Z)
    G = max(0.0, -0.6666843518 * X + 1.6164812366 * Y + 0.0157685458 * Z)
    B = max(0.0, 0.0176398574 * X - 0.0427706133 * Y + 0.9421031212 * Z)
    L = pq_encode((1688 * R + 2146 * G + 262 * B) / 4096)
    M = pq_encode((683 * R + 2951 * G + 462 * B) / 4096)
    S = pq_encode((99 * R + 309 * G + 3688 * B) / 4096)
    return (0.5 * L + 0.5 * M, (6610 * L - 13613 * M + 7003 * S) / 4096,
            (17933 * L - 17390 * M - 543 * S) / 4096)


def delta_e_itp(xyz1, xyz2) -> float:
    """PGMath.pm delta_e_itp_xyz: the metric the AutoCal worker targets."""
    a, b = ictcp(*xyz1), ictcp(*xyz2)
    return 720 * math.sqrt((a[0] - b[0]) ** 2 + 0.25 * (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def xyz_from_xyy(x: float, y: float, Y: float) -> tuple[float, float, float]:
    return (x / y * Y, Y, (1 - x - y) / y * Y)


class Reader:
    def __init__(self, pattern, meter, log, patch_size: int, settle_scale: float = 1.0):
        self.pattern, self.meter, self.log = pattern, meter, log
        self.area = window_area(patch_size)
        self.settle_scale = settle_scale

    def read(self, code: int, settle: float = 1.8, samples: int = 1) -> tuple[float, float, float]:
        self.pattern.show(code, code, code, self.area)
        time.sleep(settle * self.settle_scale)
        values = [read_with_recovery(self.meter, self.log) for _ in range(samples)]
        return tuple(sum(v[i] for v in values) / samples for i in range(3))


def wait_for_meter(reader: Reader, say, timeout: float = 900) -> dict:
    """Start as soon as the meter is on the patch: two steady bright
    readings, then a black flash must drop the reading (room light would
    not change with the patch)."""
    say("Put the meter flat on the white patch in the middle of the TV. It starts by itself.")
    deadline = time.monotonic() + timeout
    previous = None
    while time.monotonic() < deadline:
        Y = reader.read(255, settle=0.3)[1]
        steady = previous is not None and Y >= 10 and abs(Y - previous) <= 0.03 * Y
        previous = Y
        if not steady:
            continue
        dark = reader.read(0, settle=1.0)[1]
        if dark > 0.05 * Y:
            previous = None
            continue
        white = xyz_record(reader.read(255, settle=2.0, samples=2))
        say(f"Meter found. Current white: {white['Y']:.1f} cd/m2  x={white['x']:.4f} y={white['y']:.4f}")
        return white
    raise SystemExit("No meter reading from the patch within 15 minutes.")


def detect_range(reader: Reader, white_y: float, say) -> bool:
    """Return True when the TV expects limited range (16-235).

    Full code 24 is 9.4% signal. A TV expecting full range shows it at
    0.3-0.6% of white; a TV expecting limited range (Black Level Low) sees
    3.7% and shows 0.04-0.07%. A lifted black instead means the GPU is
    squeezing its output to limited while the TV expects full range, which
    no pattern can undo."""
    black = reader.read(0, settle=1.5, samples=2)[1]
    shadow = reader.read(24, settle=1.5, samples=2)[1]
    reader.log(f"RANGE black={black:.4f} code24={shadow:.4f} white={white_y:.2f} "
               f"ratio={shadow / white_y:.5f}")
    if black > 0.0008 * white_y:
        raise SystemExit(
            f"Black is lifted ({black:.3f} cd/m2): the PC is sending limited-range video but the TV "
            "expects full range. Either set the GPU output to Full RGB (NVIDIA: Output dynamic range "
            "Full; AMD: Pixel Format RGB 4:4:4 PC Standard) or set the TV's Black Level to Low/Auto, "
            "then run again.")
    limited = shadow / white_y < 0.0017
    say("The TV expects limited-range video (Black Level Low); patterns will use codes 16-235."
        if limited else "The TV expects full-range video; patterns will use codes 0-255.")
    return limited


VERIFY_LEVELS = [100, 90, 80, 70, 60, 50, 40, 30, 20, 10, 5]


def verify(reader: Reader, target_gamma: str, limited: bool, say) -> dict:
    """Measure the committed calibration against the same targets and the
    same dE ITP the worker uses (white = this measured 100%)."""
    say("")
    say("Verifying the result (independent measurement of the final calibration) ...")
    rows = []
    white = None
    for level in VERIFY_LEVELS:
        code = code_for_slot(level, limited)
        xyz = reader.read(code, samples=2 if level <= 10 else 1)
        if white is None:
            white = xyz[1]
        stimulus = stimulus_for_code(code, limited)
        target = xyz_from_xyy(*D65, white * target_yn(stimulus, target_gamma))
        record = xyz_record(xyz)
        rows.append({"level": level, "Y": xyz[1], "target_Y": target[1], "x": record["x"],
                     "y": record["y"], "de": delta_e_itp(xyz, target)})
    say(f"{'Level':>6}  {'Y cd/m2':>9}  {'target':>9}  {'x':>7}  {'y':>7}  {'dE ITP':>6}")
    for row in rows:
        say(f"{row['level']:>5}%  {row['Y']:9.3f}  {row['target_Y']:9.3f}  {row['x']:7.4f}  "
            f"{row['y']:7.4f}  {row['de']:6.2f}")
    des = [row["de"] for row in rows]
    summary = {"rows": rows, "average_de": sum(des) / len(des), "max_de": max(des)}
    say(f"Average dE ITP {summary['average_de']:.2f}, worst {summary['max_de']:.2f}")
    return summary
