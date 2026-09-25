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
    """Draws one grey patch and reads it (the only I/O in this module)."""

    def __init__(self, pattern, meter, log, patch_size: int, settle_scale: float = 1.0):
        self.pattern, self.meter, self.log = pattern, meter, log
        self.area = window_area(patch_size)
        self.settle_scale = settle_scale

    def read(self, code: int, settle: float = 1.8, samples: int = 1) -> tuple[float, float, float]:
        if samples < 1:
            raise ValueError("samples must be at least 1")
        self.pattern.show(code, code, code, self.area)
        time.sleep(settle * self.settle_scale)
        values = [read_with_recovery(self.meter, self.log) for _ in range(samples)]
        return tuple(sum(v[i] for v in values) / samples for i in range(3))


# --- pure decisions -----------------------------------------------------------

MIN_WHITE = 10.0          # cd/m2: a meter on a lit patch reads far more
STEADY = 0.03             # two white readings within 3%
PATCH_DROP = 0.05         # black must read under 5% of white when the meter is on the patch
LIFTED_BLACK = 0.0008     # black above this fraction of white is lifted
LIMITED_SHADOW = 0.0017   # code 24 below this fraction of white: TV expects limited range


def steady_white(previous: float | None, current: float) -> bool:
    return previous is not None and current >= MIN_WHITE and abs(current - previous) <= STEADY * current


def on_patch(white: float, black: float) -> bool:
    """The reading follows the patch (room light would not drop)."""
    return white > 0 and black <= PATCH_DROP * white


def classify_range(black: float, shadow: float, white: float) -> str:
    """'full', 'limited' or 'lifted' from black, full-code-24 and white.

    Code 24 is 9.4% signal. A TV expecting full range shows it at 0.3-0.6%
    of white; one expecting limited range (Black Level Low) sees 3.7% and
    shows 0.04-0.07%. A lifted black means the GPU squeezes its output to
    limited while the TV expects full range, which no pattern can undo."""
    if white <= 0:
        raise ValueError("white luminance must be positive")
    if black > LIFTED_BLACK * white:
        return "lifted"
    return "limited" if shadow < LIMITED_SHADOW * white else "full"


def verification_row(level: int, xyz, white: float, target_gamma: str, limited: bool) -> dict:
    code = code_for_slot(level, limited)
    target = xyz_from_xyy(*D65, white * target_yn(stimulus_for_code(code, limited), target_gamma))
    record = xyz_record(xyz)
    return {"level": level, "code": code, "Y": xyz[1], "target_Y": target[1], "x": record["x"],
            "y": record["y"], "de": delta_e_itp(xyz, target)}


def summarize(rows: list[dict]) -> dict:
    if not rows:
        raise ValueError("no verification readings")
    des = [row["de"] for row in rows]
    return {"rows": rows, "average_de": sum(des) / len(des), "max_de": max(des)}


# --- measured steps -----------------------------------------------------------

def wait_for_meter(reader: Reader, say, timeout: float = 900, clock=time.monotonic) -> dict:
    """Start as soon as the meter is on the patch: two steady bright
    readings, then a black flash must drop the reading."""
    say("Put the meter flat on the white patch in the middle of the TV. It starts by itself.")
    started = clock()
    next_hint = started + 45
    previous = None
    warned_room_light = False
    while clock() - started < timeout:
        Y = reader.read(255, settle=0.3)[1]
        ready = steady_white(previous, Y)
        previous = Y
        if ready:
            if on_patch(Y, reader.read(0, settle=1.0)[1]):
                white = xyz_record(reader.read(255, settle=2.0, samples=2))
                say(f"Meter found. Current white: {white['Y']:.1f} cd/m2  "
                    f"x={white['x']:.4f} y={white['y']:.4f}")
                return white
            previous = None
            if not warned_room_light:
                warned_room_light = True
                say("The meter sees light that does not come from the patch. Put it flat against the "
                    "screen, centred on the white square.")
        if clock() >= next_hint:
            next_hint = clock() + 45
            say(f"  Still waiting: the meter reads {Y:.2f} cd/m2 (needs a steady reading of at least "
                f"{MIN_WHITE:.0f} from the white patch).")
    raise SystemExit("No steady reading from the white patch within 15 minutes. Check the meter is "
                     "plugged in and lying flat on the patch, then run again.")


def measure_white(reader: Reader, say) -> dict:
    """100% white with the meter already in place (after the TV changed)."""
    white = xyz_record(reader.read(255, settle=2.0, samples=2))
    if white["Y"] < MIN_WHITE:
        raise SystemExit(f"White now reads only {white['Y']:.2f} cd/m2; the meter may have moved off the "
                         "patch. Put it back on the white square and run again.")
    say(f"White after the reset: {white['Y']:.1f} cd/m2  x={white['x']:.4f} y={white['y']:.4f}")
    return white


def detect_range(reader: Reader, white_y: float, say) -> bool:
    """Return True when the TV expects limited range (16-235)."""
    black = reader.read(0, settle=1.5, samples=2)[1]
    shadow = reader.read(24, settle=1.5, samples=2)[1]
    verdict = classify_range(black, shadow, white_y)
    reader.log(f"RANGE black={black:.4f} code24={shadow:.4f} white={white_y:.2f} -> {verdict}")
    if verdict == "lifted":
        raise SystemExit(
            f"Black is lifted ({black:.3f} cd/m2): the PC is sending limited-range video but the TV "
            "expects full range. Either set the GPU output to Full RGB (NVIDIA: Output dynamic range "
            "Full; AMD: Pixel Format RGB 4:4:4 PC Standard) or set the TV's Black Level to Low/Auto, "
            "then run again.")
    say("The TV expects limited-range video (Black Level Low); patterns will use codes 16-235."
        if verdict == "limited" else "The TV expects full-range video; patterns will use codes 0-255.")
    return verdict == "limited"


VERIFY_LEVELS = [100, 90, 80, 70, 60, 50, 40, 30, 20, 10, 5]


def verify(reader: Reader, target_gamma: str, limited: bool, say) -> dict:
    """Measure the committed calibration against the same targets and the
    same dE ITP the worker uses (white = this measured 100%)."""
    say("")
    say("Verifying the result (independent measurement of the final calibration) ...")
    rows = []
    white = 0.0
    for level in VERIFY_LEVELS:
        xyz = reader.read(code_for_slot(level, limited), samples=2 if level <= 10 else 1)
        white = white or xyz[1]
        rows.append(verification_row(level, xyz, white, target_gamma, limited))
    say(f"{'Level':>6}  {'Y cd/m2':>9}  {'target':>9}  {'x':>7}  {'y':>7}  {'dE ITP':>6}")
    for row in rows:
        say(f"{row['level']:>5}%  {row['Y']:9.3f}  {row['target_Y']:9.3f}  {row['x']:7.4f}  "
            f"{row['y']:7.4f}  {row['de']:6.2f}")
    summary = summarize(rows)
    say(f"Average dE ITP {summary['average_de']:.2f}, worst {summary['max_de']:.2f}")
    return summary
