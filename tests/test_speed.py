from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autocal import measure_settle
from domain import XYZ
from hardware import replace_with_retry


class Log:
    def write(self, _message):
        pass


class LaggyRig:
    """Pattern, TV and meter that only show a change after `lag` seconds."""

    def __init__(self, lag: float):
        self.lag = lag
        self.now = 0.0
        self.changed_at = 0.0
        self.level = 100
        self.hir = 0

    def sleep(self, seconds):
        self.now += seconds

    def show(self, level):
        if level != self.level:
            self.changed_at = self.now
        self.level = level

    def get_number(self, _code):
        return self.hir

    def set_number(self, _code, value):
        if value != self.hir:
            self.changed_at = self.now
        self.hir = value
        return value

    def read(self):
        settled = self.now - self.changed_at >= self.lag
        white = XYZ(95.047, 100.0, 108.883)
        if settled and self.level == 100 and self.hir == 0:
            return white
        # Integration started early: part of the previous picture is included.
        return XYZ(white.X * 0.9, 93.0, white.Z * 0.97)


class SettleTests(unittest.TestCase):
    def run_rig(self, lag):
        rig = LaggyRig(lag)
        pattern = type("P", (), {"show": staticmethod(rig.show)})()
        meter = type("M", (), {"read": staticmethod(rig.read)})()
        return rig, measure_settle(pattern, meter, rig, Log(), sleep=rig.sleep)

    def test_recommends_shortest_settled_delay(self):
        rig, result = self.run_rig(lag=0.4)
        self.assertEqual(result["recommended_settle_seconds"], 0.5)
        self.assertEqual(rig.hir, 0)

    def test_fast_rig_gets_shortest_delay(self):
        _, result = self.run_rig(lag=0.1)
        self.assertEqual(result["recommended_settle_seconds"], 0.25)

    def test_slow_rig_falls_back_to_two_seconds(self):
        _, result = self.run_rig(lag=1.5)
        self.assertEqual(result["recommended_settle_seconds"], 2.0)


class ReplaceRetryTests(unittest.TestCase):
    def test_waits_out_a_windows_sharing_lock(self):
        calls = []
        def flaky(source, target):
            calls.append((source, target))
            if len(calls) < 3:
                raise PermissionError(13, "Access is denied")
        with patch("hardware.os.replace", side_effect=flaky):
            replace_with_retry(Path("a.tmp"), Path("a.json"))
        self.assertEqual(len(calls), 3)

    def test_persistent_lock_still_fails(self):
        with patch("hardware.os.replace", side_effect=PermissionError(13, "Access is denied")):
            with self.assertRaises(PermissionError):
                replace_with_retry(Path("a.tmp"), Path("a.json"), timeout=0.02)


if __name__ == "__main__":
    unittest.main()
