"""CRT Low Slider: the curve and its guard rails (the Windows parts are
thin wrappers around SetDeviceGammaRamp)."""
from __future__ import annotations

import unittest

import crt_low_slider as c


class Curve(unittest.TestCase):
    def test_zero_trims_are_a_linear_ramp(self):
        ramp, report = c.build({})
        self.assertEqual(ramp, c.identity())
        self.assertEqual({r["merged"] for r in report.values()}, {0})

    def test_black_and_white_never_move(self):
        for trim in (-16, -5, 5, 16):
            for reach in (1, 2.5, 4):
                ramp, _ = c.build({"red": trim, "green": -trim, "blue": trim, "reach": reach})
                for channel in ramp:
                    self.assertEqual((channel[0], channel[255]), (0, 65535))

    def test_every_setting_stays_within_the_guard_rails(self):
        for trim in (-16, -12, -8, -4, -1, 1, 4, 8, 12, 16):
            for reach in (1, 1.5, 2, 3, 4):
                for knee in (0.02, 0.05, 0.15):
                    _, report = c.build({"red": trim, "green": -trim, "blue": trim / 2,
                                         "reach": reach, "knee": knee})
                    for r in report.values():
                        self.assertGreaterEqual(r["min_slope"], c.SLOPE_MIN - 1e-9)
                        self.assertLessEqual(r["max_slope"], c.SLOPE_MAX + 1e-9)
                        self.assertLessEqual(r["largest_step"], 2)

    def test_ramps_rise_monotonically(self):
        ramp, _ = c.build({"red": 16, "green": -16, "blue": 9, "reach": 1})
        for channel in ramp:
            self.assertTrue(all(b >= a for a, b in zip(channel, channel[1:])))

    def test_trim_direction_and_where_it_acts(self):
        knee = c.safe_knee(10, 1.5, 0.03)
        levels = c.curve(10, 1.5, knee)
        lift = [levels[i] - i for i in range(256)]
        self.assertGreater(lift[51], 5)          # 20%: lifted
        self.assertLess(lift[204], 1.5)          # 80%: almost untouched
        lower = c.curve(-10, 1.5, c.safe_knee(-10, 1.5, 0.03))
        self.assertLess(lower[51], 51)

    def test_a_steep_ramp_in_is_widened_rather_than_stepped(self):
        _, report = c.build({"green": -12, "knee": 0.02})
        self.assertTrue(report["green"]["widened"])
        self.assertGreater(report["green"]["knee"], 0.02)

    def test_settings_are_clamped(self):
        s = c.clean({"red": 99, "green": -99, "reach": 0, "knee": 5})
        self.assertEqual((s["red"], s["green"], s["reach"], s["knee"]),
                         (c.MAX_TRIM, -c.MAX_TRIM, c.REACH_MIN, c.KNEE_MAX))

    def test_ramp_comparison_tolerates_driver_rounding(self):
        a = c.identity()
        b = [[min(65535, v + 200) for v in ch] for ch in a]
        self.assertTrue(c.same(a, b))
        b[1][100] += 400
        self.assertFalse(c.same(a, b))

    def test_fade_level(self):
        self.assertEqual(c.fade_level(1), 90)
        self.assertLess(c.fade_level(4), c.fade_level(1.5))


if __name__ == "__main__":
    unittest.main()
