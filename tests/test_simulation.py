from __future__ import annotations

import copy
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autocal import AutoCal, load_config, summarise_sweep
from colour import D65_UV, power_target_y
from domain import XYZ


class FakeLog:
    def __init__(self):
        self.lines = []

    def write(self, message):
        self.lines.append(message)


class FakePattern:
    def __init__(self):
        self.level = 50

    def show(self, level):
        self.level = level


class FakeTV:
    def __init__(self):
        self.selected = 10
        self.two = {code: 0 for code in
                    ("WB:HIR", "WB:HIG", "WB:HIB", "WB:LOR", "WB:LOG", "WB:LOB")}
        self.detail = {
            str(level): {"WB:GNR": 0, "WB:GNG": 0, "WB:GNB": 0, "PC:GGN": 0}
            for level in range(10, 101, 10)
        }

    def select_point(self, level):
        self.selected = level

    def get_number(self, code):
        if code in self.two:
            return self.two[code]
        return self.detail[str(self.selected)][code]

    def set_number(self, code, value):
        value = max(-50, min(50, int(value)))
        if code in self.two:
            self.two[code] = value
        else:
            self.detail[str(self.selected)][code] = value
        return value

    def snapshot(self):
        return {
            "model": "SIMULATED-VT60",
            "picture": {"PC:BRI": 30, "PC:CON": 60, "PC:CGU": "REC",
                        "PC:TMP": "WRM2", "PC:GMM": "2.4", "PC:PBR": "Low"},
            "two_point": copy.deepcopy(self.two),
            "detail": copy.deepcopy(self.detail),
        }


class FakeMeter:
    def __init__(self, pattern, tv):
        self.pattern = pattern
        self.tv = tv

    @staticmethod
    def influence(level, centre, width):
        return max(0.0, 1.0 - abs(level-centre)/width)

    @staticmethod
    def xyz_from_uvY(u, v, Y):
        denominator = 6*u - 16*v + 12
        x = 9*u / denominator
        y = 4*v / denominator
        X = x*Y/y
        Z = (1-x-y)*Y/y
        return XYZ(X, Y, Z)

    def read(self):
        level = self.pattern.level
        p = level/100.0
        black, white = 0.005, 120.0
        Y = power_target_y(level, black, white, 2.4)
        # Smooth factory errors plus a small local ripple for detailed controls.
        u = D65_UV[0] + 0.0010*(1-p) - 0.0007*p + 0.00035*math.sin(level/13)
        v = D65_UV[1] - 0.0008*(1-p) + 0.0008*p + 0.00030*math.cos(level/11)

        low_weight = max(0.0, 1.0-level/45.0)
        high_weight = max(0.0, (level-45.0)/55.0)
        u += self.tv.two["WB:LOR"]*0.00028*low_weight
        v += self.tv.two["WB:LOR"]*0.00003*low_weight
        u -= self.tv.two["WB:LOB"]*0.00003*low_weight
        v -= self.tv.two["WB:LOB"]*0.00028*low_weight
        u += self.tv.two["WB:HIR"]*0.00028*high_weight
        v += self.tv.two["WB:HIR"]*0.00003*high_weight
        u -= self.tv.two["WB:HIB"]*0.00003*high_weight
        v -= self.tv.two["WB:HIB"]*0.00028*high_weight

        for slot_text, controls in self.tv.detail.items():
            slot = int(slot_text)
            centre = 95 if slot == 100 else slot
            weight = self.influence(level, centre, 11.0)
            u += controls["WB:GNR"]*0.00020*weight
            v += controls["WB:GNR"]*0.00002*weight
            u -= controls["WB:GNB"]*0.00002*weight
            v -= controls["WB:GNB"]*0.00020*weight
            Y *= math.exp(controls["PC:GGN"]*0.004*weight)
        return self.xyz_from_uvY(u, v, Y)


class SimulatedClosedLoopTests(unittest.TestCase):
    def test_complete_loop_improves_saved_objective(self):
        config = load_config()
        config["pattern"]["settle_seconds"] = 0.0
        config["meter"]["reads"] = {"black": 1, "shadow": 1, "normal": 1, "white": 1}
        config["solver"]["two_point_iterations"] = 1
        config["solver"]["detail_iterations"] = 1
        pattern = FakePattern()
        tv = FakeTV()
        meter = FakeMeter(pattern, tv)
        log = FakeLog()
        autocal = AutoCal(tv, pattern, meter, log, config)
        # The simulator independently records the starting error for this
        # comparison. Production run() must not do an opening sweep.
        before = summarise_sweep(autocal.sweep("simulation_before"), autocal)
        result = autocal.run()
        self.assertNotIn("baseline", result)
        self.assertNotIn("baseline_summary", result)
        after = result["final_summary"]
        self.assertLess(after["average_chroma_delta_e_2000"],
                        before["average_chroma_delta_e_2000"])
        self.assertLess(after["maximum_chroma_delta_e_2000"],
                        before["maximum_chroma_delta_e_2000"])
        for value in tv.two.values():
            self.assertTrue(-50 <= value <= 50)
        for controls in tv.detail.values():
            self.assertTrue(all(-50 <= value <= 50 for value in controls.values()))


    def test_direct_start_probes_after_one_white_read_and_only_sweeps_at_end(self):
        from unittest.mock import patch
        config = load_config()
        config["pattern"]["settle_seconds"] = 0
        config["meter"]["reads"] = {"black": 1, "shadow": 1, "normal": 1, "white": 1}
        config["solver"].update(two_point_iterations=1, detail_iterations=1)
        pattern, tv, log = FakePattern(), FakeTV(), FakeLog()
        tv.two["WB:HIR"] = 20
        tv.two["WB:HIB"] = -20
        meter = FakeMeter(pattern, tv)
        autocal = AutoCal(tv, pattern, meter, log, config)
        events = []
        read = meter.read
        write = tv.set_number
        def record_read():
            events.append(("read", pattern.level))
            return read()
        def record_write(code, value):
            events.append(("write", code, value))
            return write(code, value)
        meter.read = record_read
        tv.set_number = record_write
        with patch.object(autocal, "sweep", wraps=autocal.sweep) as sweep:
            result = autocal.run()
        first_write = next(i for i, event in enumerate(events) if event[0] == "write")
        self.assertEqual(events[:first_write], [("read", 100)])
        self.assertIn(events[first_write][1], ("WB:HIR", "WB:HIB"))
        self.assertEqual([call.args[0] for call in sweep.call_args_list], ["final_verification"])
        self.assertEqual(result["workflow"], "direct_two_point")
        self.assertEqual(result["starting_white"].level, 100)
        self.assertEqual([row.level for row in result["final"]], [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 95, 100])

    def test_two_point_reuses_reference_and_accepted_primary_readings(self):
        config = load_config()
        config["pattern"]["settle_seconds"] = 0
        config["meter"]["reads"] = {"black": 1, "shadow": 1, "normal": 1, "white": 1}
        pattern, tv, log = FakePattern(), FakeTV(), FakeLog()
        tv.two["WB:HIR"] = 20
        tv.two["WB:HIB"] = -20
        autocal = AutoCal(tv, pattern, FakeMeter(pattern, tv), log, config)
        white = autocal.read(100, "seed", fast=True)
        autocal.white_y = white.xyz.Y
        stages = []
        read = autocal.read
        def record(level, stage, fast=False):
            stages.append((level, stage))
            return read(level, stage, fast)
        autocal.read = record
        history = autocal.optimise_white_balance(
            "two_point_high", 100, [60, 80, 100], ("WB:HIR", "WB:HIB"),
            None, cap=4, iterations=2, initial_primary=white,
        )
        self.assertEqual(len(history), 2)
        self.assertTrue(history[0]["accepted"])
        self.assertFalse(any("_before_" in stage for _, stage in stages))
        self.assertFalse(any(level == 100 and "_reference_" in stage for level, stage in stages))
        for item in history:
            self.assertEqual([row.level for row in item["candidate_measurements"]], [60, 80, 100])

    def test_two_point_rejects_a_candidate_that_worsens_the_guard_levels(self):
        from dataclasses import replace
        from colour import xyz_to_xyy, xyz_to_uv
        config = load_config()
        config["pattern"]["settle_seconds"] = 0
        config["meter"]["reads"] = {"black": 1, "shadow": 1, "normal": 1, "white": 1}
        pattern, tv, log = FakePattern(), FakeTV(), FakeLog()
        tv.two["WB:HIR"] = 20
        tv.two["WB:HIB"] = -20
        original = dict(tv.two)
        autocal = AutoCal(tv, pattern, FakeMeter(pattern, tv), log, config)
        white = autocal.read(100, "seed", fast=True)
        autocal.white_y = white.xyz.Y
        read = autocal.read
        def worsen_guard(level, stage, fast=False):
            row = read(level, stage, fast)
            if level == 60 and "_candidate_" in stage:
                xyz = XYZ(row.xyz.X * 3, row.xyz.Y, row.xyz.Z * 0.1)
                x, y, _ = xyz_to_xyy(xyz)
                u, v = xyz_to_uv(xyz)
                return replace(row, xyz=xyz, x=x, y=y, u=u, v=v)
            return row
        autocal.read = worsen_guard
        history = autocal.optimise_white_balance(
            "two_point_high", 100, [60, 80, 100], ("WB:HIR", "WB:HIB"),
            None, cap=4, iterations=1, initial_primary=white,
        )
        self.assertFalse(history[0]["accepted"])
        self.assertEqual(tv.two, original)


if __name__ == "__main__":
    unittest.main()
