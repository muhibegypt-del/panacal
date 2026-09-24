from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from colour import code_fraction
from domain import XYZ
from hardware import invalid_reading, measure, read_meter


class Log:
    def __init__(self):
        self.lines = []

    def write(self, message):
        self.lines.append(message)


class Pattern:
    def __init__(self):
        self.shown = []

    def show(self, level):
        self.shown.append(level)


class ScriptedMeter:
    """Returns the scripted readings in order; exceptions are raised."""

    def __init__(self, script):
        self.script = list(script)
        self.restarts = 0

    def read(self):
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def restart(self):
        self.restarts += 1


WHITE = XYZ(95.047, 100.0, 108.883)


def grey(Y):
    return XYZ(WHITE.X * Y / 100, Y, WHITE.Z * Y / 100)


def run(meter, level=50, count=1, expected=None):
    pattern, log = Pattern(), Log()
    row = measure(pattern, meter, log, level, count, 0, "test",
                  expected_y=expected, sleep=lambda _s: None)
    return row, pattern, log


class InvalidReadingTests(unittest.TestCase):
    def test_zero_or_null_readings_are_invalid_on_lit_patches(self):
        self.assertTrue(invalid_reading(XYZ(0, 0, 0), 10))
        self.assertTrue(invalid_reading(XYZ(-0.1, -0.1, -0.1), 10))
        self.assertTrue(invalid_reading(XYZ(0.1, 0.1, 0.1), 10))      # equal-energy null
        self.assertEqual(invalid_reading(grey(1.8), 10), "")

    def test_true_black_may_read_zero(self):
        self.assertEqual(invalid_reading(XYZ(0, 0, 0), 0), "")

    def test_invalid_reads_are_discarded_and_replaced(self):
        meter = ScriptedMeter([XYZ(0, 0, 0), grey(1.8), grey(1.9), grey(1.7)])
        row, _, log = run(meter, level=10, count=3)
        self.assertAlmostEqual(row.xyz.Y, 1.8)
        self.assertTrue(any(line.startswith("DISCARD") for line in log.lines))

    def test_persistent_invalid_reads_fail(self):
        meter = ScriptedMeter([XYZ(0, 0, 0)] * 10)
        with self.assertRaisesRegex(RuntimeError, "invalid readings"):
            run(meter, level=10, count=1)

    def test_zero_black_is_reported_as_neutral(self):
        row, _, _ = run(ScriptedMeter([XYZ(0, 0, 0)]), level=0)
        self.assertEqual(row.xyz.Y, 0)
        self.assertAlmostEqual(row.x, 0.3127)


class PlausibilityTests(unittest.TestCase):
    def test_implausible_reading_reshows_the_patch_and_rereads(self):
        # The session 183117 fault: 45% read darker than 30%.
        meter = ScriptedMeter([grey(5.35), grey(15.6)])
        row, pattern, log = run(meter, level=45, expected=15.8)
        self.assertAlmostEqual(row.xyz.Y, 15.6)
        self.assertEqual(pattern.shown, [45, 45])
        self.assertTrue(any(line.startswith("IMPLAUSIBLE") for line in log.lines))

    def test_persistently_implausible_reading_stops_with_advice(self):
        meter = ScriptedMeter([grey(1.0)] * 3)
        with self.assertRaisesRegex(RuntimeError, "centred on the patch"):
            run(meter, level=50, expected=22.0)

    def test_shadows_are_not_judged_against_the_gamma_target(self):
        # Raised black makes 10% far brighter than 2.4 gamma; that is the
        # preflight's job, not a reason to re-read.
        row, pattern, _ = run(ScriptedMeter([grey(1.9)]), level=10, expected=0.4)
        self.assertEqual(pattern.shown, [10])

    def test_measurement_records_the_drawn_signal(self):
        row, _, _ = run(ScriptedMeter([grey(1.8)]), level=10)
        self.assertAlmostEqual(row.signal, code_fraction(26))


class MeterRecoveryTests(unittest.TestCase):
    def test_transient_failure_restarts_spotread_and_retries(self):
        meter = ScriptedMeter([TimeoutError("spotread measurement timed out"), grey(50)])
        log = Log()
        self.assertEqual(read_meter(meter, log).Y, 50)
        self.assertEqual(meter.restarts, 1)
        self.assertTrue(any("METER RECOVERY" in line for line in log.lines))

    def test_repeated_failure_is_raised(self):
        meter = ScriptedMeter([RuntimeError("spot read failed")] * 3)
        with self.assertRaises(RuntimeError):
            read_meter(meter, Log())
        self.assertEqual(meter.restarts, 2)


if __name__ == "__main__":
    unittest.main()
