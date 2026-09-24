from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from colour import (D65_UV, D65_XYZ, bt1886_target_y, code_fraction, delta_e_2000,
                    metrics, power_target_y, signal_code, tint, xyz_to_lab, xyz_to_xyy)
from domain import Measurement, XYZ
from hardware import PatternHost, TVSession
from autocal import connect_tv, resolve_connection, validate_signal_path
from solver import best_integer_move, predict_error, update_model, white_balance_improved


def measurement(level: int, xyz: XYZ) -> Measurement:
    x, y, Y = xyz_to_xyy(xyz)
    denominator = -2*x + 12*y + 3
    return Measurement(level, xyz, x, y, 4*x/denominator, 9*y/denominator, 1)


class ColourMathTests(unittest.TestCase):
    def test_xyz_to_xyy_preserves_luminance(self):
        x, y, Y = xyz_to_xyy(XYZ(21.451712, 22.837241, 26.267488))
        self.assertAlmostEqual(x, 0.3040362, places=6)
        self.assertAlmostEqual(y, 0.3236734, places=6)
        self.assertEqual(Y, 22.837241)

    def test_published_sharma_ciede2000_pair(self):
        self.assertAlmostEqual(
            delta_e_2000((50.0, 2.6772, -79.7751), (50.0, 0.0, -82.7485)),
            2.0425,
            places=4,
        )

    def test_identical_colours_have_zero_delta_e(self):
        lab = xyz_to_lab(D65_XYZ)
        self.assertLess(delta_e_2000(lab, lab), 1e-12)

    def test_target_curves_hit_measured_endpoints(self):
        for function in (power_target_y, bt1886_target_y):
            self.assertAlmostEqual(function(0, 0.007, 120.0), 0.007, places=12)
            self.assertAlmostEqual(function(100, 0.007, 120.0), 120.0, places=10)

    def test_perfect_d65_power_curve_has_zero_error(self):
        black_y, white_y = 0.0, 120.0
        rows = []
        for level in range(5, 101, 5):
            Y = power_target_y(level, black_y, white_y, 2.4)
            rows.append(measurement(level, XYZ(D65_XYZ.X*Y, Y, D65_XYZ.Z*Y)))
        for row in rows:
            item = metrics(row, black_y, white_y, "power", 2.4)
            self.assertLess(item.total_delta_e, 1e-10)
            self.assertLess(item.chroma_delta_e, 1e-10)
            self.assertLess(abs(item.log_y_error), 1e-10)
            self.assertLess(tint(row), 1e-9)

    def test_signal_codes_round_half_up_and_targets_use_the_drawn_code(self):
        self.assertEqual(signal_code(10), 26)
        self.assertEqual(signal_code(30), 77)   # 76.5: banker's rounding gave 76
        self.assertEqual(signal_code(50), 128)
        self.assertEqual(signal_code(95), 242)
        self.assertEqual(signal_code(100, "limited"), 235)
        self.assertEqual(signal_code(0, "limited"), 16)
        self.assertAlmostEqual(code_fraction(26), 26 / 255)
        white = 120.0
        drawn = code_fraction(26)
        Y = white * drawn ** 2.4
        row = Measurement(10, XYZ(D65_XYZ.X*Y/1, Y, D65_XYZ.Z*Y), 0.3127, 0.3290,
                          D65_UV[0], D65_UV[1], 1, signal=drawn)
        self.assertLess(abs(metrics(row, 0.0, white).log_y_error), 1e-12)


class SolverTests(unittest.TestCase):
    MODEL = ((0.001, 0.0), (0.0, 0.002))   # red moves u', blue moves v'

    def test_integer_search_finds_the_exact_whole_step_correction(self):
        plan = best_integer_move({"R": 0, "B": 0}, ("R", "B"), (0.004, -0.004), self.MODEL, cap=4)
        self.assertEqual(plan.move, (-4, 2))
        self.assertEqual(plan.values, {"R": -4, "B": 2})
        self.assertLess(max(abs(value) for value in plan.predicted_error), 1e-12)

    def test_small_corrections_are_not_rounded_away(self):
        # 0.6 of a step needed: the old damped solver rounded 0.42 to no move.
        plan = best_integer_move({"R": 0, "B": 0}, ("R", "B"), (0.0006, 0.0), self.MODEL, cap=4)
        self.assertEqual(plan.move, (-1, 0))

    def test_already_best_setting_proposes_no_move(self):
        plan = best_integer_move({"R": 3, "B": -2}, ("R", "B"), (0.0002, -0.0003), self.MODEL, cap=4)
        self.assertEqual(plan.move, (0, 0))

    def test_every_proposal_respects_tv_and_step_bounds(self):
        for start in range(-50, 51, 5):
            plan = best_integer_move({"R": start, "B": start}, ("R", "B"),
                                     (0.5, -0.5), self.MODEL, cap=4)
            self.assertTrue(all(-50 <= value <= 50 for value in plan.values.values()))
            self.assertTrue(all(-4 <= move <= 4 for move in plan.move))

    def test_model_update_reproduces_the_observed_change(self):
        true = ((0.0015, 0.0002), (-0.0001, 0.0025))
        move = (2, -1)
        observed = predict_error((0.0, 0.0), true, move)
        updated = update_model(self.MODEL, move, observed, weight=1.0)
        predicted = predict_error((0.0, 0.0), updated, move)
        self.assertAlmostEqual(predicted[0], observed[0], places=12)
        self.assertAlmostEqual(predicted[1], observed[1], places=12)
        half = update_model(self.MODEL, move, observed, weight=0.5)
        self.assertNotEqual(half, self.MODEL)
        self.assertEqual(update_model(self.MODEL, (0, 0), observed), self.MODEL)

    def test_acceptance_requires_real_improvement(self):
        self.assertTrue(white_balance_improved(1.0, 0.8, 0.03))
        self.assertFalse(white_balance_improved(1.0, 0.99, 0.03))


class ConfigurationTests(unittest.TestCase):
    def test_current_tv_settings_and_meter_configuration(self):
        config = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
        self.assertNotIn("required_picture", config["tv"])
        self.assertNotIn("reset_calibration_controls", config["tv"])
        self.assertEqual(config["pattern"]["window_area"], 0.065)
        self.assertEqual(config["pattern"]["settle_seconds"], 0.5)
        self.assertEqual(config["pattern"]["range"], "full")
        self.assertIn("PlasmaFamily_20Jul12.ccss", config["meter"]["correction_file"])
        self.assertIn("-X", config["meter"]["args"])



    def test_tv_connection_retries_transient_timeout(self):
        attempts = []
        messages = []

        class Connected:
            pass

        def factory(ip, log, models, mode):
            attempts.append((ip, tuple(models), mode))
            if len(attempts) == 1:
                raise TimeoutError("temporary")
            return Connected()

        class Log:
            def write(self, message):
                messages.append(message)

        config = {"tv": {"accepted_models": ["VT60"], "mode": "DAY"}}
        connected = connect_tv(
            "192.168.1.105", Log(), config, retries=2, cycles=1,
            pause=lambda seconds: None, factory=factory,
        )
        self.assertIsInstance(connected, Connected)
        self.assertEqual(len(attempts), 2)
        self.assertTrue(any("TV CONNECT RETRY TimeoutError" in row for row in messages))

    def test_numeric_reply_must_match_requested_control(self):
        tv = TVSession.__new__(TVSession)
        tv.command = lambda _: "QWB:HIB+08"
        with self.assertRaisesRegex(RuntimeError, "Unexpected reply"):
            tv.get_number("WB:HIR")
        tv.command = lambda _: "QWB:HIR-05"
        self.assertEqual(tv.get_number("WB:HIR"), -5)
        tv.command = lambda _: "QWB:HIRjunk000"
        with self.assertRaisesRegex(RuntimeError, "Cannot parse"):
            tv.get_number("WB:HIR")

    def test_tv_wire_framing_accepts_fragmented_reply(self):
        class Socket:
            def __init__(self):
                self.parts = iter([bytes([2]) + b"QWB:", b"HIR+0", b"8" + bytes([3])])
                self.sent = []
            def sendall(self, data):
                self.sent.append(data)
            def recv(self, size):
                return next(self.parts)
        class Log:
            def write(self, text):
                pass
        tv = TVSession.__new__(TVSession)
        tv.sock, tv.log, tv.buffer = Socket(), Log(), bytearray()
        self.assertEqual(tv.get_number("WB:HIR"), 8)
        self.assertEqual(tv.sock.sent, [bytes([2]) + b"QWB:HIR" + bytes([3])])

    def test_picture_contrast_encoder_allows_sixty(self):
        tv = TVSession.__new__(TVSession)
        sent = []
        tv.command = lambda body: sent.append(body) or ""
        tv.get_number = lambda code: 60
        self.assertEqual(tv.set_number("PC:CON", 60, 0, 100), 60)
        self.assertEqual(sent, ["VPC:CON060"])

    def test_dispwin_mapping_uses_device_name_not_display_position(self):
        listing = """ 1 = 'DISPLAY3, at 0, 0, width 2560, height 1440'
 2 = 'DISPLAY2, at 3072, 0, width 1536, height 864'"""
        self.assertEqual(PatternHost._dispwin_display_index(listing, r"\\.\DISPLAY2"), 2)

    def test_connection_comes_from_config_or_command_line_without_prompts(self):
        config = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
        config["meter"]["default_executable"] = sys.executable
        self.assertEqual(resolve_connection(config), (config["tv"]["default_ip"], Path(sys.executable)))
        self.assertEqual(resolve_connection(config, "10.0.0.9")[0], "10.0.0.9")
        with self.assertRaisesRegex(RuntimeError, "--meter"):
            resolve_connection(config, meter="C:/missing/spotread.exe")

    def test_signal_guard_accepts_reference_chain_and_rejects_raised_black(self):
        config = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
        rows = [
            measurement(0, XYZ(0.006, 0.007, 0.008)),
            measurement(10, XYZ(0.45, 0.48, 0.52)),
            measurement(95, XYZ(100.0, 105.0, 110.0)),
            measurement(100, XYZ(113.0, 120.0, 127.0)),
        ]
        evidence = validate_signal_path(rows, config)
        self.assertLess(evidence["black_white_ratio"], 0.001)
        raised = list(rows)
        raised[0] = measurement(0, XYZ(0.3, 0.325, 0.4))
        with self.assertRaisesRegex(RuntimeError, "black is raised"):
            validate_signal_path(raised, config)
        crushed = list(rows)
        crushed[1] = measurement(10, XYZ(0.02, 0.02, 0.02))
        with self.assertRaisesRegex(RuntimeError, "10% is crushed"):
            validate_signal_path(crushed, config)


if __name__ == "__main__":
    unittest.main()
