from __future__ import annotations

import json
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from colour import (D65_UV, D65_XYZ, bt1886_target_y, delta_e_2000,
                    evaluate, metrics, power_target_y, xyz_to_lab, xyz_to_xyy)
from domain import Measurement, XYZ
from hardware import PatternHost, TVSession
from autocal import connect_tv, validate_signal_path
from solver import (gamma_improved, propose_gamma, propose_red_blue,
                    white_balance_improved)


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
        result = evaluate(rows, black_y, white_y, "power", 2.4)
        self.assertLess(result.chroma_score, 1e-10)
        self.assertLess(result.gamma_score, 1e-10)
        for row in rows:
            item = metrics(row, black_y, white_y, "power", 2.4)
            self.assertLess(item.total_delta_e, 1e-10)


class SolverTests(unittest.TestCase):
    def test_red_blue_solver_moves_toward_target_and_stays_bounded(self):
        baseline = Measurement(
            level=50,
            xyz=XYZ(1, 1, 1),
            x=1/3,
            y=1/3,
            u=D65_UV[0] + 0.010,
            v=D65_UV[1] - 0.020,
            read_count=1,
        )
        proposal = propose_red_blue(
            current={"R": 0, "B": 0},
            codes=("R", "B"),
            baseline=baseline,
            red_response_per_step=(0.001, 0.0),
            blue_response_per_step=(0.0, 0.002),
            target_uv=D65_UV,
            cap=4,
            gain=0.70,
        )
        self.assertEqual(proposal.applied_move, (-4, 4))
        self.assertEqual(proposal.values, {"R": -4, "B": 4})

    def test_every_proposal_respects_tv_and_iteration_bounds(self):
        baseline = Measurement(50, XYZ(1, 1, 1), 1/3, 1/3,
                               D65_UV[0] + 0.5, D65_UV[1] - 0.5, 1)
        for start in range(-50, 51, 5):
            proposal = propose_red_blue(
                {"R": start, "B": start}, ("R", "B"), baseline,
                (0.01, 0.0), (0.0, 0.01), D65_UV, cap=4,
            )
            self.assertTrue(all(-50 <= value <= 50 for value in proposal.values.values()))
            self.assertTrue(all(-4 <= move <= 4 for move in proposal.applied_move))

    def test_gamma_proposal_moves_in_correct_direction(self):
        low = measurement(50, XYZ(0.18, 0.18, 0.18))
        raised = propose_gamma(0, low, desired_y=0.20,
                               log_y_response_per_step=0.01, cap=12, gain=0.75)
        lowered = propose_gamma(0, low, desired_y=0.16,
                                log_y_response_per_step=0.01, cap=12, gain=0.75)
        self.assertGreater(raised, 0)
        self.assertLess(lowered, 0)

    def test_acceptance_requires_real_improvement(self):
        self.assertTrue(white_balance_improved(1.0, 0.8, 0.03))
        self.assertFalse(white_balance_improved(1.0, 0.99, 0.03))
        self.assertTrue(gamma_improved(0.10, 0.05, 0.7, 0.75, 0.004, 0.10))
        self.assertFalse(gamma_improved(0.10, 0.05, 0.7, 0.90, 0.004, 0.10))


class ConfigurationTests(unittest.TestCase):
    def test_current_tv_settings_and_meter_configuration(self):
        config = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
        self.assertNotIn("required_picture", config["tv"])
        self.assertNotIn("reset_calibration_controls", config["tv"])
        self.assertEqual(config["pattern"]["window_area"], 0.065)
        self.assertEqual(config["pattern"]["settle_seconds"], 2.0)
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

    def test_signal_guard_accepts_reference_chain_and_rejects_raised_black(self):
        config = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
        rows = [
            measurement(0, XYZ(0.006, 0.007, 0.008)),
            measurement(5, XYZ(0.08, 0.09, 0.10)),
            measurement(95, XYZ(100.0, 105.0, 110.0)),
            measurement(100, XYZ(113.0, 120.0, 127.0)),
        ]
        evidence = validate_signal_path(rows, config)
        self.assertLess(evidence["black_white_ratio"], 0.001)
        raised = list(rows)
        raised[0] = measurement(0, XYZ(0.3, 0.325, 0.4))
        with self.assertRaisesRegex(RuntimeError, "black is raised"):
            validate_signal_path(raised, config)


if __name__ == "__main__":
    unittest.main()
